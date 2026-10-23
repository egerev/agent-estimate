"""Regression tests for independent samples, metric coverage and honest backtesting."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(pr, merge_h, sp=2, wall=1, active=.5, usd=2, model='m1', source='agent', created_h=None):
    start_h = merge_h - wall
    return {'id': f'{pr}-{model}-{source}', 'sp': sp, 'source': source, 'closest_pr': None,
            'created_at': E.iso(T0 + timedelta(hours=start_h if created_h is None else created_h)),
            'estimator': {'agent': 'codex', 'model': model},
            'actual': {'pr': pr, 'title': 'synthetic', 'merged_at': E.iso(T0 + timedelta(hours=merge_h)),
                       'start_at': E.iso(T0 + timedelta(hours=start_h)), 'wall_h': wall, 'active_h': active,
                       'usd': usd, 'cost_complete': usd is not None, 'review_rounds': 1, 'executor': 'codex/m1'}}


class IndependentSamples(unittest.TestCase):
    def test_fifteen_estimators_on_one_pr_remain_one_overall_sample(self):
        rows = [row(1, 10, model=f'm{i}') for i in range(15)]
        cal = E.calibrate(rows, T0 + timedelta(days=1))
        q = E.quote(cal, 2)
        self.assertEqual((cal['n'], cal['prs'], q['n']), (1, 1, 1))
        self.assertEqual(q['confidence'], 'low')
        self.assertEqual(len(cal['groups']), 15)
        self.assertTrue(all(g['n'] == 1 for g in cal['groups'].values()))

    def test_duplicate_estimates_by_same_model_do_not_inflate_group(self):
        rows = [row(1, 10, created_h=i) for i in range(6)]
        cal = E.calibrate(rows, T0 + timedelta(days=1))
        self.assertEqual(cal['groups']['codex/m1']['n'], 1)

    def test_each_metric_keeps_its_own_confidence_and_count(self):
        rows = [row(i, i + 1, usd=2 if i == 0 else None) for i in range(15)]
        q = E.quote(E.calibrate(rows, T0 + timedelta(days=1)), 2)
        self.assertEqual(q['wall_h']['confidence'], 'high')
        self.assertEqual(q['usd']['confidence'], 'low')
        self.assertEqual((q['usd']['n'], q['n'], q['confidence']), (1, 1, 'low'))
        line = E.quote_line(q)
        self.assertIn('observed active ≈', line)
        self.assertIn('n=1, low', line)
        self.assertIn('n=15, high', line)

    def test_explicit_partial_and_legacy_unpriced_costs_are_excluded(self):
        rows = [row(i, i + 1) for i in range(4)]
        rows[0]['actual']['cost_complete'] = False
        rows[1]['actual'].pop('cost_complete')
        rows[1]['actual']['unpriced_msgs'] = 1
        rows[3]['actual'].pop('cost_complete')  # no proof either way: requires refresh
        cal = E.calibrate(rows, T0 + timedelta(days=1))
        self.assertEqual(cal['per_sp']['usd'], [1])
        self.assertEqual(cal['buckets']['2']['usd']['n'], 1)


class HonestBacktest(unittest.TestCase):
    def history(self):
        return [row(i, 2 * i + 2) for i in range(6)]

    def test_excludes_postmerge_inprogress_unknown_timing_targets_and_training(self):
        good = self.history()
        bad = [row(20, 20, wall=100, created_h=21),
               row(21, 21, wall=100, created_h=20),
               row(22, 22, source='pr-body')]
        result = E.backtest(good + bad)
        self.assertEqual(result['n'], 1)
        self.assertEqual(result['metrics']['wall_h']['n'], 1)
        self.assertEqual(result['metrics']['wall_h']['mdape'], 0)

    def test_invalid_history_cannot_make_five_training_samples(self):
        rows = [row(i, i + 1, source='pr-body') for i in range(5)] + [row(20, 20)]
        self.assertEqual(E.backtest(rows)['n'], 0)

    def test_target_pr_is_scored_once_and_never_trains_itself(self):
        rows = self.history()
        target = row(30, 30, created_h=25)
        duplicate = row(30, 30, sp=8, model='m2', created_h=31)
        result = E.backtest(rows + [target, duplicate])
        self.assertEqual(result['n'], 2)
        self.assertEqual(result['metrics']['wall_h']['n'], 2)
        self.assertEqual(result['metrics']['wall_h']['mdape'], 0)

    def test_last_ten_baseline_counts_each_pr_once(self):
        rows = [row(i, 2 * i + 2, wall=1) for i in range(5)]
        # Same PR is sized by many models. Its wall time must have one vote in the baseline.
        rows += [row(10, 14, wall=10, model=f'm{i}') for i in range(15)]
        rows += [row(20, 20, wall=1)]
        result = E.backtest(rows)
        self.assertEqual(result['metrics']['wall_h']['mdape_baseline_last10'], 0)

    def test_since_clips_both_targets_and_training(self):
        rows = self.history() + [row(30, 30)]
        result = E.backtest(rows, since=T0 + timedelta(hours=20))
        self.assertEqual(result['n'], 0)
        self.assertEqual(result['skipped'], 1)

    def test_retro_flag_reports_diagnostic_results_separately(self):
        rows = [row(i, 2 * i + 2, source='retro', created_h=100) for i in range(8)]
        self.assertEqual(E.backtest(rows)['n'], 0)
        result = E.backtest(rows, include_retro=True)
        self.assertEqual(result['n'], 0)
        self.assertGreater(result['retro']['n'], 0)
        self.assertIn('retrospective', result['retro']['note'])

    def test_prospective_bootstrap_can_train_on_retro_sizes_logged_before_target(self):
        retros = [row(i, i + 1, source='retro', created_h=10) for i in range(5)]
        target = row(20, 20)
        self.assertEqual(E.backtest(retros + [target])['n'], 1)
        for r in retros: r['created_at'] = E.iso(T0 + timedelta(hours=21))
        self.assertEqual(E.backtest(retros + [target])['n'], 0)

    def test_body_training_uses_observation_time_and_never_pr_creation_time(self):
        bodies = [row(i, i + 1, source='pr-body', created_h=0) for i in range(5)]
        target = row(20, 20)
        self.assertEqual(E.backtest(bodies + [target])['n'], 0)
        for r in bodies: r['observed_at'] = E.iso(T0 + timedelta(hours=10))
        self.assertEqual(E.backtest(bodies + [target])['n'], 1)
        for r in bodies: r['observed_at'] = E.iso(T0 + timedelta(hours=21))
        self.assertEqual(E.backtest(bodies + [target])['n'], 0)

    def test_usd_with_incomplete_coverage_is_not_scored(self):
        rows = self.history()
        target = row(30, 30)
        target['actual']['cost_complete'] = False
        result = E.backtest(rows + [target])
        self.assertEqual(result['metrics']['usd']['n'], 1)  # only the eligible historical target


class BacklogUncertainty(unittest.TestCase):
    def test_invalid_non_fibonacci_or_zero_sizes_are_rejected(self):
        for text in ('0 Empty', '4 Arbitrary', 'title | 6', '21 Too large'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                E.parse_backlog(text)
        self.assertEqual(E.parse_backlog('13 Split me')[0]['sp'], 13)  # retained for the split warning

    def test_small_sample_uses_scaled_min_max_instead_of_narrow_monte_carlo(self):
        rows = [row(1, 3, sp=1, wall=1), row(2, 6, sp=1, wall=4)]
        cal = E.calibrate(rows, T0 + timedelta(days=1))
        items = [{'sp': 1, 'title': str(i)} for i in range(20)]
        q = E.simulate_backlog(items, cal, sims=100)['wall_h']
        self.assertEqual((q['lo'], q['mid'], q['hi']), (20, 50, 80))
        self.assertEqual((q['n'], q['kind'], q['confidence']), (2, 'min-max', 'low'))

    def test_direct_simulation_validates_sizes_and_positive_simulation_count(self):
        cal = E.calibrate([row(1, 2)], T0 + timedelta(days=1))
        with self.assertRaises(ValueError): E.simulate_backlog([{'sp': 0}], cal)
        with self.assertRaises(ValueError): E.simulate_backlog([{'sp': 2}], cal, sims=0)

    def test_many_calendar_days_do_not_make_one_pr_confident(self):
        cal = E.calibrate([row(1, 48, wall=48)], T0 + timedelta(days=14), tz=timezone.utc)
        days = E.simulate_backlog([{'sp': 2}], cal, sims=100)['days']
        self.assertGreaterEqual(days['full_days'], 7)
        self.assertEqual(days['confidence'], 'low')


if __name__ == '__main__': unittest.main()
