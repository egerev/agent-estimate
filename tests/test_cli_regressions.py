"""CLI and persistence regressions: fixtures only, never real agent logs/network."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def pr(n=1, created=1, merged=2):
    return {'number': n, 'title': 'fixture', 'body': '', 'headRefName': 'feat/task',
            'createdAt': E.iso(T + timedelta(hours=created)),
            'mergedAt': E.iso(T + timedelta(hours=merged)), 'commits': [], 'reviews': [], 'files': []}


def actual(p, usd=2):
    return {**E.pr_actual(p, None), 'usd': usd, 'known_usd': usd, 'cost_complete': True,
            'unpriced_msgs': 0, 'usage_by_model': {}, 'measurement_version': E.__version__}


def save_estimate(store, p, model='m', usd=2):
    r = E.new_estimate(1, f"#{p['number']}", 'agent', T, 'test', model)
    store.append([r, {'kind': 'actual', 'id': r['id'], **actual(p, usd)}])
    return r


class ActualRegression(unittest.TestCase):
    def test_old_commit_is_kept_and_start_source_is_identified(self):
        p = pr(created=240, merged=264)
        p['commits'] = [{'authoredDate': E.iso(T)}]
        a = E.pr_actual(p, None)
        self.assertEqual(a['wall_h'], 264)
        self.assertEqual(a['start_source'], 'commit')

    def test_partial_cost_details_survive_settlement_and_wait_is_unknown(self):
        p = pr()
        agent = {'first_ts': T, 'active_h': .5, 'usd': None, 'known_usd': 4,
                 'unpriced_msgs': 1, 'missing_usage_msgs': 2, 'cost_complete': False,
                 'pricing_basis': 'fixture custom prices',
                 'usage_by_model': {'gpt-unknown': [{'input_tokens': 100}]},
                 'sessions': 1, 'executor': 'test/m', 'agents': {}}
        a = E.pr_actual(p, agent)
        self.assertIsNone(a['usd'])
        self.assertEqual(a['known_usd'], 4)
        self.assertFalse(a['cost_complete'])
        self.assertEqual(a['unpriced_msgs'], 1)
        self.assertEqual(a['missing_usage_msgs'], 2)
        self.assertEqual(a['pricing_basis'], 'fixture custom prices')
        self.assertEqual(a['usage_by_model'], agent['usage_by_model'])
        self.assertIsNone(a['wait_h'])
        self.assertEqual(a['unobserved_h'], 1.5)
        self.assertEqual(a['active_basis'], 'log-gap heuristic')

    def test_logged_estimate_is_an_available_work_start_signal(self):
        p = pr(created=240, merged=264); p['estimateStartAt'] = E.iso(T)
        a = E.pr_actual(p, None)
        self.assertEqual((a['wall_h'], a['start_source']), (264, 'estimate'))


class CLIRegression(unittest.TestCase):
    def run_cli(self, store, *command, now=None, window=14):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            E.main(['--repo', 'owner/repo', '--dir', str(store.dir), '--now', E.iso(now or T + timedelta(hours=4)),
                    '--window', str(window), *command])
        return out.getvalue()

    def test_forecast_rebuilds_stale_cache_and_honors_current_window(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d)
            for i in range(15): save_estimate(store, pr(n=i + 1, created=1, merged=2))
            E.recalibrate(store, T + timedelta(days=1), 14)
            q = json.loads(self.run_cli(store, 'forecast', '--sp', '1', '--json',
                                        now=T + timedelta(days=40), window=1))
            self.assertEqual(q['n'], 0)
            self.assertEqual(q['window_days'], 1)
            self.assertEqual(q['confidence'], 'none')

    def test_refresh_updates_all_estimators_preserving_estimate_events(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr()
            originals = [save_estimate(store, p, model=m, usd=1) for m in ('a', 'b')]
            with patch.object(E, 'fetch_prs', return_value=[p]), \
                    patch.object(E, 'measure', return_value={1: actual(p, 7)}) as measure:
                self.run_cli(store, 'record', '--refresh')
            self.assertEqual(measure.call_count, 1)
            rows = store.rows()
            self.assertEqual(len(rows), 2)
            for before, after in zip(originals, rows):
                self.assertEqual(after['created_at'], before['created_at'])
                self.assertEqual(after['source'], before['source'])
                self.assertEqual(after['actual']['usd'], 7)

    def test_refresh_fetches_tracked_pr_outside_history_page(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr(n=2); save_estimate(store, p)
            with patch.object(E, 'fetch_prs', return_value=[]), \
                    patch.object(E, 'fetch_pr', return_value=p) as fetch, \
                    patch.object(E, 'measure', return_value={2: actual(p, 9)}):
                self.run_cli(store, 'record', '--refresh')
            fetch.assert_called_once_with('owner/repo', 2)
            self.assertEqual(store.rows()[0]['actual']['usd'], 9)

    def test_body_sp_is_observed_now_and_never_backdated(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr(); p['body'] = 'SP: 3'
            now = T + timedelta(hours=4)
            with patch.object(E, 'fetch_prs', return_value=[p]), \
                    patch.object(E, 'measure', return_value={1: actual(p)}):
                self.run_cli(store, 'record', now=now)
            r = store.rows()[0]
            self.assertEqual(r['created_at'], E.iso(now))
            self.assertEqual(r['timing'], 'unknown')

    def test_history_preserves_commit_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr()
            p['commits'] = [{'authoredDate': E.iso(T - timedelta(days=10))}]
            E.save_history(store, [p])
            self.assertEqual(store.read_json('history.json')[0]['commits'], p['commits'])

    def test_backtest_honors_saved_process_cutoff(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); since = T + timedelta(hours=1)
            store.write_json('config.json', {'since': E.iso(since)})
            with patch.object(E, 'backtest', return_value={}) as backtest:
                self.run_cli(store, 'backtest')
            self.assertEqual(backtest.call_args.kwargs['since'], since)

    def test_future_process_cutoff_produces_empty_nonnegative_window(self):
        since = T + timedelta(days=1)
        cal = E.calibrate([], T, since=since)
        self.assertEqual(cal['n'], 0)
        self.assertEqual(cal['window']['days'], 0)
        self.assertEqual(cal['window']['start'], cal['window']['end'])
        self.assertEqual(cal['window']['since'], E.iso(since))

    def test_non_refresh_record_does_not_remeasure_settled_estimates(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr(); save_estimate(store, p)
            with patch.object(E, 'fetch_prs', return_value=[p]), patch.object(E, 'measure') as measure:
                self.run_cli(store, 'record')
            measure.assert_not_called()

    def test_pending_pr_number_outside_history_page_is_settled(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr(n=22)
            store.append([E.new_estimate(1, '#22', 'agent', T, 'test', 'm')])
            with patch.object(E, 'fetch_prs', return_value=[]), patch.object(E, 'fetch_pr', return_value=p), \
                    patch.object(E, 'measure', return_value={22: actual(p)}):
                self.run_cli(store, 'record')
            self.assertEqual(store.rows()[0]['actual']['pr'], 22)

    def test_record_measures_early_branch_work_with_verified_fixture_streams(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr(created=240, merged=264)
            p['commits'] = [{'authoredDate': E.iso(T + timedelta(hours=239))}]
            store.append([E.new_estimate(1, '#1', 'agent', T, 'test', 'm')])
            usage = {'input_tokens': 1_000_000}
            stream = {'s': [(T + timedelta(minutes=m), 'feat/task', 'claude-code',
                             'claude-opus-5-5', usage if m == 1 else None, True) for m in (1, 3)]}
            with patch.object(E, 'fetch_prs', return_value=[p]), \
                    patch.object(E, 'scan_claude_logs', return_value=(stream, {})), \
                    patch.object(E, 'scan_codex_logs', return_value={}):
                self.run_cli(store, 'record', now=T + timedelta(hours=265))
            a = store.rows()[0]['actual']
            self.assertEqual(a['usd'], 4)
            self.assertEqual(a['start_source'], 'estimate')
            self.assertEqual(a['wall_h'], 264)
            self.assertAlmostEqual(a['active_h'], round(2 / 60, 4))

    def test_record_reused_branch_ignores_commits_from_before_previous_merge(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d)
            previous = pr(n=1, created=1, merged=2)
            current = pr(n=2, created=11, merged=12); current['body'] = 'SP: 1'
            current['commits'] = [{'authoredDate': E.iso(T + timedelta(hours=h))} for h in (0, 10)]
            E.save_history(store, [previous])
            stream = {'s': [(T + timedelta(hours=10), 'feat/task', 'claude-code',
                             'claude-opus-5-5', {'input_tokens': 1}, True)]}
            with patch.object(E, 'fetch_prs', return_value=[current]), \
                    patch.object(E, 'scan_claude_logs', return_value=(stream, {})), \
                    patch.object(E, 'scan_codex_logs', return_value={}):
                self.run_cli(store, 'record', now=T + timedelta(hours=13))
            a = store.rows()[0]['actual']
            self.assertEqual(a['start_at'], E.iso(T + timedelta(hours=10)))
            self.assertEqual(a['wall_h'], 2)

    def test_refresh_keeps_start_evidence_of_superseded_estimate(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); p = pr(created=10, merged=12)
            store.append([E.new_estimate(1, '#1', 'agent', T + timedelta(hours=h), 'test', 'm') for h in (0, 4)])
            with patch.object(E, 'fetch_prs', side_effect=lambda *a: [dict(p)]), \
                    patch.object(E, 'scan_claude_logs', return_value=({}, {})), \
                    patch.object(E, 'scan_codex_logs', return_value={}):
                self.run_cli(store, 'record', now=T + timedelta(hours=13))
                first = E.settled(store.rows())[0]['actual']
                self.run_cli(store, 'record', '--refresh', now=T + timedelta(hours=14))
            refreshed = E.settled(store.rows())[0]['actual']
            self.assertTrue(refreshed['refresh'])
            self.assertEqual((first['wall_h'], first['start_at']), (12, E.iso(T)))
            self.assertEqual((refreshed['wall_h'], refreshed['start_at']), (12, E.iso(T)))


if __name__ == '__main__':
    unittest.main()
