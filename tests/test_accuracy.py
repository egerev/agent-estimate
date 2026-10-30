"""Accuracy round 1: pre-work timing, interval coverage, 'no PR' estimates, --all, transcript dirs, gone worktrees.
Synthetic fixtures only (acme/shop, feat/x); no network, no real agent logs."""
import contextlib
import io
import json
import os
import random
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def pr(n=1, branch='feat/x', created_min=30, merged_min=120, commits=()):
    return {'number': n, 'title': 't', 'body': '', 'headRefName': branch, 'reviews': [], 'files': [],
            'createdAt': E.iso(T + timedelta(minutes=created_min)), 'mergedAt': E.iso(T + timedelta(minutes=merged_min)),
            'commits': [{'authoredDate': E.iso(T + timedelta(minutes=m))} for m in commits]}


def agent(first_min):
    return {'first_ts': T + timedelta(minutes=first_min), 'active_h': .5, 'usd': 1, 'known_usd': 1,
            'cost_complete': True, 'sessions': 1, 'executor': 'claude-code/m', 'agents': {}}


def cli(argv, env=None):
    out, err = io.StringIO(), io.StringIO(); code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.dict(os.environ, env or {}):
        try: E.main(argv)
        except SystemExit as e: code = e.code if isinstance(e.code, int) else 1; err.write(str(e.code or ''))
    return code, out.getvalue(), err.getvalue()


class SizingBeforeTheEstimate(unittest.TestCase):
    """A dogfood repo: the worker read the task for 6-70 s before logging its estimate, so the agent log
    started first and 13 of 15 estimates did not count as pre-work."""

    def settle(self, p, first_min, made_min=10):
        p['estimateStartAt'] = E.iso(T + timedelta(minutes=made_min))
        row = {'source': 'agent', 'created_at': p['estimateStartAt'], 'actual': E.pr_actual(p, agent(first_min))}
        return row['actual'], E._prospective_estimate(row)

    def test_agent_activity_minutes_before_the_estimate_is_sizing(self):
        a, prework = self.settle(pr(), first_min=9)
        self.assertEqual((a['start_source'], a['start_at']), ('estimate', E.iso(T + timedelta(minutes=10))))
        self.assertTrue(prework)

    def test_long_activity_before_the_estimate_is_work(self):
        a, prework = self.settle(pr(), first_min=10 - E.SIZING_GRACE_MIN - 1)
        self.assertEqual(a['start_source'], 'agent-log'); self.assertFalse(prework)

    def test_a_commit_before_the_estimate_is_work(self):
        a, prework = self.settle(pr(commits=(9.5,)), first_min=9)
        self.assertEqual(a['start_source'], 'agent-log'); self.assertFalse(prework)


class IntervalCoverage(unittest.TestCase):
    """A dogfood repo: p20-p80 of a few in-sample rates held 44% of new tasks; the leave-one-out error widens it."""

    def rows(self, n, seed=3):
        rng = random.Random(seed); out = []
        for i in range(n):
            sp = rng.choice((1, 2, 3, 5)); merged = T + timedelta(hours=i)
            v = sp * .2 * rng.lognormvariate(0, .6)
            out.append({'id': f'e{i}', 'sp': sp, 'source': 'agent', 'created_at': E.iso(merged - timedelta(hours=1)),
                        'estimator': {'agent': 'a', 'model': 'm'},
                        'actual': {'pr': i + 1, 'merged_at': E.iso(merged), 'start_at': E.iso(merged - timedelta(hours=1)),
                                   'wall_h': v * 2, 'active_h': v, 'usd': v, 'cost_complete': True}})
        return out

    def test_interval_is_never_narrower_and_the_median_stays(self):
        cal = E.calibrate(self.rows(12), T + timedelta(hours=13))
        self.assertEqual(len(cal['loo_resid']['active_h']), 2)
        plain = {**cal, 'loo_resid': {}}
        for sp in (1, 3, 8):
            wide, narrow = E.quote(cal, sp)['active_h'], E.quote(plain, sp)['active_h']
            self.assertEqual(wide['mid'], narrow['mid'])
            self.assertLessEqual(wide['lo'], narrow['lo']); self.assertGreaterEqual(wide['hi'], narrow['hi'])
        self.assertGreater(E.quote(cal, 3)['active_h']['hi'], E.quote(plain, 3)['active_h']['hi'])

    def test_simulated_backtest_holds_about_sixty_percent(self):
        wide, plain = [], []
        for seed in range(60):
            rs = self.rows(14, seed)
            cal = E.calibrate(rs[:10], T + timedelta(hours=10))
            for r in rs[10:]:
                a = r['actual']['active_h']
                for hits, c in ((wide, cal), (plain, {**cal, 'loo_resid': {}})):
                    q = E.quote(c, r['sp'])['active_h']; hits.append(q['lo'] <= a <= q['hi'])
        rate = lambda hits: sum(hits) / len(hits)
        self.assertLess(rate(plain), .52)                      # in-sample p20-p80 of 10 rates: about half
        self.assertGreaterEqual(rate(wide), .55); self.assertLess(rate(wide), .7)

    def test_residuals_use_the_bucket_forecast_a_quote_uses(self):
        # Six 1-SP and six 8-SP tasks, all one hour: every bucket forecast is exact, so nothing widens.
        rows = self.rows(12)
        for i, r in enumerate(rows): r['sp'] = 1 if i < 6 else 8; r['actual']['active_h'] = 1.0
        cal = E.calibrate(rows, T + timedelta(hours=13))
        self.assertEqual(cal['loo_resid']['active_h'], [0, 0])
        q = E.quote(cal, 8)['active_h']; self.assertEqual((q['lo'], q['hi']), (1.0, 1.0))

    def test_few_rows_have_no_residual_interval(self):
        self.assertIsNone(E.loo_resid(self.rows(E.MIN_INTERVAL_N - 1), 'active_h'))


