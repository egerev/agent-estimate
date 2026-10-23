"""Teammate dashboard number sources: burn from local agent logs, the planner, agent lanes, rates. Synthetic data only."""
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import estimate as E  # noqa: E402
from test_estimate import T0, row  # noqa: E402

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)   # a Thursday, 18:00 UTC
SECRET = 'SECRET-PROMPT-TEXT'


def at(hours_ago):
    return E.iso(NOW - timedelta(hours=hours_ago))


def claude_line(ts, msg_id, req, model='claude-opus-5-5', out=100, session='s1', kind='assistant', **extra):
    usage = {'input_tokens': 10, 'output_tokens': out, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 0}
    return json.dumps({'type': kind, 'sessionId': session, 'requestId': req, 'timestamp': ts, **extra,
                       'message': {'id': msg_id, 'model': model, 'usage': usage, 'content': [{'type': 'text', 'text': SECRET}]}})


def codex_lines(model, events):
    """events: (ts, cumulative total, last) -> rollout lines; `None` last means `info: null`."""
    out = [json.dumps({'type': 'session_meta', 'payload': {'id': 'c1', 'instructions': SECRET}}),
           json.dumps({'type': 'turn_context', 'timestamp': events[0][0], 'payload': {'model': model}})]
    for ts, total, last in events:
        info = None if last is None else {'total_token_usage': {'total_tokens': total}, 'last_token_usage':
                                          {'input_tokens': last, 'cached_input_tokens': last // 2, 'output_tokens': 7,
                                           'total_tokens': last + 7}}
        out.append(json.dumps({'type': 'event_msg', 'timestamp': ts, 'payload': {'type': 'token_count', 'info': info}}))
    return out


