"""Honest forecasts: backlog totals, calendar-day basis, and why sample sizes shrink. Synthetic data only."""
import contextlib
import io
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E  # noqa: E402

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(sp, merged_h, wall=1.0, active=None, usd=None, pr=None, source='retro', model='m1', sessions=1, **actual):
    merged = T0 + timedelta(hours=merged_h)
    return {'id': f'e-{pr or merged_h}-{model}', 'sp': sp, 'source': source, 'created_at': E.iso(merged),
            'estimator': {'agent': 'claude-code', 'model': model},
            'actual': {'pr': pr if pr is not None else int(merged_h * 10), 'merged_at': E.iso(merged),
                       'start_at': E.iso(merged - timedelta(hours=wall)), 'wall_h': wall, 'active_h': active,
                       'usd': usd, 'cost_complete': usd is not None, 'sessions': sessions, 'title': 't', **actual}}


def cli(store, *command, now):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        E.main(['--repo', 'owner/repo', '--dir', str(store.dir), '--now', E.iso(now), *command])
    return out.getvalue(), err.getvalue()


class BacklogTotal(unittest.TestCase):
    """Per-item scatter must not cancel out into false precision over a long backlog."""

    def setUp(self):
        rng = random.Random(5)
        # 30 PRs with per-SP rates spread about ×3 (p80/p20), like a consistent real repo.
        self.rows = [row(2, h, active=2 * rng.uniform(.1, .5), usd=2 * rng.uniform(1, 4), pr=h + 1) for h in range(30)]
        self.cal = E.calibrate(self.rows, T0 + timedelta(days=2), 14, tz=timezone.utc)
        self.items = [{'sp': sp} for sp in [3] * 24 + [2] * 9 + [5] * 8 + [8] * 6 + [1] * 4]  # 51 items

    def test_total_is_wider_than_independent_draws_and_narrower_than_summed_item_ranges(self):
        rs = self.cal['per_sp']['active_h']; sps = [i['sp'] for i in self.items]
        rng = random.Random(1)
        indep = [sum(sp * rng.choice(rs) for sp in sps) for _ in range(4000)]
        indep_width = E.percentile(indep, .8) - E.percentile(indep, .2)
        q = E.simulate_backlog(self.items, self.cal, sims=4000)['active_h']
        summed = (sum(sp * E.percentile(rs, .2) for sp in sps), sum(sp * E.percentile(rs, .8) for sp in sps))
        self.assertGreater(q['hi'] - q['lo'], 1.2 * indep_width)
        self.assertGreater(q['lo'], summed[0]); self.assertLess(q['hi'], summed[1])
        self.assertAlmostEqual(q['mid'], E.percentile(indep, .5), delta=.05 * q['mid'])
        self.assertIn('bootstrap of 30 PRs', q['basis'])

    def test_total_is_deterministic_for_a_seed(self):
        a = E.simulate_backlog(self.items, self.cal, sims=500, seed=4)
        self.assertEqual(a, E.simulate_backlog(self.items, self.cal, sims=500, seed=4))
        self.assertNotEqual(a['active_h'], E.simulate_backlog(self.items, self.cal, sims=500, seed=5)['active_h'])

    def test_confidence_follows_item_spread_not_the_narrow_sum(self):
        rows = [row(1, h, active=v, pr=h + 1) for h, v in enumerate([.01, .02, 1, 2, .015, 1.5, .03, 2.5] * 3)]
        cal = E.calibrate(rows, T0 + timedelta(days=2), 14, tz=timezone.utc)
        q = E.simulate_backlog([{'sp': 1}] * 200, cal, sims=500)['active_h']
        self.assertLess(q['hi'] / q['lo'], 10)        # the sum of 200 items is comparatively narrow ...
        self.assertEqual(q['confidence'], 'low')       # ... but items scatter over ×100, so trust stays low


