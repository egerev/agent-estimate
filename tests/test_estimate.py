"""Unit tests for the math and the log readers. Synthetic fixtures only; no network, no gh."""
import json
import random
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E  # noqa: E402

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(sp, merged_h, wall, active=None, usd=None, agent='claude-code', model='m1', pr=None, source='agent',
        closest=None, reviews=1, created=None):
    merged = T0 + timedelta(hours=merged_h)
    r = {'id': f'e-{random.random()}', 'sp': sp, 'source': source, 'closest_pr': closest,
         'created_at': E.iso(created or merged - timedelta(hours=wall)),
         'estimator': {'agent': agent, 'model': model},
         'actual': {'pr': pr if pr is not None else int(merged_h * 10), 'merged_at': E.iso(merged),
                    'start_at': E.iso(merged - timedelta(hours=wall)), 'wall_h': wall, 'active_h': active,
                    'usd': usd, 'cost_complete': usd is not None, 'review_rounds': reviews,
                    'executor': f'{agent}/{model}', 'title': 't', 'areas': []}}
    return r


class Percentiles(unittest.TestCase):
    def test_percentile_matches_linear_interpolation(self):
        xs = [1, 2, 3, 4, 5]
        self.assertEqual(E.percentile(xs, .5), 3)
        self.assertAlmostEqual(E.percentile(xs, .2), 1.8)
        self.assertAlmostEqual(E.percentile(xs, .8), 4.2)
        self.assertEqual(E.percentile([7], .8), 7)
        self.assertIsNone(E.percentile([], .5))

    def test_spread_falls_back_to_min_max_below_five(self):
        s = E.spread([4, 1, 2])
        self.assertEqual((s['lo'], s['mid'], s['hi'], s['kind']), (1, 2, 4, 'min-max'))
        s = E.spread([1, 2, 3, 4, 5, None])
        self.assertEqual(s['kind'], 'p20-p80'); self.assertEqual(s['n'], 5)

    def test_ratio_needs_five_values(self):
        self.assertIsNone(E.ratio_p80_p20([1, 2, 3, 4]))
        self.assertAlmostEqual(E.ratio_p80_p20([1, 2, 3, 4, 5]), round(4.2 / 1.8, 2))


class ActiveTime(unittest.TestCase):
    def test_gaps_over_idle_are_dropped_and_parallel_streams_merge(self):
        t = [T0 + timedelta(minutes=m) for m in (0, 2, 4, 30, 31)]
        self.assertAlmostEqual(E.active_hours(t, 5), 5 / 60)
        # a second session in parallel adds no extra hours, only fills gaps
        par = t + [T0 + timedelta(minutes=m) for m in (1, 3)]
        self.assertAlmostEqual(E.active_hours(par, 5), 5 / 60)
        self.assertEqual(E.active_hours([], 5), 0)