class Burn(unittest.TestCase):
    def logs(self, d):
        claude = Path(d) / 'claude' / 'proj'; claude.mkdir(parents=True)
        (claude / 'a.jsonl').write_text('\n'.join([
            claude_line(at(1), 'm1', 'r1', out=40),                       # streamed snapshot of m1
            claude_line(at(1), 'm1', 'r1', out=100),                      # final snapshot: the largest wins
            claude_line(at(.95), 'm2', 'r2', model='<synthetic>'),        # never billed
            claude_line(at(30), 'm3', 'r3'),                              # yesterday: week only
            claude_line(at(24 * 9), 'm4', 'r4'),                          # older than the week
            claude_line(at(.9), 'u1', 'r5', kind='user'),                 # a user line mentioning usage
            'not json "usage"']))
        (claude / 'b.jsonl').write_text(claude_line(at(1), 'm1', 'r1', out=100, session='s2'))  # a resumed copy of m1
        codex = Path(d) / 'codex' / '2026'; codex.mkdir(parents=True)
        events = [(at(2), 100, 100), (at(1.98), 100, 100), (at(1.96), None, None), (at(1.95), 260, 160)]
        (codex / 'x.jsonl').write_text('\n'.join(codex_lines('gpt-6.1-sol', events)))
        (codex / 'fork.jsonl').write_text('\n'.join(codex_lines('gpt-6.1-sol', events[:1])))     # a fork copies history
        (codex / 'y.jsonl').write_text('\n'.join(codex_lines('gpt-9-unknown', [(at(3), 50, 50)])))
        return Path(d) / 'claude', Path(d) / 'codex'

    def test_each_response_counts_once_and_is_priced(self):
        with tempfile.TemporaryDirectory() as d:
            recs = E.scan_burn(*self.logs(d), NOW - timedelta(days=7), E.DEFAULT_PRICES)
        claude = [r for r in recs if r[1] == 'claude-code']; codex = [r for r in recs if r[1] == 'codex']
        self.assertEqual([r[3] for r in claude], [1110, 1110])                 # m3 yesterday, m1 once at its final size
        usage = {'input_tokens': 10, 'output_tokens': 100, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 0}
        self.assertAlmostEqual(claude[1][4], E.message_cost(usage, E.DEFAULT_PRICES['claude-opus-5-5']))
        self.assertEqual(sorted(r[3] for r in codex), [57, 107, 167])         # repeated totals, null info and the fork dropped
        sol = [r for r in codex if r[2] == 'gpt-6.1-sol']
        self.assertTrue(all(r[4] is not None for r in sol))
        self.assertEqual([r[4] for r in codex if r[2] == 'gpt-9-unknown'], [None])   # unknown model: unpriced, never guessed

    def test_report_splits_today_and_week_and_sums_parallel_agents(self):
        with tempfile.TemporaryDirectory() as d:
            recs = E.scan_burn(*self.logs(d), NOW - timedelta(days=7), E.DEFAULT_PRICES)
        rep = E.burn_report(recs, NOW, timezone.utc)
        today, week = rep['today'], rep['week']
        self.assertEqual((today['responses'], week['responses']), (4, 5))
        self.assertEqual(today['agents']['claude-code']['tokens'], 1110)
        self.assertEqual(today['agents']['codex']['unpriced_tokens'], 57)
        self.assertEqual(today['unpriced_tokens'], 57)
        self.assertAlmostEqual(week['usd'] - today['usd'], claude_cost := week['agents']['claude-code']['usd'] / 2)
        self.assertGreater(claude_cost, 0)
        stamps = [NOW - timedelta(minutes=m) for m in (30, 27, 24)]
        parallel = [(t + timedelta(seconds=k), 'claude-code', 'm', 1, 0.0, s) for t in stamps for k, s in enumerate('ab')]
        self.assertAlmostEqual(E.burn_report(parallel, NOW, timezone.utc)['today']['agent_h'], 2 * 6 / 60)

    def test_rows_name_unpriced_tokens_and_missing_logs(self):
        with tempfile.TemporaryDirectory() as d:
            rep = E.burn_report(E.scan_burn(*self.logs(d), NOW - timedelta(days=7), E.DEFAULT_PRICES), NOW, timezone.utc)
        rows = dict((k, (v, n)) for k, v, n in E.burn_rows(rep))
        self.assertIn('Codex partly unpriced', rows['Today'][1]); self.assertIn('1 k Claude', rows['Today'][1])
        self.assertIn('opus-5-5 $', rows['7 days'][1]); self.assertIn('gpt-9-unknown 57 unpriced', rows['7 days'][1])
        self.assertIn('no local agent logs found', E.burn_rows(None)[0][1])
        self.assertIn('could not be read', E.burn_rows({'error': True})[0][1])
        empty = E.burn_report([], NOW, timezone.utc)
        self.assertIn('no agent responses', dict((k, v) for k, v, _ in E.burn_rows(empty))['Today'])

    def test_page_never_contains_message_text(self):
        with tempfile.TemporaryDirectory() as d:
            rep = E.burn_report(E.scan_burn(*self.logs(d), NOW - timedelta(days=7), E.DEFAULT_PRICES), NOW, timezone.utc)
        page = E.dashboard_html('me/r', [], None, None, NOW, burn=rep, tz=timezone.utc)
        self.assertNotIn(SECRET, page); self.assertNotIn(SECRET, repr(rep))
        self.assertIn('<div>Today</div><div><b>$', page)

    def test_missing_directories_are_empty(self):
        self.assertEqual(E.scan_burn('/nonexistent/a', '/nonexistent/b', NOW - timedelta(days=7), E.DEFAULT_PRICES), [])


class DashboardCommand(unittest.TestCase):
    def run_dashboard(self, *extra, fail=None):
        with tempfile.TemporaryDirectory() as d:
            backlog = Path(d) / 'b.md'; backlog.write_text('| id | title | effort | value |\n|---|---|---|---|\n| 1 | a | 3 | 5 |\n')
            out = Path(d) / 'dash.html'; err = io.StringIO()
            with mock.patch.object(E, 'scan_burn', return_value=[], side_effect=fail) as scanned, mock.patch.dict(os.environ, {'ESTIMATE_WINDOWS_FILE': f'{d}/w.jsonl', 'ESTIMATE_RATE_HISTORY': f'{d}/r.jsonl'}), contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                E.main(['--repo', 'o/r', '--dir', d, '--now', E.iso(NOW), '--claude-dir', d, '--codex-dir', d,
                        'dashboard', '--backlog', str(backlog), '--out', str(out), *extra])
            return out.read_text(), scanned, err.getvalue()

    def test_unreadable_logs_never_stop_the_dashboard(self):
        page, _, err = self.run_dashboard(fail=UnicodeDecodeError('utf-8', b'x', 0, 1, 'bad'))
        self.assertIn('agent logs could not be read', page); self.assertIn('3<small>SP in 1 card', page)
        self.assertIn('agent logs not read for the burn rows: UnicodeDecodeError', err)

    def test_burn_reads_the_last_week_and_can_be_skipped(self):
        page, scanned, _ = self.run_dashboard()
        self.assertEqual(scanned.call_args[0][2], NOW - timedelta(days=E.BURN_DAYS))
        self.assertIn('no agent responses', page)
        page, scanned, _ = self.run_dashboard('--no-burn')
        scanned.assert_not_called(); self.assertIn('no local agent logs found', page)