class CalendarDays(unittest.TestCase):
    def steady_repo(self):
        """15 PRs in three bursts over 12.5 days: most SP merged on the partial first and last days."""
        rows = ([row(1, 3 + i * .1, pr=100 + i) for i in range(5)] + [row(8, 3.5, pr=110)]          # day 1: 13 SP
                + [row(2, 24 * 7 + 12 + i, pr=120 + i) for i in range(4)]                          # day 8: 8 SP
                + [row(5, 24 * 12 + 2 + i, pr=130 + i, wall=.5) for i in range(3)]                  # day 13: 15 SP
                + [row(3, 24 * 12 + 5, pr=140, wall=.5), row(3, 24 * 12 + 6, pr=141, wall=.5)])    # +6 SP
        return E.calibrate(rows, T0 + timedelta(days=12, hours=14), 14, tz=timezone.utc)

    def test_days_follow_the_span_pace_including_partial_edge_days(self):
        cal = self.steady_repo(); tp = cal['throughput']
        self.assertEqual(tp['sp'], 42); self.assertEqual(tp['merge_days'], 3)
        d = E.simulate_backlog([{'sp': 1}] * 18 + [{'sp': 2}] * 3 + [{'sp': 5}], cal, sims=2000)['days']
        pace_days = 29 / (tp['sp'] / tp['span_days'])
        self.assertLess(d['lo'], pace_days); self.assertGreater(d['hi'], pace_days)
        self.assertLess(d['mid'], 2 * pace_days)   # dropping the edge days gave ~4.5x the pace here

    def test_basis_names_the_pace_and_warns_when_waits_dominate(self):
        cal = self.steady_repo()
        cal['per_sp']['active_h'] = [.1] * 15
        sim = E.simulate_backlog([{'sp': 1}] * 29, cal, sims=500)
        basis, wait = E.calendar_basis(sim)
        self.assertIn('42 SP merged by 15 PRs', basis); self.assertIn('SP merged on 3 of', basis)
        self.assertIn('bootstrap of 15 PRs', basis)
        self.assertTrue(wait.startswith('Waits dominate'))
        self.assertIn(f"{E._num(sim['active_h']['mid'])} h of observed agent work", wait)

    def test_no_wait_warning_when_hours_fill_the_days(self):
        sim = {'active_h': {'mid': 30, 'basis': 'b'}, 'days': {'mid': 2.5, 'sp_merged': 140, 'prs': 30, 'span_days': 1.7,
                                                                'sp_per_day': 80, 'merge_days': 2, 'n': 2}}
        self.assertIsNone(E.calendar_basis(sim)[1])
        self.assertIsNotNone(E.calendar_basis(sim, concurrency=7)[1])  # one agent at the repo's per-agent pace

    def test_forecast_and_dashboard_print_basis_and_warning(self):
        rows = [row(2, 24 * d + 1, active=.05, pr=d + 1) for d in range(12)]
        with tempfile.TemporaryDirectory() as tmp:
            store = E.Store(tmp)
            store.append([{**{k: v for k, v in r.items() if k != 'actual'}, 'kind': 'estimate'} for r in rows])
            store.append([{**r['actual'], 'kind': 'actual', 'id': r['id']} for r in rows])
            backlog = Path(tmp) / 'b.md'; backlog.write_text('| id | title | effort |\n|---|---|---|\n| 1 | a | 5 |\n')
            out, _ = cli(store, 'forecast', '--backlog', str(backlog), now=T0 + timedelta(days=12))
            self.assertIn('based on: calendar days from merge pace', out)
            self.assertIn('warning: waits dominate', out)


