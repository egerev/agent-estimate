"""`estimate usage` and the dashboard's Usage section: labels, repos per session, cache, trend, parallelism, models."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E  # noqa: E402

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
SECRET = 'SECRET-PROMPT-TAIL'
KEY = 'sk-' + 'live-' + 'abcdefghijklmnopqrstuvwxyz0123456789'   # built here so leak scanners skip a fake key


def at(hours_ago):
    return E.iso(NOW - timedelta(hours=hours_ago))


def usage(inp=10, out=100, read=1000, write=0):
    return {'input_tokens': inp, 'output_tokens': out, 'cache_read_input_tokens': read, 'cache_creation_input_tokens': write}


def assistant(ts, mid, session='s1', model='claude-opus-5-5', cwd='/w/a', **u):
    return json.dumps({'type': 'assistant', 'sessionId': session, 'requestId': 'r' + mid, 'timestamp': ts, 'cwd': cwd,
                       'message': {'id': mid, 'model': model, 'usage': usage(**u), 'content': [{'type': 'text', 'text': SECRET}]}})


def user(ts, content, session='s1', **extra):
    return json.dumps({'type': 'user', 'sessionId': session, 'timestamp': ts, 'cwd': '/w/a', **extra,
                       'message': {'role': 'user', 'content': content}})


def rec(ts, session, usd=1.0, model='claude-opus-5-5', agent='claude-code', u=None, tokens=None):
    u = E.normalize_usage(u or usage())
    return (ts, agent, model, tokens if tokens is not None else u['total_tokens'], usd, (session,), (session, ts), u, session)


class Labels(unittest.TestCase):
    def scan(self, d, labels=True):
        claude = Path(d) / 'claude' / '-w-a'; (claude / 's1' / 'subagents').mkdir(parents=True)
        long = 'Fix the flaky scheduler test and then ' + 'also refactor the clock helpers ' * 3 + SECRET
        (claude / 's1.jsonl').write_text('\n'.join([
            user(at(2), '<local-command-caveat>Caveat: harness text</local-command-caveat>', isMeta=True),
            user(at(2), [{'type': 'tool_result', 'content': 'tool output'}]),
            user(at(1.9), f'\n  {long}\nsecond line {SECRET}'),
            assistant(at(1.8), 'm1'), assistant(at(1.7), 'm2')]))
        (claude / 's1' / 'subagents' / 'agent-x.jsonl').write_text('\n'.join([
            user(at(1.75), 'A subagent prompt', isSidechain=True, agentId='x'),
            json.dumps({**json.loads(assistant(at(1.74), 'm3')), 'agentId': 'x', 'isSidechain': True})]))
        (claude / 's2.jsonl').write_text('\n'.join([user(at(1), '<command-name>/review</command-name><command-args></command-args>', session='s2'),
                                                    assistant(at(.9), 'm4', session='s2'),
                                                    user(at(.8), f'Deploy with key {KEY} now', session='s3'),
                                                    assistant(at(.7), 'm5', session='s3')]))
        codex = Path(d) / 'codex'; codex.mkdir()
        (codex / 'c.jsonl').write_text('\n'.join(json.dumps(x) for x in [
            {'type': 'session_meta', 'timestamp': at(3), 'payload': {'id': 'c1', 'cwd': '/w/gone', 'base_instructions': SECRET,
                                                                       'git': {'repository_url': 'https://bot:' + 'hunter2' + '@github.com/me/cx.git'}}},
            {'type': 'response_item', 'timestamp': at(3), 'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': '# AGENTS.md instructions for /w'}, {'type': 'input_text', 'text': '<environment_context>'}]}},
            {'type': 'response_item', 'timestamp': at(3), 'payload': {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Review the diff on this branch'}]}},
            {'type': 'turn_context', 'timestamp': at(3), 'payload': {'model': 'gpt-6.1-sol'}},
            {'type': 'event_msg', 'timestamp': at(2.9), 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'total_tokens': 60}, 'last_token_usage': {'input_tokens': 50, 'cached_input_tokens': 40, 'output_tokens': 10}}}}]))
        meta = {}
        recs = E.scan_burn(Path(d) / 'claude', codex, NOW - timedelta(days=7), E.DEFAULT_PRICES, None, meta, labels)
        return recs, meta

    def test_first_real_prompt_line_is_the_label_and_nothing_more(self):
        with tempfile.TemporaryDirectory() as d:
            recs, meta = self.scan(d)
        s1 = meta['claude:s1']
        self.assertTrue(s1['label'].startswith('Fix the flaky scheduler test'))
        self.assertLessEqual(len(s1['label']), E.LABEL_CHARS); self.assertTrue(s1['label'].endswith('…'))
        self.assertEqual(s1['cwd'], '/w/a')
        self.assertEqual(meta['claude:s2']['label'], '/review')
        self.assertNotIn('sk-live', meta['claude:s3']['label']); self.assertIn('Deploy with key', meta['claude:s3']['label'])
        codex = next(v for k, v in meta.items() if k.startswith('codex:'))
        self.assertEqual((codex['label'], codex['cwd'], codex['remote']), ('Review the diff on this branch', '/w/gone', 'me/cx'))
        self.assertNotIn('hunter2', repr(meta))                                    # the remote keeps owner/name, never userinfo
        self.assertNotIn(SECRET, repr(meta)); self.assertNotIn(SECRET, repr(recs))

    def test_a_subagent_pools_into_its_session_and_records_carry_usage(self):
        with tempfile.TemporaryDirectory() as d:
            recs, meta = self.scan(d)
        sub = next(r for r in recs if r[6][1] == 'm3')
        self.assertEqual(E.session_key(sub[8]), 'claude:s1')
        self.assertEqual(sub[7]['cache_read_input_tokens'], 1000)
        codex = next(r for r in recs if r[1] == 'codex')
        self.assertEqual((codex[7]['input_tokens'], codex[7]['cache_read_input_tokens']), (10, 40))
        self.assertEqual(E.burn_report(recs, NOW, timezone.utc)['today']['responses'], len(recs))   # burn reads the longer tuple

    def test_labels_off_reads_no_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            _, meta = self.scan(d, labels=False)
        self.assertEqual({v['label'] for v in meta.values()}, {None})
        self.assertEqual(meta['claude:s1']['cwd'], '/w/a')

    def test_label_rules(self):
        self.assertIsNone(E.claude_label({'content': '<system-reminder>x</system-reminder>'}))
        self.assertEqual(E.claude_label({'content': [{'type': 'text', 'text': 'Ship it\nmore'}]}), 'Ship it')
        self.assertIsNone(E.prompt_label('   \n  '))
        self.assertEqual(E.prompt_label('word ' * 30), 'word ' * 11 + 'word…')
        self.assertEqual(E.prompt_label('token a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6 end'), 'token … end')


class SessionRepos(unittest.TestCase):
    def git_dir(self, root, name, url):
        p = Path(root) / name; p.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(p)], check=True)
        subprocess.run(['git', '-C', str(p), 'remote', 'add', 'origin', url], check=True)
        return str(p)

    def test_worktrees_of_one_repo_pool_and_a_removed_one_follows_its_siblings(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.git_dir(d, 'wt/shop/feat-a', 'git@github.com:me/shop.git')
            b = self.git_dir(d, 'wt/shop/feat-b', 'https://github.com/me/shop')
            other = self.git_dir(d, 'wt/blog/main', 'https://github.com/me/blog.git')
            plain = Path(d) / 'notes'; plain.mkdir()
            meta = {'s1': {'cwd': a}, 's2': {'cwd': b}, 's3': {'cwd': other},
                    'gone': {'cwd': f'{d}/wt/shop/feat-c'},                  # removed worktree: its siblings are all me/shop
                    'mixed': {'cwd': f'{d}/wt/old/x'},                       # nothing resolved under wt/old
                    'cx': {'cwd': f'{d}/nowhere', 'remote': 'me/cx'},
                    'pr': {'cwd': f'{d}/gone2/y', 'repos': {'me/pr'}},
                    'plain': {'cwd': str(plain)}, 'none': {}}
            out = E.session_repos(meta, {})
        self.assertEqual([out[k] for k in ('s1', 's2', 's3', 'gone', 'cx', 'pr')], ['me/shop', 'me/shop', 'me/blog', 'me/shop', 'me/cx', 'me/pr'])
        self.assertTrue(out['mixed'].endswith('/wt/old/x (folder)')); self.assertTrue(out['plain'].endswith('/notes (folder)'))
        self.assertEqual(out['none'], '(no cwd)')

    def test_folders_without_a_repo_pool_by_their_top_folder_under_home(self):
        with tempfile.TemporaryDirectory() as home:
            for sub in ('orch/estimate', 'orch/notes/deep', 'other'): (Path(home) / sub).mkdir(parents=True)
            meta = {'a': {'cwd': f'{home}/orch/estimate'}, 'b': {'cwd': f'{home}/orch/notes/deep'}, 'c': {'cwd': f'{home}/other'},
                    'h': {'cwd': home}, 'gone': {'cwd': f'{home}/orch/removed/x'}, 'out': {'cwd': '/srv/app'}}
            with mock.patch.dict(os.environ, {'HOME': home}):
                out = E.session_repos(meta, {}, remote=lambda c: None)
        self.assertEqual(out, {'a': '~/orch (folder)', 'b': '~/orch (folder)', 'c': '~/other (folder)', 'h': '~ (folder)',
                               'gone': '~/orch (folder)', 'out': '/srv/app (folder)'})

    def test_a_git_repo_under_a_folder_keeps_its_own_name(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'orch/app').mkdir(parents=True)
            with mock.patch.dict(os.environ, {'HOME': home}):
                out = E.session_repos({'a': {'cwd': f'{home}/orch/app'}}, {}, remote=lambda c: 'me/app')
        self.assertEqual(out, {'a': 'me/app'})

    def test_registry_checkout_and_transcript_dirs_name_a_repo(self):
        reg = {'me/app': {'dir': '/x', 'claude_project_dir': ['/gone/app', '/claude/projects/-gone-app2']}}
        out = E.session_repos({'a': {'cwd': '/gone/app/sub'}, 'b': {'cwd': '/gone/app2', 'dir': '/claude/projects/-gone-app2'}}, reg,
                              remote=lambda c: None)
        self.assertEqual(out, {'a': 'me/app', 'b': 'me/app'})

    def test_merged_history_per_repo(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'history.json').write_text(json.dumps([{'number': 1, 'mergedAt': at(5)}, {'number': 2}]))
            (Path(d) / 'bad').mkdir(); (Path(d) / 'bad' / 'history.json').write_text('{')
            out = E.merged_by_repo({'me/a': {'dir': d}, 'me/b': {'dir': f'{d}/bad'}, 'me/c': {'dir': f'{d}/none'}})
        self.assertEqual(out['me/a'], [NOW - timedelta(hours=5)]); self.assertNotIn('me/b', out); self.assertNotIn('me/c', out)   # no history: unknown, not zero


class Report(unittest.TestCase):
    def report(self, recs, days=7, labels=True, merged=None, meta=None):
        meta = meta if meta is not None else {s: {'label': f'task {s}'} for s in ('a', 'b', 'c', 'x')}
        repos = {'a': 'me/shop', 'b': 'me/shop', 'c': 'me/blog', 'x': 'me/blog'}
        return E.usage_report(recs, meta, repos, NOW, days, E.DEFAULT_PRICES, tz=timezone.utc, labels=labels, merged=merged)

    def test_cache_tiles_and_savings(self):
        u = usage(inp=100, out=50, read=900, write=0)
        rep = self.report([rec(NOW - timedelta(hours=1), 'a', u=u), rec(NOW - timedelta(hours=2), 'b', u=usage(inp=0, out=0, read=0, write=100))])
        c = rep['cache']
        self.assertEqual((c['processed'], c['uncached_input'], c['output'], c['cache_read']), (1150, 200, 50, 900))
        self.assertAlmostEqual(c['cached_share'], 900 / 1100)
        self.assertAlmostEqual(c['savings_usd'], 900 * (4 - .2) / 1e6)   # opus-5-5: input $4, cached read $0.20 per M

    def test_repos_sessions_and_merged_prs_per_session(self):
        recs = [rec(NOW - timedelta(hours=h), s, usd=v) for h, s, v in ((1, 'a', 5), (2, 'b', 1), (3, 'c', 2), (30, 'a', 1))]
        recs.append(rec(NOW - timedelta(days=9), 'x', usd=100))      # before the period
        rep = self.report(recs, merged={'me/shop': [NOW - timedelta(hours=4), NOW - timedelta(days=20)]})
        self.assertEqual([(r['repo'], r['usd'], r['sessions'], r['prs']) for r in rep['repos']], [('me/shop', 7, 2, 1), ('me/blog', 2, 1, None)])
        self.assertEqual(rep['repos'][0]['prs_per_session'], .5)
        self.assertEqual((rep['parallel']['prs'], rep['parallel']['sessions'], rep['parallel']['prs_per_session']), (1, 2, .5))
        self.assertEqual([(s['repo'], s['usd'], s['label']) for s in rep['sessions']], [('me/shop', 6, 'task a'), ('me/blog', 2, 'task c'), ('me/shop', 1, 'task b')])
        self.assertEqual({s['label'] for s in self.report(recs, labels=False)['sessions']}, {None})
        self.assertEqual(rep['total']['sessions'], 3)

    def test_top_sessions_are_ten(self):
        recs = [rec(NOW - timedelta(minutes=k), f's{k}', usd=k) for k in range(1, 15)]
        rep = E.usage_report(recs, {}, {}, NOW, 7, E.DEFAULT_PRICES, tz=timezone.utc)
        self.assertEqual([s['usd'] for s in rep['sessions']], list(range(14, 4, -1)))

    def test_daily_trend_by_agent_and_model_over_local_days(self):
        recs = [rec(NOW - timedelta(hours=1), 'a', usd=2), rec(NOW - timedelta(hours=1, minutes=1), 'c', usd=1, model='gpt-6.1-sol', agent='codex'),
                rec(NOW - timedelta(days=6, hours=17), 'b', usd=3), rec(NOW - timedelta(days=6, hours=19), 'b', usd=50)]
        rep = self.report(recs)
        self.assertEqual(len(rep['daily']), 7); self.assertEqual(rep['daily'][0]['date'], '2026-10-02')
        self.assertEqual(rep['daily'][0]['agents'], {'claude-code': 3})               # 01:00 on the first day counts, 23:00 the day before does not
        self.assertEqual(rep['daily'][-1]['agents'], {'claude-code': 2, 'codex': 1})
        self.assertEqual(rep['daily'][-1]['models'], {'claude-opus-5-5': 2, 'gpt-6.1-sol': 1})
        self.assertEqual(rep['start'], '2026-10-02T00:00:00Z')

    def test_parallelism_over_active_slots(self):
        # a, b and c work together for 30 minutes (17:00-17:30), then a alone for 30 more: slots 17:00, 17:15, 17:30 x3, 17:45 x1
        t0 = NOW - timedelta(hours=1)
        recs = [rec(t0 + timedelta(minutes=m), s) for s in 'abc' for m in range(0, 31, 3)]
        recs += [rec(t0 + timedelta(minutes=m), 'a') for m in range(33, 59, 3)]
        p = self.report(recs)['parallel']
        self.assertEqual((p['slots'], p['median'], p['usually'], p['days'][0]['peak']), (4, 3, 3, 3))
        self.assertAlmostEqual(p['p80'], 3)
        self.assertIsNone(self.report([])['parallel']['usually'])

    def test_models_share_and_approximate_flag(self):
        recs = [rec(NOW - timedelta(hours=1), 'a', usd=3), rec(NOW - timedelta(hours=1), 'b', usd=1, model='claude-sonnet-5-5'),
                rec(NOW - timedelta(hours=2), 'c', usd=None, model='gpt-9-new', agent='codex', tokens=500)]
        rep = self.report(recs)
        m = {x['model']: x for x in rep['models']}
        self.assertEqual((m['claude-opus-5-5']['share'], m['claude-opus-5-5']['approximate']), (.75, False))
        self.assertEqual((m['gpt-9-new']['usd'], m['gpt-9-new']['approximate'], m['gpt-9-new']['tokens']), (None, True, 500))
        self.assertTrue(rep['total']['approximate']); self.assertEqual(rep['total']['unpriced_tokens'], 500)


class CodexPrices(unittest.TestCase):
    """OpenAI standard-tier list prices (https://developers.openai.com/api/docs/pricing, 2026-10-26)."""
    def cost(self, model, **u):
        return E.message_cost({'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0, **u}, E.DEFAULT_PRICES[E.model_family(model, E.DEFAULT_PRICES)])

    def test_gpt_6_sol_is_priced_on_its_own(self):
        self.assertEqual(E.model_family('gpt-6-sol', E.DEFAULT_PRICES), 'gpt-6-sol')
        self.assertEqual(E.model_family('gpt-6.1-sol', E.DEFAULT_PRICES), 'gpt-6.1-sol')
        # 100K prompt: 60K uncached input at $2, 40K cached at $0.20, 10K output at $10 per M
        self.assertAlmostEqual(self.cost('gpt-6-sol', input_tokens=100_000, cached_input_tokens=40_000, output_tokens=10_000),
                               (60_000 * 2 + 40_000 * .2 + 10_000 * 10) / 1e6)
        # 300K prompt (long context): $4 / $0.40 / $15
        self.assertAlmostEqual(self.cost('gpt-6-sol', input_tokens=300_000, cached_input_tokens=100_000, output_tokens=10_000),
                               (200_000 * 4 + 100_000 * .4 + 10_000 * 15) / 1e6)

    def test_the_other_codex_rows_match_the_same_page(self):
        sol, astra = E.DEFAULT_PRICES['gpt-6.1-sol'], E.DEFAULT_PRICES['gpt-6-astra']
        self.assertEqual([sol[k] for k in ('input', 'cache_read', 'output')], [2, .1, 10])
        self.assertEqual([sol['long_context'][k] for k in ('input', 'cache_read', 'output')], [4, .2, 15])
        self.assertEqual([astra[k] for k in ('input', 'cache_read', 'output')], [10, 1, 50])
        self.assertEqual([astra['long_context'][k] for k in ('input', 'cache_read', 'output')], [20, 2, 75])


class Command(unittest.TestCase):
    def run_usage(self, d, *extra, config=None):
        claude = Path(d) / 'claude' / 'p'; claude.mkdir(parents=True, exist_ok=True)
        (claude / 's1.jsonl').write_text('\n'.join([user(at(2), 'Size the backlog <b>now</b>'), assistant(at(1.9), 'm1'), assistant(at(1.8), 'm2')]))
        data = Path(d) / 'data'; data.mkdir(exist_ok=True)
        if config is not None: (data / 'config.json').write_text(json.dumps(config))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'ESTIMATE_REGISTRY': f'{d}/reg.json'}), contextlib.redirect_stdout(out):
            E.main(['--repo', 'o/r', '--dir', str(data), '--now', E.iso(NOW), '--claude-dir', f'{d}/claude', '--codex-dir', f'{d}/codex',
                    'usage', *extra])
        return out.getvalue()

    def test_json_and_text(self):
        with tempfile.TemporaryDirectory() as d:
            rep = json.loads(self.run_usage(d, '--days', '7', '--json'))
            text = self.run_usage(d)
        self.assertEqual((rep['days'], rep['total']['responses'], rep['sessions'][0]['label']), (7, 2, 'Size the backlog <b>now</b>'))
        for part in ('Usage, last 30 days', 'Cache', 'cache savings', 'By repo', 'Top 1 sessions by $', 'Daily $', 'Parallel sessions', 'Models'):
            self.assertIn(part, text)
        self.assertNotIn(SECRET, text)

    def test_labels_hide_by_flag_or_config(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(json.loads(self.run_usage(d, '--json', '--no-labels'))['sessions'][0]['label'])
            self.assertIsNone(json.loads(self.run_usage(d, '--json', config={'session_labels': False}))['sessions'][0]['label'])
            self.assertNotIn('Size the backlog', self.run_usage(d, '--no-labels'))

    def test_days_are_7_30_or_90(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual([json.loads(self.run_usage(d, '--days', str(n), '--json'))['days'] for n in (7, 30, 90)], [7, 30, 90])
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                self.run_usage(d, '--days', '14')
        self.assertIn('argument --days: invalid choice', err.getvalue())

    def test_no_logs_is_a_quiet_empty_report(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {'ESTIMATE_REGISTRY': f'{d}/reg.json'}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            E.main(['--dir', d, '--repo', 'o/r', '--now', E.iso(NOW), '--claude-dir', f'{d}/none', '--codex-dir', f'{d}/none', 'usage'])
        self.assertIn('No agent responses in this period.', out.getvalue())


class Dashboard(unittest.TestCase):
    def usage(self, labels=True):
        recs = [rec(NOW - timedelta(hours=h, minutes=m), s, usd=1.5, model=mo, agent=ag)
                for h in range(0, 40, 4) for m in (0, 3, 6) for s, mo, ag in (('a', 'claude-opus-5-5', 'claude-code'), ('c', 'gpt-6.1-sol', 'codex'))]
        meta = {'a': {'label': 'Fix <script>alert(1)</script> login'}, 'c': {'label': 'Review the diff'}}
        return {d: E.usage_report(recs, meta, {'a': 'me/shop', 'c': 'me/blog'}, NOW, d, E.DEFAULT_PRICES, tz=timezone.utc, labels=labels)
                for d in E.USAGE_PERIODS}

    def test_usage_section_is_static_and_escaped(self):
        page = E.dashboard_html('me/r', [], None, None, NOW, tz=timezone.utc, usage=self.usage())
        self.assertIn('aria-label="Usage"', page); self.assertEqual(page.count('<svg class="chart"'), 4)   # by agent and by model, 30 and 90 days
        self.assertIn('id="us-30" aria-label="Usage: last 30 days" checked', page)
        self.assertNotIn('<script', page); self.assertIn('Fix &lt;script&gt;', page)
        for part in ('Cached input', 'Cache savings', 'Spend by repo', 'Top sessions by $', 'Models', 'Sessions at once', 'me/shop'):
            self.assertIn(part, page)
        self.assertNotIn('Fix &lt;script', E.dashboard_html('me/r', [], None, None, NOW, tz=timezone.utc, usage=self.usage(labels=False)))

    def test_planner_shows_how_many_agents_you_usually_run(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / 'ui')); import fixtures  # noqa: E401
        page = fixtures.page(fixtures.history(), fixtures.BACKLOG, usage=self.usage())
        self.assertIn('<span class="hint">you usually run ~2</span>', page)
        self.assertNotIn('you usually run', fixtures.page(fixtures.history(), fixtures.BACKLOG))

    def test_a_failing_usage_report_leaves_the_rest_of_the_dashboard(self):
        with tempfile.TemporaryDirectory() as d:
            backlog = Path(d) / 'b.md'; backlog.write_text('| id | title | effort | value |\n|---|---|---|---|\n| 1 | a | 3 | 5 |\n')
            (Path(d) / 'config.json').write_text('{}'); out = Path(d) / 'dash.html'; err = io.StringIO()
            with mock.patch.object(E, 'usage_report', side_effect=KeyError('x')), contextlib.redirect_stderr(err), \
                    mock.patch.dict(os.environ, {'ESTIMATE_REGISTRY': f'{d}/reg.json', 'ESTIMATE_WINDOWS_FILE': f'{d}/w.jsonl',
                                                 'ESTIMATE_RATE_HISTORY': f'{d}/r.jsonl'}), contextlib.redirect_stdout(io.StringIO()):
                E.main(['--repo', 'o/r', '--dir', d, '--now', E.iso(NOW), '--claude-dir', d, '--codex-dir', d,
                        'dashboard', '--backlog', str(backlog), '--out', str(out)])
            page = out.read_text()
        self.assertIn('Agent logs could not be read for usage.', page); self.assertIn('3<small>SP in 1 card', page)
        self.assertIn('the Usage section was not built: KeyError', err.getvalue())



class ReviewFixes(unittest.TestCase):
    """Regressions from the Codex review of the first version."""

    def test_credentials_never_reach_a_label(self):
        cases = {'AWS key AS' + 'IAABCDEFGHIJKLMNOP please': 'AWS key … please',
                 'curl -H "Authorization: Bearer abc.def" now': 'curl -H "Authorization: Bearer … now',
                 'set password=hunter2 and token: s3cr3t': 'set password=… and token: …',
                 'clone https://bot:' + 'hunter2' + '@github.com/me/x': 'clone https://github.com/me/x',
                 'use eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig here': 'use … here',
                 'export GITHUB_TOKEN=gh' + 'p_abcdefghij1234567890': 'export GITHUB_TOKEN=…'}
        for text, want in cases.items():
            self.assertEqual(E.prompt_label(text), want, text)
        self.assertEqual(E.prompt_label('Fix https://example.com/docs/page and the session timeout'),
                         'Fix https://example.com/docs/page and the session timeout')

    def test_masking_is_linear(self):
        import time
        start = time.perf_counter(); E.prompt_label('a' * 200_000 + ' ' + 'b1' * 50_000)
        self.assertLess(time.perf_counter() - start, .2)

    def test_labels_off_decodes_no_user_line(self):
        with tempfile.TemporaryDirectory() as d:
            claude = Path(d) / 'claude' / 'p'; claude.mkdir(parents=True)
            prompt = json.dumps({'type': 'user', 'sessionId': 's1', 'timestamp': at(2), 'cwd': '/w/a',
                                 'message': {'role': 'user', 'content': 'PROMPT-ONLY-TEXT', 'usage': {'output_tokens': 1}}})
            (claude / 's1.jsonl').write_text('\n'.join([prompt, assistant(at(1.9), 'm1')]))
            decoded = []; real = json.loads
            with mock.patch.object(E.json, 'loads', side_effect=lambda x, *a, **k: decoded.append(x) or real(x, *a, **k)):
                recs = E.scan_burn(Path(d) / 'claude', Path(d) / 'none', NOW - timedelta(days=7), E.DEFAULT_PRICES, None, {}, False)
        self.assertEqual(len(recs), 1)
        self.assertFalse(any(b'PROMPT-ONLY-TEXT' in (x if isinstance(x, bytes) else x.encode()) for x in decoded))

    def test_odd_json_shapes_never_stop_the_scan(self):
        with tempfile.TemporaryDirectory() as d:
            codex = Path(d) / 'codex'; codex.mkdir(); claude = Path(d) / 'claude' / 'p'; claude.mkdir(parents=True)
            (codex / 'c.jsonl').write_text('\n'.join(['["user", "session_meta", "token_count"]', '"user_message"', '7']))
            (claude / 'a.jsonl').write_text('\n'.join(['["type", "usage", "pr-link"]', assistant(at(1), 'm1')]))
            recs = E.scan_burn(Path(d) / 'claude', codex, NOW - timedelta(days=7), E.DEFAULT_PRICES, None, {}, True)
        self.assertEqual(len(recs), 1)

    def test_a_drawing_error_leaves_the_planner_and_burn_rows(self):
        usage = Dashboard().usage(); err = io.StringIO()
        with mock.patch.object(E, 'usage_svg', side_effect=ZeroDivisionError), contextlib.redirect_stderr(err):
            page = E.dashboard_html('me/r', [], None, [{'id': '1', 'title': 'a', 'sp': 3}], NOW, tz=timezone.utc, usage=usage)
        self.assertIn('Agent logs could not be read for usage.', page); self.assertIn('3<small>SP in 1 card', page)
        self.assertIn('Usage section was not drawn: ZeroDivisionError', err.getvalue())

    def test_a_stalled_git_lookup_falls_back(self):
        def stall(cwd): raise subprocess.TimeoutExpired('git', E.GIT_TIMEOUT_S)
        with tempfile.TemporaryDirectory() as d:
            out = E.session_repos({'a': {'cwd': d, 'repos': {'me/pr'}}, 'b': {'cwd': d}}, {}, remote=stall)
        self.assertEqual(out['a'], 'me/pr'); self.assertTrue(out['b'].endswith(Path(d).name + ' (folder)'))

    def test_a_copied_response_stays_with_the_session_that_began_first(self):
        def rollout(began, events):
            return '\n'.join(json.dumps(x) for x in [
                {'type': 'session_meta', 'timestamp': began, 'payload': {'id': began, 'timestamp': began}},
                {'type': 'turn_context', 'timestamp': began, 'payload': {'model': 'gpt-6.1-sol'}}] + [
                {'type': 'event_msg', 'timestamp': ts, 'payload': {'type': 'token_count', 'info': {
                    'total_token_usage': {'total_tokens': total}, 'last_token_usage': {'input_tokens': total, 'output_tokens': 1}}}}
                for ts, total in events])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'codex'; root.mkdir()
            (root / 'a-fork.jsonl').write_text(rollout(at(1), [(at(3), 100), (at(.5), 300)]))     # sorts first, began later
            (root / 'b-original.jsonl').write_text(rollout(at(4), [(at(3), 100), (at(2), 200)]))
            recs = E.scan_burn(Path(d) / 'none', root, NOW - timedelta(days=7), E.DEFAULT_PRICES, None, {}, True)
        homes = {r[3]: Path(r[8]).name for r in recs}
        self.assertEqual(homes, {101: 'b-original.jsonl', 201: 'b-original.jsonl', 301: 'a-fork.jsonl'})

    def test_days_start_at_their_own_local_midnight_across_dst(self):
        from zoneinfo import ZoneInfo
        ny = ZoneInfo('America/New_York'); now = datetime(2026, 3, 15, 12, tzinfo=timezone.utc)
        recs = [rec(datetime(2026, 2, 14, 4, 30, tzinfo=timezone.utc), 'a'), rec(datetime(2026, 2, 14, 5, 30, tzinfo=timezone.utc), 'a')]
        rep = E.usage_report(recs, {}, {}, now, 30, E.DEFAULT_PRICES, tz=ny)
        self.assertEqual((rep['start'], rep['daily'][0]['date'], rep['total']['responses']), ('2026-02-14T05:00:00Z', '2026-02-14', 1))
        with mock.patch.dict(os.environ, {'TZ': 'America/New_York'}):
            import time
            time.tzset()
            try: rep = E.usage_report(recs, {}, {}, now, 30, E.DEFAULT_PRICES)
            finally:
                os.environ.pop('TZ', None)
            self.assertEqual(rep['start'], '2026-02-14T05:00:00Z')
        time.tzset()

    def test_an_unpriced_model_with_no_tokens_is_still_approximate(self):
        rep = E.usage_report([rec(NOW - timedelta(hours=1), 'a', usd=None, model='gpt-9-new', tokens=0)], {}, {}, NOW, 7,
                             E.DEFAULT_PRICES, tz=timezone.utc)
        self.assertEqual((rep['models'][0]['usd'], rep['models'][0]['approximate'], rep['total']['approximate']), (None, True, True))


if __name__ == '__main__':
    unittest.main()