class Planner(unittest.TestCase):
    RATES = {'wall': {'lo': .25, 'mid': .5, 'hi': 2.0}, 'sp_day': {'lo': 20.0, 'mid': 10.0, 'hi': 4.0},
             'active': {'mid': .1}, 'usd': {'mid': 3.0}, 'tok': {'mid': 2e6}}

    def test_boxes_follow_the_local_clock(self):
        boxes = E.time_boxes(NOW, timezone.utc)
        self.assertEqual([b[:3] for b in boxes], [('t0', 'Today', '6 h left'), ('t1', '1 day', 'until Fri'), ('t2', '2 days', 'until Sat'),
                                                  ('t3', '3 days', 'until Sun'), ('t7', 'Week', 'until 15 Oct')])
        self.assertEqual(E.time_boxes(NOW.replace(hour=23, minute=40), timezone.utc)[0][2], '1 h left')     # never zero
        self.assertEqual(E.time_boxes(NOW, timezone(timedelta(hours=8)))[0][2], '22 h left')               # 02:00 local

    def test_capacity_hours_over_wall_rate_and_days_times_pace(self):
        today, day = ('t0', 'Today', '', 'h', 4), ('t2', '2 days', '', 'd', 2)
        self.assertEqual([E.box_capacity(today, t, self.RATES) for t in ('sure', 'likely', 'maybe')], [2, 8, 16])
        self.assertEqual([E.box_capacity(day, t, self.RATES) for t in ('sure', 'likely', 'maybe')], [8, 20, 40])

    def test_schedule_never_splits_a_card_and_starts_after_its_dependencies(self):
        cards = [{'id': 'a', 'sp': 3}, {'id': 'b', 'sp': 5}, {'id': 'c', 'sp': 1, 'depends': ['a']}, {'id': 'd', 'sp': 2, 'depends': ['b']}]
        plan = E.schedule(cards, 2)
        self.assertEqual(plan, [(0, 0, 3), (1, 0, 5), (0, 3, 4), (1, 5, 7)])
        self.assertEqual(E.done_cards(plan, 4.5), 1); self.assertEqual(E.done_cards(plan, 2), 0)   # b ends at 5: c waits in the count
        self.assertEqual(E.done_cards(plan, 6), 3); self.assertEqual(E.done_cards(plan, 7), 4)

    def test_a_dependency_chain_does_not_speed_up_with_more_agents(self):
        chain = [{'id': str(k), 'sp': 3, 'depends': [str(k - 1)] if k else []} for k in range(4)]
        day = ('t1', '1 day', '', 'd', 1)
        for agents in (1, 5):
            s = E.plan_state(chain, day, agents, self.RATES, {})
            self.assertEqual((s['sure']['cards'], s['likely']['cards'], s['maybe']['cards']), (1, 3, 4))
            self.assertIsNone(s['need']); self.assertFalse(s['all'])

    def test_a_card_waiting_on_a_card_placed_after_it_is_never_planned(self):
        cards = [{'id': 'b', 'sp': 1, 'depends': ['a']}, {'id': 'a', 'sp': 1, 'depends': ['gone']}, {'id': 'c', 'sp': 1}]
        plan = E.schedule(cards, 2)
        self.assertEqual(plan[0][2], math.inf); self.assertEqual(E.done_cards(plan, 100), 0)

    def test_need_stays_fast_on_a_long_chain(self):
        chain = [{'id': str(k), 'sp': 1, 'depends': [str(k - 1)] if k else []} for k in range(500)]
        loose = [{'id': str(k), 'sp': 1} for k in range(500)]
        started = time.monotonic()
        self.assertIsNone(E.plan_state(chain, ('t7', 'Week', '', 'd', 7), 5, self.RATES, {})['need'])
        self.assertEqual(E.plan_state(loose, ('t7', 'Week', '', 'd', 7), 5, self.RATES, {})['need'], 8)   # 500 SP ÷ 70 SP each
        self.assertLess(time.monotonic() - started, 2)

    def test_a_card_longer_than_the_box_is_never_counted(self):
        big = [{'id': '1', 'sp': 8}, {'id': '2', 'sp': 1}]
        s = E.plan_state(big, ('t0', 'Today', '', 'h', 2), 2, self.RATES, {})     # one agent: 4 SP likely, 8 SP maybe
        self.assertEqual((s['likely']['cards'], s['maybe']['cards'], s['need']), (0, 2, None))

    def test_state_prices_the_likely_sp_and_reads_the_table_backwards(self):
        cards = [{'id': str(k), 'sp': 2} for k in range(20)]
        s = E.plan_state(cards, ('t0', 'Today', '', 'h', 4), 3, self.RATES, {})
        self.assertEqual((s['sure'], s['likely'], s['maybe']), ({'cards': 3, 'sp': 6}, {'cards': 12, 'sp': 24}, {'cards': 20, 'sp': 40}))
        self.assertAlmostEqual(float(s['hours']), 2.4); self.assertAlmostEqual(float(s['usd']), 72)
        self.assertAlmostEqual(float(s['tok']), 48e6)
        self.assertEqual(s['need'], math.ceil(40 / 8)); self.assertFalse(s['all'])
        self.assertEqual(E.tier_of(2, s), 'sure'); self.assertEqual(E.tier_of(3, s), 'likely'); self.assertIsNone(E.tier_of(20, s))
        thin = E.plan_state(cards, ('t1', '1 day', '', 'd', 1), 5, {**self.RATES, 'usd': None}, {})
        self.assertIsNone(thin['usd']); self.assertTrue(thin['all'])