class NoPr(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = E.Store(self.tmp.name)
        self.design = E.new_estimate(3, 'design/x', 'agent', T, 'claude-code', 'm')
        self.fresh = E.new_estimate(3, 'feat/new', 'agent', T + timedelta(days=6), 'claude-code', 'm')
        self.opened = E.new_estimate(3, 'feat/open', 'agent', T, 'claude-code', 'm')
        self.number = E.new_estimate(3, '#77', 'agent', T, 'claude-code', 'm')
        self.store.append([self.design, self.fresh, self.opened, self.number])

    def record(self, merged=(), now=T + timedelta(days=8), *extra, has_pr=None):
        # feat/open has an open PR that the bounded history page does not show; only a per-branch query finds it
        has_pr = has_pr or (lambda repo, branch: branch == 'feat/open')
        with patch.object(E, 'fetch_prs', return_value=list(merged)), patch.object(E, 'fetch_pr', return_value={'mergedAt': None}), \
                patch.object(E, 'branch_has_pr', side_effect=has_pr), \
                patch.object(E, 'measure', side_effect=lambda prs, *a: {p['number']: E.pr_actual(p, None) for p in prs}):
            return cli(['--repo', 'acme/shop', '--dir', self.tmp.name, '--now', E.iso(now), 'record', *extra])

    def test_old_branch_without_a_pr_leaves_the_pending_list_once(self):
        out = self.record()[1]
        self.assertIn("marked 1 estimate 'no PR'", out); self.assertIn('design/x', out)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertTrue(rows[self.design['id']]['no_pr'])
        for kept in (self.fresh, self.opened, self.number): self.assertNotIn('no_pr', rows[kept['id']])
        again = self.record(now=T + timedelta(days=9))[1]
        self.assertNotIn('design/x', again); self.assertNotIn("marked", again)
        _, doc, _ = cli(['--repo', 'acme/shop', '--dir', self.tmp.name, '--now', E.iso(T + timedelta(days=9)), 'doctor'])
        self.assertIn('3 pending, 1 no PR', doc); self.assertIn('no PR        design/x', doc)

    def test_a_late_merged_pr_still_settles_it(self):
        self.record()
        late = pr(5, 'design/x', created_min=60 * 24 * 9, merged_min=60 * 24 * 9 + 60)
        self.record([late], T + timedelta(days=10))
        row = {r['id']: r for r in self.store.rows()}[self.design['id']]
        self.assertEqual(row['actual']['pr'], 5)

    def test_zero_days_never_marks(self):
        self.assertNotIn('marked', self.record((), T + timedelta(days=30), '--no-pr-days', '0')[1])

    def test_a_failed_branch_lookup_marks_nothing_and_still_settles(self):
        def down(repo, branch): sys.exit('estimate: gh pr list failed: rate limited')
        merged = pr(5, 'feat/new', created_min=60 * 24 * 7, merged_min=60 * 24 * 7 + 60)
        _, out, err = self.record([merged], T + timedelta(days=8), has_pr=down)
        self.assertIn("no estimates marked 'no PR' this run", err); self.assertIn('settled estimates on 1 PRs', out)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows[self.fresh['id']]['actual']['pr'], 5); self.assertNotIn('no_pr', rows[self.design['id']])