class SampleAudit(unittest.TestCase):
    def test_dropped_prs_are_listed_per_reason(self):
        now = T0 + timedelta(days=30)
        rows = ([row(2, 24 * 30 - h, active=.2, usd=1, pr=10 + h) for h in range(1, 4)]       # in the window
                + [row(2, 24 * 2 + h, pr=50 + h) for h in range(5)]                          # merged long before
                + [row(3, 24 * 30 - 1, pr=11, model='m2')]                                    # same PR, another model
                + [{'id': 'e-p', 'sp': 3, 'task_ref': 'feat/x', 'created_at': E.iso(now), 'source': 'agent',
                    'estimator': {}}])
        a = E.calibrate(rows, now, 14)['audit']
        self.assertEqual((a['sized_prs'], a['used'], a['extra_estimates'], a['pending']), (8, 3, 1, ['feat/x']))
        (why, prs), = a['dropped'].items()
        self.assertIn('before the 14-day window', why); self.assertEqual(prs, [50, 51, 52, 53, 54])
        text = '\n'.join(E.audit_lines(a))
        self.assertIn('calibration n=3 of 8 sized PRs', text)
        self.assertIn('5 merged before the 14-day window', text); self.assertIn('#50, #51', text)
        self.assertIn('1 more estimates of the same PRs', text)
        self.assertIn('1 logged estimates await settlement (PR not merged, or merged and not yet recorded', text)

    def test_saved_since_is_named_as_the_reason(self):
        now = T0 + timedelta(days=10)
        rows = [row(1, 24 * 9, pr=1), row(1, 24 * 5, pr=2)]
        a = E.calibrate(rows, now, 14, since=T0 + timedelta(days=7))['audit']
        self.assertEqual(a['dropped'], {'merged before the saved --since 2026-10-08': [2]})

    def test_cost_gaps_explain_each_missing_dollar_total(self):
        now = T0 + timedelta(days=1)
        agents = {'codex/gpt-x': {'unpriced_msgs': 4, 'missing_usage_msgs': 2}, 'claude-code/opus': {'unpriced_msgs': 0}}
        rows = [row(2, 1, active=.2, usd=1, pr=1),
                row(2, 2, pr=2, sessions=0),                                                          # no logs
                row(2, 3, active=.2, pr=3, unpriced_msgs=4, missing_usage_msgs=2, agents=agents),
                row(2, 4, active=.2, pr=4, unpriced_msgs=1, agents={'codex/gpt-y': {'unpriced_msgs': 1}})]
        legacy = row(2, 5, active=.2, usd=3, pr=5); legacy['actual'].pop('cost_complete')
        m = E.calibrate(rows + [legacy], now, 14)['audit']['metrics']
        self.assertEqual((m['usd']['n'], m['usd']['of']), (1, 5))
        self.assertEqual(m['usd']['missing'], {
            'no agent logs matched the PR branch': [2],
            'no price for codex/gpt-x': [3],
            'codex/gpt-x responses logged without token usage': [3],
            'no price for codex/gpt-y': [4],
            'cost measured by an older version (run `estimate record --refresh`)': [5]})
        self.assertEqual(m['active_h']['missing'], {'no agent logs matched the PR branch': [2]})
        self.assertIn('API-equivalent $ n=1 of 5; a PR can have several reasons', E.audit_lines(
            E.calibrate(rows + [legacy], now, 14)['audit']))

    def test_calibrate_init_and_forecast_show_the_audit(self):
        now = T0 + timedelta(days=30)
        rows = [row(2, 24 * 29 + h, active=.2, usd=1, pr=h + 1) for h in range(6)] + [row(2, 24, pr=99)]
        with tempfile.TemporaryDirectory() as tmp:
            store = E.Store(tmp)
            store.append([{**{k: v for k, v in r.items() if k != 'actual'}, 'kind': 'estimate'} for r in rows])
            store.append([{**r['actual'], 'kind': 'actual', 'id': r['id']} for r in rows])
            out, _ = cli(store, 'calibrate', now=now)
            self.assertIn('calibration n=6 of 7 sized PRs', out); self.assertIn('1 merged before the 14-day window', out)
            out, err = cli(store, 'forecast', '--sp', '2', now=now)
            self.assertEqual(len(out.strip().splitlines()), 1)     # the quote line stays one line
            self.assertIn('1 outside the window: see `estimate doctor`', err)   # one summary line; the list is in doctor
            E.Store(tmp).write_json('retro-packet.json', {'repo': 'owner/repo', 'prs': []})
            with patch.object(E, 'measure', return_value={}):
                sizes = Path(tmp) / 's.json'; sizes.write_text('{}')
                out, _ = cli(store, 'init', '--apply', str(sizes), '--agent', 'a', '--model', 'm', now=now)
            self.assertIn('1 merged before the 14-day window', out)