class Confidence(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(E.confidence(4, 1.5, 0, False), 'low')
        self.assertEqual(E.confidence(5, 1.5, 0, False), 'medium')
        self.assertEqual(E.confidence(15, 3, 0, False), 'high')
        self.assertEqual(E.confidence(15, 6, 0, False), 'medium')
        self.assertEqual(E.confidence(15, 3, 8, False), 'medium')
        self.assertEqual(E.confidence(6, 3, 0, True), 'low')
        self.assertEqual(E.confidence(30, 12, 0, False), 'low')   # p80 more than 10x p20
        self.assertEqual(E.confidence(30, None, 0, False), 'low')


class Cost(unittest.TestCase):
    P = {'input': 4, 'cache_write_5m': 5, 'cache_write_1h': 8, 'cache_read': 0.4, 'output': 20}

    def test_claude_usage(self):
        u = {'input_tokens': 1_000_000, 'cache_creation_input_tokens': 2_000_000,
             'cache_creation': {'ephemeral_1h_input_tokens': 1_500_000}, 'cache_read_input_tokens': 10_000_000,
             'output_tokens': 100_000}
        self.assertAlmostEqual(E.message_cost(u, self.P), 4 + .5 * 5 + 1.5 * 8 + 10 * .4 + .1 * 20)

    def test_codex_usage_prices_cached_input_at_cache_read(self):
        u = {'input_tokens': 1_000_000, 'cached_input_tokens': 600_000, 'output_tokens': 50_000}
        self.assertAlmostEqual(E.message_cost(u, self.P), .4 * 4 + .6 * .4 + .05 * 20)

    def test_family_prefers_longest_key(self):
        self.assertEqual(E.model_family('claude-opus-5-5', {'opus': 1, 'claude-opus': 2}), 'claude-opus')
        self.assertIsNone(E.model_family('gpt-x', E.DEFAULT_PRICES))


class Calibration(unittest.TestCase):
    def rows(self):
        rs = [row(3, h, wall=3 * (1 + h % 3) * .2, active=.3 * (1 + h % 3), usd=3.0 * (1 + h % 2)) for h in range(1, 7)]
        rs += [row(1, 10 + h, wall=.2, active=.1, usd=1.0, model='m2') for h in range(3)]
        return rs

    def test_bucket_when_n_ge_5_else_pooled_rate(self):
        cal = E.calibrate(self.rows(), T0 + timedelta(days=1), 14)
        self.assertEqual(cal['n'], 9)
        q3 = E.quote(cal, 3)
        self.assertEqual(q3['wall_h']['basis'], '3 SP bucket')
        walls = [r['actual']['wall_h'] for r in self.rows() if r['sp'] == 3]
        self.assertAlmostEqual(q3['wall_h']['lo'], E.percentile(walls, .2))
        q5 = E.quote(cal, 5)
        rates = [r['actual']['wall_h'] / r['sp'] for r in self.rows()]
        self.assertEqual(q5['wall_h']['basis'], 'per-SP rate × SP')
        self.assertAlmostEqual(q5['wall_h']['mid'], 5 * E.percentile(rates, .5))
        # review rounds are not scaled by SP
        self.assertEqual(q5['review_rounds']['mid'], 1)

    def test_groups_per_estimator_and_fallback(self):
        cal = E.calibrate(self.rows(), T0 + timedelta(days=1), 14)
        self.assertEqual(set(cal['groups']), {'claude-code/m1', 'claude-code/m2'})
        self.assertEqual(E.quote(cal, 3, estimator='claude-code/m1')['scope'], 'claude-code/m1')
        self.assertEqual(E.quote(cal, 3, estimator='claude-code/m2')['scope'], E.ALL)  # n=3 < 5
        self.assertEqual(E.quote(cal, 3, estimator='codex/x')['scope'], E.ALL)

    def test_window_excludes_old_rows(self):
        cal = E.calibrate(self.rows(), T0 + timedelta(days=30), 14)
        self.assertEqual(cal['n'], 0)
        self.assertEqual(E.quote(cal, 3)['confidence'], 'none')

    def test_since_clips_the_previous_window_so_drift_does_not_fire_on_ignored_data(self):
        old = [row(2, h, wall=1.0) for h in range(5)]
        new = [row(2, 24 * 5 + h, wall=.2) for h in range(5)]
        cal = E.calibrate(old + new, T0 + timedelta(days=6), 3, since=T0 + timedelta(days=4))
        self.assertEqual(cal['drift'], {}); self.assertEqual(cal['n'], 5)

    def test_drift_flag(self):
        old = [row(2, h, wall=1.0) for h in range(5)]
        new = [row(2, 24 * 5 + h, wall=.2) for h in range(5)]
        cal = E.calibrate(old + new, T0 + timedelta(days=6), 3)
        self.assertEqual(cal['drift']['wall_h']['ratio'], .2)
        self.assertTrue(cal['drift']['wall_h']['flag'])
        self.assertTrue(E.quote(cal, 2)['drift'])

    def test_same_pr_sized_by_two_estimators_counts_once_overall_and_per_group(self):
        a = [row(3, h, wall=1, pr=100 + h, model='m1', source='retro') for h in range(5)]
        b = [row(5, h, wall=1, pr=100 + h, model='m2', source='retro') for h in range(5)]
        cal = E.calibrate(a + b, T0 + timedelta(days=1), 14)
        self.assertEqual((cal['n'], cal['prs']), (5, 5))
        self.assertEqual(sorted(cal['buckets']), ['3'])
        self.assertEqual(cal['per_sp']['wall_h'], cal['groups']['claude-code/m1']['per_sp']['wall_h'])
        self.assertEqual(cal['groups']['claude-code/m1']['n'], 5)
        self.assertEqual(cal['groups']['claude-code/m2']['n'], 5)
        self.assertEqual(cal['throughput']['sp'], 15)

    def test_anchored_vs_unanchored_spread(self):
        rs = [row(2, h, wall=1 + h * .01, closest=1) for h in range(5)] + [row(2, 10 + h, wall=1 + h) for h in range(5)]
        g = E.calibrate(rs, T0 + timedelta(days=1), 14)['groups']['claude-code/m1']
        self.assertEqual(g['anchored']['n'], 5); self.assertEqual(g['unanchored']['n'], 5)
        self.assertLess(g['anchored']['wall_h'], g['unanchored']['wall_h'])


class Throughput(unittest.TestCase):
    def test_daily_sp_from_first_sized_merge_and_coverage(self):
        rs = [row(3, 1, wall=1), row(5, 2, wall=1), row(2, 49, wall=1)]
        end = T0 + timedelta(hours=72)
        merged = [utc for utc in (T0 + timedelta(hours=h) for h in (0.5, 1, 2, 30, 49))]
        tp = E.throughput(rs, T0 - timedelta(days=5), end, merged, tz=timezone.utc)
        self.assertEqual([sp for sp, _ in tp['samples']], [8, 0, 2])  # `end` is midnight: no zero-length day
        # the span starts at the earliest start of a sized PR (0 h), not at its merge
        self.assertAlmostEqual(tp['sp_per_day'], round(10 / 3, 2))
        self.assertEqual(tp['coverage'], round(3 / 5, 3))  # the unsized 0.5 h merge is inside the span

    def test_no_rate_under_one_day(self):
        tp = E.throughput([row(5, 1, wall=.5)], T0 - timedelta(days=5), T0 + timedelta(hours=2), tz=timezone.utc)
        self.assertIsNone(tp['sp_per_day'])


    def test_partial_edge_days_keep_their_sp_with_their_share_of_the_day(self):
        rs = [row(1, 23, wall=1)] + [row(2, 24 * d + 12, wall=1) for d in (1, 2, 3)] + [row(5, 24 * 4 + 1, wall=1)]
        tp = E.throughput(rs, T0 - timedelta(days=5), T0 + timedelta(days=4, hours=2), tz=timezone.utc)
        self.assertEqual([sp for sp, _ in tp['samples']], [1, 2, 2, 2, 5])
        for (_, got), want in zip(tp['samples'], [2 / 24, 1, 1, 1, 2 / 24]): self.assertAlmostEqual(got, want)
        self.assertEqual(tp['full_days'], 3); self.assertEqual(tp['merge_days'], 5)
        self.assertAlmostEqual(sum(length for _, length in tp['samples']), tp['span_days'], places=2)


class Backlog(unittest.TestCase):
    def test_parse_both_forms(self):
        items = E.parse_backlog('# comment\n3 Add export\n5sp: Import CSV\nRewrite sync | 13\n2FA for admins | 3\nFix login 2\n'
                                'Title - 3\n5 | title\nFix it,3\n')
        self.assertEqual([(i['sp'], i['title']) for i in items],
                         [(3, 'Add export'), (5, 'Import CSV'), (13, 'Rewrite sync'), (3, '2FA for admins'), (2, 'Fix login'),
                          (3, 'Title'), (5, 'title'), (3, 'Fix it')])
        with self.assertRaises(ValueError): E.parse_backlog('no points here')

    def test_simulation_is_deterministic_and_ordered(self):
        cal = E.calibrate([row(2, h, wall=.5 + h % 3, active=.2, usd=2) for h in range(10)], T0 + timedelta(days=1), 14,
                          tz=timezone.utc)
        items = [{'sp': 3, 'title': 'a'}, {'sp': 5, 'title': 'b'}, {'sp': 13, 'title': 'c'}]
        a = E.simulate_backlog(items, cal, sims=2000, seed=3); b = E.simulate_backlog(items, cal, sims=2000, seed=3)
        self.assertEqual(a, b)
        self.assertLessEqual(a['wall_h']['lo'], a['wall_h']['mid']); self.assertLessEqual(a['wall_h']['mid'], a['wall_h']['hi'])
        self.assertAlmostEqual(a['usd']['mid'], 21 * 1.0)  # every per-SP $ rate is 1.0
        self.assertEqual([i['title'] for i in a['oversize']], ['c'])
        self.assertGreater(a['days']['lo'], 0)


class Backtest(unittest.TestCase):
    def test_leave_future_out(self):
        rs = [row(2, h * 2, wall=1.0, created=T0 + timedelta(hours=h * 2 - 1)) for h in range(1, 12)]
        res = E.backtest(rs, 14)
        self.assertEqual(res['n'] + res['skipped'], 11)
        self.assertEqual(res['metrics']['wall_h']['interval_hit_rate'], 1.0)
        self.assertEqual(res['metrics']['wall_h']['mdape'], 0)
        # a row logged after the target's estimate time must not shape its quote
        late = [row(2, h * 2, wall=50.0, pr=900 + h, model='m2', created=T0 + timedelta(days=30)) for h in range(1, 12)]
        self.assertEqual(E.backtest(rs + late, 14)['metrics']['wall_h']['mdape'], 0)
        retro = [dict(r, source='retro') for r in rs]
        self.assertEqual(E.backtest(retro, 14)['n'], 0)
        self.assertGreater(E.backtest(retro, 14, include_retro=True)['retro']['n'], 0)


class Attribution(unittest.TestCase):
    def pr(self, n, branch, created_h, merged_h):
        return {'number': n, 'headRefName': branch, 'createdAt': E.iso(T0 + timedelta(hours=created_h)),
                'mergedAt': E.iso(T0 + timedelta(hours=merged_h)), 'commits': [], 'files': [], 'reviews': [],
                'title': 't', 'additions': 1, 'deletions': 1, 'changedFiles': 1}

    def test_branch_window_and_single_pr_link(self):
        prs = [self.pr(1, 'feat/a', 1, 2), self.pr(2, 'feat/a', 5, 6), self.pr(3, 'feat/b', 1, 3)]
        u = {'input_tokens': 1_000_000, 'output_tokens': 0}
        t = lambda h: T0 + timedelta(hours=h)
        sessions = {
            's1': [(t(1.5), 'feat/a', 'claude-code', 'claude-opus-5-5', u, True), (t(1.52), 'feat/a', 'claude-code', None, None, True)],
            's2': [(t(5.5), 'feat/a', 'claude-code', 'claude-sonnet-5-5', u, True)],
            's3': [(t(2.0), 'main', 'claude-code', 'claude-opus-5-5', u, True)],   # linked to PR 3 only
            's4': [(t(2.0), 'main', 'claude-code', 'claude-opus-5-5', u, True)],   # links two PRs: ignored
            'codex:x': [(t(2.5), 'feat/b', 'codex', 'gpt-x', {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 1, 'total_tokens': 11}, True)],
        }
        links = {'s3': {3}, 's4': {1, 3}}
        out = E.attribute(prs, sessions, links, E.DEFAULT_PRICES, 5, now=t(10))
        self.assertAlmostEqual(out[1]['usd'], 4.0)
        self.assertEqual(out[1]['executor'], 'claude-code/claude-opus-5-5')
        self.assertAlmostEqual(out[2]['usd'], 2.0)
        self.assertEqual(out[3]['sessions'], 2)
        self.assertEqual(set(out[3]['agents']), {'claude-code/claude-opus-5-5', 'codex/gpt-x'})
        self.assertIsNone(out[3]['agents']['codex/gpt-x']['usd'])
        self.assertIsNone(out[3]['usd'])
        self.assertEqual(out[3]['known_usd'], 4)
        self.assertEqual(out[3]['unpriced_msgs'], 1)

    def test_reused_branch_is_split_by_pr_windows(self):
        prs = [self.pr(1, 'feat/a', 1, 2), self.pr(2, 'feat/a', 5, 6)]
        t = lambda h: T0 + timedelta(hours=h)
        recs = {'s': [(t(h), 'feat/a', 'claude-code', 'm', None, True) for h in (1.5, 1.55, 5.5, 5.55)]}
        out = E.attribute(prs, recs, {}, E.DEFAULT_PRICES, 5, now=t(10))
        self.assertAlmostEqual(out[1]['active_h'], .05); self.assertAlmostEqual(out[2]['active_h'], .05)
        only_new = E.attribute(prs[1:], recs, {}, E.DEFAULT_PRICES, 5, now=t(10))
        self.assertAlmostEqual(only_new[2]['active_h'], .1)  # no previous PR boundary available

    def test_session_linking_a_merged_and_an_open_pr_is_not_single(self):
        prs = [self.pr(1, 'a', 1, 3)]  # PR 2 on branch b is still open and unknown here
        t = lambda h: T0 + timedelta(hours=h)
        recs = {'s': [(t(1.5), 'a', 'claude-code', 'claude-opus-5-5', None, True),
                      (t(2), 'b', 'claude-code', 'claude-opus-5-5', {'input_tokens': 1_000_000}, True)]}
        out = E.attribute(prs, recs, {'s': {1, 2}}, E.DEFAULT_PRICES, 5, now=t(10))
        self.assertIsNone(out[1]['usd'])  # branch b's work stays off PR 1

    def test_match_pr_by_branch_ignores_older_prs_on_the_same_branch(self):
        merged = [self.pr(1, 'feat/a', 1, 2), self.pr(2, 'feat/a', 100, 101), self.pr(3, 'feat/a', 200, 201)]
        est = {'task_ref': 'feat/a', 'created_at': E.iso(T0 + timedelta(hours=95))}
        self.assertEqual(E.match_pr(est, merged)['number'], 2)
        self.assertIsNone(E.match_pr(dict(est, created_at=E.iso(T0 + timedelta(hours=300))), merged))
        self.assertEqual(E.match_pr({'task_ref': '#1', 'created_at': E.iso(T0)}, merged)['number'], 1)

    def test_pr_actual_starts_at_earliest_signal(self):
        p = self.pr(9, 'x', 2, 4)
        p['commits'] = [{'authoredDate': E.iso(T0 + timedelta(hours=1))}, {'authoredDate': E.iso(T0 - timedelta(days=30))}]
        a = E.pr_actual(p, {'first_ts': T0 + timedelta(hours=1.5), 'active_h': .5, 'usd': 1.0, 'sessions': 1,
                            'executor': 'k', 'agents': {}})
        self.assertEqual(a['start_at'], E.iso(T0 - timedelta(days=30)))
        self.assertEqual((a['wall_h'], a['unobserved_h']), (724, 723.5))
        self.assertIsNone(a['wait_h'])
        self.assertEqual(a['fix_commits'], 0)


class LogReaders(unittest.TestCase):
    def test_claude_reader_dedupes_and_reads_links(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d) / '-Users-x-work-myrepo-feat'; (proj / 's1' / 'subagents').mkdir(parents=True)
            msg: dict = {'type': 'assistant', 'timestamp': '2026-10-01T01:00:00Z', 'sessionId': 's1', 'gitBranch': 'feat/a',
                   'requestId': 'r1', 'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 5}}}
            lines = [msg, msg, {'type': 'user', 'timestamp': '2026-10-01T01:01:00Z', 'sessionId': 's1', 'gitBranch': 'feat/a'},
                     {'type': 'pr-link', 'sessionId': 's1', 'prNumber': 7, 'prRepository': 'Me/MyRepo'},
                     {'type': 'pr-link', 'sessionId': 's1', 'prNumber': 8, 'prRepository': 'other/repo'}]
            (proj / 's1.jsonl').write_text('\n'.join(json.dumps(x) for x in lines) + '\nnot json\n')
            sub = {**msg, 'isSidechain': True, 'requestId': 'r2', 'message': {**msg['message'], 'id': 'm2'}}
            (proj / 's1' / 'subagents' / 'a.jsonl').write_text(json.dumps(sub) + '\n')
            (Path(d) / '-Users-x-other').mkdir(); (Path(d) / '-Users-x-other' / 'z.jsonl').write_text(json.dumps(msg) + '\n')
            sessions, links = E.scan_claude_logs(d, 'myrepo', 'me/myrepo')
            recs = [r for recs in sessions.values() for r in recs]
            self.assertEqual(len(recs), 4)
            self.assertEqual(sum(1 for r in recs if r[4]), 2)  # duplicate line counted once
            self.assertEqual([r[5] for r in recs], [True, True, True, False])
            self.assertEqual(links['s1'], {7})

    def test_subagent_on_another_model_does_not_relabel_the_parent(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d) / '-x-myrepo'; (proj / 's1' / 'subagents').mkdir(parents=True)
            base = {'sessionId': 's1', 'gitBranch': 'b'}
            main = [dict(base, type='user', timestamp='2026-10-01T00:59:00Z'),
                    dict(base, type='assistant', timestamp='2026-10-01T01:00:00Z', requestId='r1',
                         message={'id': 'm1', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 1}}),
                    dict(base, type='user', timestamp='2026-10-01T01:01:00Z'),
                    dict(base, type='assistant', timestamp='2026-10-01T01:01:30Z', message={'model': '<synthetic>'})]
            sub = [dict(base, type='assistant', isSidechain=True, timestamp='2026-10-01T01:00:30Z', requestId='r2',
                        message={'id': 'm2', 'model': 'claude-haiku-5-5', 'usage': {'input_tokens': 1}}),
                   {'type': 'user', 'sessionId': 's1', 'gitBranch': 'b', 'timestamp': '2026-10-01T01:00:40Z',
                    'isSidechain': True, 'message': {'content': 'about "pr-link" records'}}]
            (proj / 's1.jsonl').write_text('\n'.join(json.dumps(x) for x in main))
            (proj / 's1' / 'subagents' / 'a.jsonl').write_text('\n'.join(json.dumps(x) for x in sub))
            sessions, _ = E.scan_claude_logs(d, 'myrepo', 'me/myrepo', project_dirs=[proj])
            models = [(r[3], r[5]) for r in sorted(r for recs in sessions.values() for r in recs)]
            self.assertEqual(models, [('claude-opus-5-5', True), ('claude-opus-5-5', True), ('claude-haiku-5-5', False),
                                      ('claude-haiku-5-5', False), ('claude-opus-5-5', True), ('claude-opus-5-5', True)])

    def test_codex_reader_filters_by_remote(self):
        with tempfile.TemporaryDirectory() as d:
            meta = lambda url: {'type': 'session_meta', 'timestamp': '2026-10-01T00:00:00Z',
                                'payload': {'id': 'c1', 'git': {'branch': 'feat/a', 'repository_url': url}}}
            body = [{'type': 'turn_context', 'timestamp': '2026-10-01T00:00:01Z', 'payload': {'model': 'gpt-x'}},
                    {'type': 'token_usage_record', 'timestamp': '2026-10-01T00:00:02Z', 'payload': {'usage': {'total_tokens': 9}}}]
            (Path(d) / 'a.jsonl').write_text('\n'.join(json.dumps(x) for x in [meta('https://github.com/me/myrepo.git'), *body]))
            (Path(d) / 'b.jsonl').write_text('\n'.join(json.dumps(x) for x in [meta('git@github.com:me/other.git'), *body]))
            s = E.scan_codex_logs(d, 'me/myrepo')
            self.assertEqual(list(s), ['codex:c1'])
            self.assertEqual(s['codex:c1'][-1][3:5], ('gpt-x', {'total_tokens': 9}))

    def test_codex_reader_falls_back_to_last_token_usage(self):
        with tempfile.TemporaryDirectory() as d:
            lines = [{'type': 'session_meta', 'timestamp': '2026-10-01T00:00:00Z',
                      'payload': {'id': 'c2', 'git': {'branch': 'b', 'repository_url': 'https://github.com/me/myrepo'}}},
                     {'type': 'turn_context', 'timestamp': '2026-10-01T00:00:01Z', 'payload': {'model': 'gpt-x'}},
                     {'type': 'event_msg', 'timestamp': '2026-10-01T00:00:02Z', 'payload': {'type': 'token_count', 'info': {
                         'total_token_usage': {'input_tokens': 900, 'total_tokens': 950},
                         'last_token_usage': {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 5, 'total_tokens': 105}}}}]
            (Path(d) / 'r.jsonl').write_text('\n'.join(json.dumps(x) for x in lines))
            (Path(d) / 'r.jsonl').write_text('\n'.join(json.dumps(x) for x in lines + [dict(lines[-1], timestamp='2026-10-01T00:00:03Z')]))
            used = [r[4] for r in E.scan_codex_logs(d, 'me/myrepo')['codex:c2'] if r[4]]  # the re-emit is dropped
            self.assertEqual(used, [{'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 5, 'total_tokens': 105}])


class LedgerAndDashboard(unittest.TestCase):
    def test_fold_actual_and_superseded(self):
        with tempfile.TemporaryDirectory() as d:
            st = E.Store(Path(d) / '.estimate')
            a = E.new_estimate(3, '#5', 'agent', T0, 'claude-code', 'm', 4)
            b = E.new_estimate(5, '#5', 'agent', T0, 'claude-code', 'm')
            st.append([a, b, {'kind': 'superseded', 'id': a['id']}, {'kind': 'actual', 'id': b['id'], 'pr': 5, 'wall_h': 1}])
            rows = {r['id']: r for r in st.rows()}
            self.assertTrue(rows[a['id']]['superseded']); self.assertEqual(rows[b['id']]['actual']['pr'], 5)
            self.assertEqual(len(E.settled(st.rows())), 1)
            self.assertEqual((st.dir / '.gitignore').read_text().splitlines()[-1], '*')
            with st.ledger.open('a') as fh: fh.write('{"kind": "estim')  # torn last line
            self.assertEqual(len(st.rows()), 2)

    def test_dashboard_escapes_and_makes_no_requests(self):
        rs = [row(2, h, wall=.5 + h % 3, active=.2, usd=2) for h in range(8)]
        for r in rs: r['actual']['title'] = '<script>alert(1)</script>'
        cal = E.calibrate(rs, T0 + timedelta(days=1), 14)
        page = E.dashboard_html('me/<b>r</b>', rs, cal, [{'sp': 3, 'title': '<b>x</b>'}], T0 + timedelta(days=1),
                                {'label': '<script>s</script>', 'updated': None}, [{'id': 'u', 'title': '<i>u</i>'}])
        self.assertNotIn('<script>', page); self.assertNotIn('<b>x', page); self.assertNotIn('<b>r', page); self.assertNotIn('<i>', page)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', page); self.assertIn('&lt;script&gt;s', page)
        self.assertIsNone(re.search(r'(src|href)\s*=', page))
        self.assertEqual(re.findall(r'https?://[^\s"\']+', page), [])
        self.assertIn("default-src 'none'", page)


if __name__ == '__main__':
    unittest.main()