class RecordAll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); root = Path(self.tmp.name)
        self.reg = root / 'projects.json'; self.dirs = {}
        for repo in ('acme/shop', 'acme/api'):
            d = root / repo.replace('/', '-'); d.mkdir(); self.dirs[repo] = d
        self.reg.write_text(json.dumps({r: {'dir': str(d)} for r, d in self.dirs.items()}))
        self.env = {'ESTIMATE_REGISTRY': str(self.reg)}

    def fake_record(self, failing):
        def run(args):
            if args.repo in failing: sys.exit(f'estimate: gh pr list failed for {args.repo}\nmore')
            print('noise that --all keeps out')
            self.assertEqual(args.dir, str(self.dirs[args.repo]))
            return {'settled': 2, 'new_prs': 3, 'no_pr': 0, 'n': 7, 'prework': 4}
        return run

    def test_one_line_per_project_and_a_failure_does_not_stop_the_rest(self):
        with patch.object(E, 'cmd_record', self.fake_record({'acme/api'})):
            code, out, _ = cli(['record', '--all'], self.env)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ['acme/api: FAILED estimate: gh pr list failed for acme/api',
                                            'acme/shop: settled 2, new PRs 3, calibration n=7, pre-work n=4'])

    def test_exit_is_non_zero_only_when_every_project_fails(self):
        with patch.object(E, 'cmd_record', self.fake_record({'acme/api', 'acme/shop'})):
            code, out, _ = cli(['record', '--all'], self.env)
        self.assertNotEqual(code, 0); self.assertEqual(out.count('FAILED'), 2)

    def test_each_project_uses_its_own_transcript_dirs_not_the_shell_env(self):
        reg = json.loads(self.reg.read_text()); reg['acme/shop']['claude_project_dir'] = '/logs/shop'
        self.reg.write_text(json.dumps(reg)); seen = {}
        def run(args):
            seen[args.repo] = E.resolve(args)['claude_project_dir']
            return {'settled': 0, 'new_prs': 0, 'no_pr': 0, 'n': 0, 'prework': 0}
        with patch.object(E, 'cmd_record', run):
            cli(['record', '--all'], {**self.env, 'ESTIMATE_CLAUDE_PROJECT_DIR': '/logs/other'})
        self.assertEqual(seen, {'acme/api': [], 'acme/shop': ['/logs/shop']})

    def test_dashboard_all_and_flags_that_conflict(self):
        res = {'out': 'x.html', 'n': 1, 'prework': 0}
        with patch.object(E, 'cmd_dashboard', lambda args: res):
            code, out, _ = cli(['dashboard', '--all', '--no-burn'], self.env)
        self.assertEqual(code, 0); self.assertIn('acme/shop: wrote x.html, calibration n=1, pre-work n=0', out)
        code, _, err = cli(['--dir', self.tmp.name, 'record', '--all'], self.env)
        self.assertNotEqual(code, 0); self.assertIn('drop --dir', err)

    def test_dashboard_all_scans_the_agent_logs_once_and_prices_each_project_itself(self):
        logs = Path(self.tmp.name) / 'logs'; logs.mkdir(); seen = []
        def run(args):
            seen.append((args.now, E.scan_burn(logs, logs, E.utc(args.now) - timedelta(days=30), E.DEFAULT_PRICES)))
            return {'out': 'x.html', 'n': 1, 'prework': 0}
        with patch.object(E, 'cmd_dashboard', run), patch.object(E, '_scan_burn_logs', wraps=E._scan_burn_logs) as scan:
            code, _, _ = cli(['dashboard', '--all', '--no-burn'], self.env)
        self.assertEqual(code, 0); self.assertEqual(scan.call_count, 1); self.assertEqual(len(seen), 2)
        self.assertEqual(len({now for now, _ in seen}), 1)   # one clock for the run
        self.assertIsNone(E._SCAN_CACHE)                     # and the cache does not outlive it
        with patch.object(E, '_scan_burn_logs', wraps=E._scan_burn_logs) as scan:
            E.scan_burn(logs, logs, T, E.DEFAULT_PRICES); E.scan_burn(logs, logs, T, E.DEFAULT_PRICES)
        self.assertEqual(scan.call_count, 2)                 # outside --all every call reads the logs
        # one project hides session labels, the other does not: still one scan (Codex review r1)
        (self.dirs['acme/api'] / 'config.json').write_text(json.dumps({'session_labels': False}))
        mixed = []
        def labelled(args):
            mixed.append(E.scan_burn(logs, logs, T, E.DEFAULT_PRICES, None, {}, E.labels_on(args, E._quiet_config(args.dir))))
            return {'out': 'x.html', 'n': 1, 'prework': 0}
        with patch.object(E, 'cmd_dashboard', labelled), patch.object(E, '_scan_burn_logs', wraps=E._scan_burn_logs) as scan2:
            cli(['dashboard', '--all', '--no-burn'], self.env)
        self.assertEqual(scan2.call_count, 1)
        self.assertEqual(len(mixed), 2)

    def test_dashboard_all_goes_on_when_a_config_cannot_be_parsed(self):
        res = {'out': 'x.html', 'n': 1, 'prework': 0}
        with patch.object(E, 'cmd_dashboard', lambda args: res), patch.object(E, '_quiet_config', side_effect=RecursionError):
            code, out, _ = cli(['dashboard', '--all', '--no-burn'], self.env)
        self.assertEqual(code, 0); self.assertEqual(out.count('wrote x.html'), 2)


class TranscriptDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.claude = self.root / 'claude-projects'
        self.checkout = self.root / 'work' / 'acme.shop'; self.checkout.mkdir(parents=True)
        self.encoded = self.claude / re.sub(r'[^A-Za-z0-9]', '-', str(self.checkout)); self.encoded.mkdir(parents=True)

    def test_a_checkout_maps_to_its_transcript_dir(self):
        self.assertEqual(E.transcript_dir(self.checkout, self.claude)[0], str(self.encoded))
        self.assertEqual(E.transcript_dir(self.encoded, self.claude), (str(self.encoded), None))
        kept, note = E.transcript_dir(self.root / 'elsewhere', self.claude)
        self.assertEqual(kept, str(self.root / 'elsewhere')); self.assertIn('not a transcript dir', note or '')

    def test_link_stores_the_mapped_dir_and_doctor_warns(self):
        env = {'ESTIMATE_REGISTRY': str(self.root / 'projects.json')}
        data = self.root / 'data'; data.mkdir(); (data / 'history.json').write_text('[]')
        code, _, err = cli(['--claude-dir', str(self.claude), 'link', '--repo', 'acme/shop', '--dir', str(data),
                            '--claude-project-dir', str(self.checkout)], env)
        self.assertEqual(code, 0); self.assertIn('is a checkout', err)
        self.assertEqual(json.loads((self.root / 'projects.json').read_text())['acme/shop']['claude_project_dir'], str(self.encoded))
        with patch.object(E, 'gh_auth_ok', return_value=(True, 'ok')):
            _, out, _ = cli(['--claude-dir', str(self.claude), '--repo', 'acme/shop', '--claude-project-dir',
                             str(self.root / 'elsewhere'), 'doctor'], env)
        self.assertIn('warning:', out); self.assertIn('omit it, matching by repo/branch is the default', out)


class GoneWorktree(unittest.TestCase):
    """A dogfood repo: a worker's worktree was removed after merge, so its session lost repository identity on refresh."""

    def test_session_in_a_removed_worktree_inherits_a_pr_linked_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            gone = str(Path(d) / 'worktrees' / 'feat-x')   # never created: removed after merge
            def line(sid, minute, **kw):
                return {'type': 'assistant', 'timestamp': E.iso(T + timedelta(minutes=minute)), 'sessionId': sid,
                        'gitBranch': 'feat/x', 'cwd': gone, 'requestId': f'r{sid}{minute}',
                        'message': {'id': f'm{sid}{minute}', 'model': 'claude-opus-5-5', 'usage': {'output_tokens': 1}}, **kw}
            proj = Path(d) / 'logs' / '-worktrees-feat-x'; proj.mkdir(parents=True)
            (proj / 'a.jsonl').write_text('\n'.join(json.dumps(x) for x in (
                line('a', 1), {'type': 'pr-link', 'sessionId': 'a', 'prRepository': 'acme/shop', 'prNumber': 1})))
            (proj / 'b.jsonl').write_text(json.dumps(line('b', 2)))
            other = Path(d) / 'logs' / '-elsewhere'; other.mkdir()
            (other / 'c.jsonl').write_text(json.dumps({**line('c', 3), 'cwd': str(Path(d) / 'other-gone')}))
            sessions, _ = E.scan_claude_logs(Path(d) / 'logs', 'shop', 'acme/shop')
            self.assertEqual(sorted(sessions), ['a', 'b'])
            # a sibling that links a PR of another repo keeps that identity
            (proj / 'd.jsonl').write_text('\n'.join(json.dumps(x) for x in (
                line('d', 4), {'type': 'pr-link', 'sessionId': 'd', 'prRepository': 'acme/other', 'prNumber': 2})))
            self.assertEqual(sorted(E.scan_claude_logs(Path(d) / 'logs', 'shop', 'acme/shop')[0]), ['a', 'b'])


if __name__ == '__main__': unittest.main()