class TokensPerSp(unittest.TestCase):
    def test_logged_usage_of_window_prs_per_sp(self):
        u = {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': 850, 'cache_creation_input_tokens': 0}
        rows = [row(2, 10 * h, wall=1, pr=h) for h in range(1, 4)]
        rows[0]['actual']['usage_by_model'] = {'m1': [u, u]}; rows[1]['actual']['usage_by_model'] = {'m1': [u], 'gpt': [u]}
        old = row(2, -24 * 40, wall=1, pr=9); old['actual']['usage_by_model'] = {'m1': [u]}
        cal = E.calibrate(rows + [old], T0 + timedelta(days=2), 14)
        self.assertEqual(E.tokens_per_sp(rows + [old], cal), [1000.0, 1000.0])   # no usage and out of window are left out
        self.assertEqual(E.tokens_per_sp(rows, None), [])


class CodexBurn(unittest.TestCase):
    @staticmethod
    def event(kind, ts, payload):
        return json.dumps({'type': kind, 'timestamp': ts, 'payload': payload})

    def scan(self, files):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'codex'; root.mkdir()
            for name, lines in files.items():
                (root / name).write_text('\n'.join([self.event('session_meta', lines[0][1], {'id': name}),
                                                    self.event('turn_context', lines[0][1], {'model': 'gpt-6.1-sol'})]
                                                   + [self.event(*x) for x in lines]))
            return E.scan_burn(Path(d) / 'none', root, NOW - timedelta(days=7), E.DEFAULT_PRICES)

    def test_reports_without_a_cumulative_total_each_count(self):
        count = lambda ts, out: ('event_msg', ts, {'type': 'token_count', 'info': {'last_token_usage': {'output_tokens': out}}})
        recs = self.scan({'a.jsonl': [count(at(2), 100), count(at(1.9), 200)]})
        self.assertEqual(sum(r[3] for r in recs), 300)

    def test_usage_records_count_once_per_response_and_their_echo_is_skipped(self):
        u1, u2 = {'input_tokens': 50, 'output_tokens': 10}, {'input_tokens': 70, 'output_tokens': 5}
        rec = lambda ts, rid, u: ('token_usage_record', ts, {'response_id': rid, 'usage': u})
        echo = lambda ts, total, u: ('event_msg', ts, {'type': 'token_count', 'info': {'total_token_usage': {'t': total},
                                                                                       'last_token_usage': u}})
        lines = [rec(at(2), 'r1', u1), echo(at(1.99), 60, u1), rec(at(1.9), 'r2', u2), echo(at(1.89), 135, u2),
                 echo(at(1.8), 200, {'input_tokens': 60, 'output_tokens': 5})]      # a report with no record of its own
        recs = self.scan({'a.jsonl': lines, 'b-fork.jsonl': lines[:2]})          # the fork copies r1 and its echo
        self.assertEqual([r[3] for r in recs], [60, 75, 65])

    def test_a_record_pairs_with_its_report_by_cumulative_total_in_either_order(self):
        u = {'input_tokens': 50, 'output_tokens': 10}
        rec = lambda ts, rid, total: ('token_usage_record', ts, {'response_id': rid, 'usage': u, 'thread_token_usage': {'t': total}})
        cnt = lambda ts, total: ('event_msg', ts, {'type': 'token_count', 'info': {'total_token_usage': {'t': total}, 'last_token_usage': u}})
        report_first = self.scan({'a.jsonl': [cnt(at(2), 60), rec(at(1.99), 'r1', 60)]})
        self.assertEqual([r[3] for r in report_first], [60])
        twins = self.scan({'a.jsonl': [rec(at(2), 'r1', 60), cnt(at(1.99), 60), cnt(at(1.9), 120)]})   # equal usage, new total
        self.assertEqual([r[3] for r in twins], [60, 60])
        subthread = self.scan({'a.jsonl': [rec(at(2), 'r1', 60), cnt(at(1.99), 900)]})   # a thread total, the session's differs
        self.assertEqual([r[3] for r in subthread], [60])
        copied_echo = self.scan({'a.jsonl': [cnt(at(2), 60), rec(at(1.99), 'r1', 60)], 'b-fork.jsonl': [cnt(at(2), 60)]})
        self.assertEqual([r[3] for r in copied_echo], [60])                               # the fork copied only the report
        bare = lambda kind, payload: (kind, at(2), payload)                                # no response id, no total, one instant
        same_instant = self.scan({'a.jsonl': [bare('token_usage_record', {'usage': u}),
                                              bare('event_msg', {'type': 'token_count', 'info': {'last_token_usage': u}})]})
        self.assertEqual([r[3] for r in same_instant], [60])

    def test_parallel_sessions_with_equal_timestamps_both_count(self):
        stamps = [NOW - timedelta(minutes=m) for m in (30, 27, 24)]
        recs = [(t, 'codex', 'm', 1, 0.0, (s,), (s, k)) for k, t in enumerate(stamps) for s in ('a', 'b')]
        self.assertAlmostEqual(E.burn_report(recs, NOW, timezone.utc)['today']['agent_h'], 2 * 6 / 60)

    def test_a_forked_copy_counts_its_tokens_and_hours_once(self):
        count = lambda ts, total: ('event_msg', ts, {'type': 'token_count', 'info': {'total_token_usage': {'t': total},
                                                                                     'last_token_usage': {'output_tokens': 7}}})
        original = [count(at(1), 1), count(at(.97), 2), count(at(.94), 3)]
        alone = E.burn_report(self.scan({'z-original.jsonl': original}), NOW, timezone.utc)['today']
        for fork in ('a-fork.jsonl', 'zz-fork.jsonl'):                  # sorting before or after the original
            recs = self.scan({'z-original.jsonl': original, fork: original[:2] + [count(at(.5), 9)]})
            both = E.burn_report(recs, NOW, timezone.utc)['today']
            self.assertEqual((alone['tokens'], both['tokens']), (21, 28))
            self.assertAlmostEqual(both['agent_h'], alone['agent_h']); self.assertAlmostEqual(alone['agent_h'], .06)  # 2 × 1.8 min
            self.assertEqual(len(recs[0][5]), 2)                          # the copied response is seen in both sessions


if __name__ == '__main__':
    unittest.main()