class ReviewRegressions(unittest.TestCase):
    """Codex review of PR #8."""

    def test_merge_exactly_at_midnight_end_keeps_its_sp(self):
        end = T0 + timedelta(days=7)                                   # 00:00 UTC
        rows = [row(1, 24 * d + 12, pr=d + 1) for d in range(7)] + [row(13, 24 * 7, pr=99)]
        tp = E.throughput(rows, T0 - timedelta(days=1), end, tz=timezone.utc)
        self.assertEqual(tp['sp'], 20)
        self.assertEqual(sum(sp for sp, _ in tp['samples']), 20)
        self.assertTrue(all(length > 0 for _, length in tp['samples']))
        cal = E.calibrate(rows, end, 14, tz=timezone.utc)
        d = E.simulate_backlog([{'sp': 1}] * 20, cal, sims=1000)['days']
        self.assertLessEqual(d['merge_days'], d['n'])                  # never "8 of 7 days"
        self.assertLess(d['mid'], 12)                                  # 20 SP at ~20 SP/week, not ~19 days

    def test_audit_is_shown_when_every_sized_pr_aged_out(self):
        rows = [row(2, h, active=.2, pr=h + 1) for h in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            store = E.Store(tmp)
            store.append([{**{k: v for k, v in r.items() if k != 'actual'}, 'kind': 'estimate'} for r in rows])
            store.append([{**r['actual'], 'kind': 'actual', 'id': r['id']} for r in rows])
            now = T0 + timedelta(days=60)
            out, err = cli(store, 'forecast', '--sp', '2', now=now)
            self.assertIn('no settled history yet', out)
            self.assertIn('calibration n=0 of 3 sized PRs; 3 outside the window', err)
            backlog = Path(tmp) / 'b.md'; backlog.write_text('| id | title | effort |\n|---|---|---|\n| 1 | a | 2 |\n')
            out, _ = cli(store, 'forecast', '--backlog', str(backlog), now=now)
            self.assertIn('calibration n=0 of 3 sized PRs', out)

    def test_price_hint_names_the_data_directory_in_use(self):
        rows = [row(2, 1, active=.2, pr=1, unpriced_msgs=1, agents={'codex/gpt-y': {'unpriced_msgs': 1}})]
        with tempfile.TemporaryDirectory() as tmp:
            store = E.Store(Path(tmp) / 'data' / 'shop')
            store.append([{**{k: v for k, v in r.items() if k != 'actual'}, 'kind': 'estimate'} for r in rows])
            store.append([{**r['actual'], 'kind': 'actual', 'id': r['id']} for r in rows])
            out, _ = cli(store, 'calibrate', now=T0 + timedelta(days=1))
            self.assertIn(f"add the missing model rates to {store.dir / 'prices.json'}", out)
            self.assertNotIn('.estimate/prices.json', out)

    def test_pending_duplicates_of_settled_estimates_are_named(self):
        done = row(2, 1, pr=7, source='agent'); done['task_ref'] = '#7'
        again = E.new_estimate(2, '#7', 'agent', T0, 'claude-code', 'm1')
        fresh = E.new_estimate(2, 'feat/new', 'agent', T0, 'claude-code', 'm1')
        a = E.calibrate([done, again, fresh], T0 + timedelta(days=1), 14)['audit']
        self.assertEqual((a['pending'], a['duplicates']), (['feat/new'], ['#7']))
        text = '\n'.join(E.audit_lines(a))
        self.assertIn('1 logged estimates repeat an already settled estimate by the same estimator', text)
        self.assertNotIn('wait for their PR to merge', text)

    def test_dashboard_wait_warning_says_one_agent(self):
        sim = {'active_h': {'mid': 3, 'basis': 'b'}, 'days': {'mid': 8, 'sp_merged': 42, 'prs': 13, 'span_days': 12.5,
                                                               'sp_per_day': 3.35, 'merge_days': 3, 'n': 13}}
        self.assertIn('calendar days for one agent', E.calendar_basis(sim, 1.3, one_agent=True)[1])
        self.assertNotIn('one agent', E.calendar_basis(sim)[1])


class RecordReport(unittest.TestCase):
    def pr(self, n, branch, created_h, merged_h, body=''):
        return {'number': n, 'title': 't', 'body': body, 'headRefName': branch, 'commits': [], 'reviews': [], 'files': [],
                'createdAt': E.iso(T0 + timedelta(hours=created_h)), 'mergedAt': E.iso(T0 + timedelta(hours=merged_h))}

    def test_nothing_to_settle_says_what_was_checked(self):
        now = T0 + timedelta(days=40)
        prs = [self.pr(1, 'feat/a', 0, 2), self.pr(2, 'feat/old', 0, 3), self.pr(3, 'feat/c', 4, 5, body='SP: 4'),
               self.pr(4, 'feat/d', 6, 7)]
        with tempfile.TemporaryDirectory() as tmp:
            store = E.Store(tmp)
            r = row(2, 2, pr=1, source='agent'); r['task_ref'] = '#1'
            store.append([{**{k: v for k, v in r.items() if k != 'actual'}, 'kind': 'estimate'},
                          {**r['actual'], 'kind': 'actual', 'id': r['id']}])
            late = E.new_estimate(3, 'feat/old', 'agent', T0 + timedelta(days=1), 'claude-code', 'm1')
            unmerged = E.new_estimate(2, '#77', 'agent', T0 + timedelta(days=1), 'claude-code', 'm1')
            nowhere = E.new_estimate(2, 'feat/new', 'agent', T0 + timedelta(days=1), 'claude-code', 'm1')
            store.append([late, unmerged, nowhere])
            with patch.object(E, 'fetch_prs', return_value=prs), patch.object(E, 'fetch_pr', return_value={'mergedAt': None}), \
                    patch.object(E, 'measure') as measure:
                out, _ = cli(store, 'record', now=now)
            measure.assert_not_called()
        self.assertIn('nothing to settle. Checked 4 merged PRs (--history 200; #1–#4', out)
        self.assertIn('and 3 pending estimate(s)', out)
        self.assertIn('already settled: 1; no logged estimate and no `SP: n` line in the body: 2', out)
        self.assertIn('SP line off the scale', out); self.assertIn('#3', out)
        self.assertIn(f"pending {unmerged['id']} (#77, 2 SP, logged 2026-10-02): PR #77 is not merged yet", out)
        self.assertIn('branch feat/old: its merged PRs (#2) merged before the estimate', out)
        self.assertIn('no merged PR from branch feat/new among the fetched PRs', out)

    def test_empty_ledger_points_to_logging_an_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = E.Store(tmp)
            with patch.object(E, 'fetch_prs', return_value=[]):
                out, _ = cli(store, 'record', now=T0)
        self.assertIn('Checked 0 merged PRs (--history 200) and 0 pending estimate(s)', out)
        self.assertIn('estimate forecast --sp N --task', out)

    def test_already_settled_estimator_is_named(self):
        prs = [self.pr(5, 'feat/e', 0, 2)]
        done = row(2, 2, pr=5, source='agent')
        again = {**E.new_estimate(2, '#5', 'agent', T0, 'claude-code', 'm1')}
        reasons = E.unsettled_reasons([again], prs, {(E.estimator_key(done), 5)})
        self.assertEqual(reasons, [(again, '#5 already has a settled estimate by claude-code/m1')])


if __name__ == '__main__': unittest.main()
