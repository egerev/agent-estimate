"""Learning loop: per-estimator ranges, backtest accuracy on the dashboard, cross-project compare."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(pr, merge_h, sp=2, wall=1.0, active=.5, usd: float | None = 2.0, model='m1', source='agent', created_h=None,
        executor='codex/m1', reviews=1):
    start_h = merge_h - wall
    return {'id': f'{pr}-{model}-{source}', 'sp': sp, 'source': source, 'closest_pr': None,
            'created_at': E.iso(T0 + timedelta(hours=start_h if created_h is None else created_h)),
            'estimator': {'agent': 'codex', 'model': model},
            'actual': {'pr': pr, 'title': 'synthetic', 'merged_at': E.iso(T0 + timedelta(hours=merge_h)),
                       'start_at': E.iso(T0 + timedelta(hours=start_h)), 'wall_h': wall, 'active_h': active,
                       'usd': usd, 'cost_complete': usd is not None, 'review_rounds': reviews, 'executor': executor}}


def events(rows):
    return [e for r in rows for e in ({**{k: v for k, v in r.items() if k != 'actual'}, 'kind': 'estimate'},
                                      {**r['actual'], 'kind': 'actual', 'id': r['id']})]


class EstimatorFallback(unittest.TestCase):
    NOW = T0 + timedelta(days=3)

    def pooled_rows(self):
        # another estimator with slow PRs: pooled rates differ clearly from m1's own
        return [row(100 + i, 10 + i, wall=8, model='m2') for i in range(10)]

    def test_five_prework_samples_switch_to_own_rates(self):
        own = [row(i, 30 + i, wall=1) for i in range(E.ESTIMATOR_MIN_N)]
        cal = E.calibrate(own + self.pooled_rows(), self.NOW)
        q = E.quote(cal, 2, estimator='codex/m1')
        self.assertEqual((q['scope'], q['estimator_prework']), ('codex/m1', 5))
        self.assertEqual(q['wall_h']['mid'], 1)
        self.assertIn('own ranges of codex/m1, 5 pre-work PRs', E.quote_line(q))

    def test_below_threshold_falls_back_to_pooled_and_reports_sample_size(self):
        own = [row(i, 30 + i, wall=1) for i in range(E.ESTIMATOR_MIN_N - 1)]
        cal = E.calibrate(own + self.pooled_rows(), self.NOW)
        q = E.quote(cal, 2, estimator='codex/m1')
        self.assertEqual((q['scope'], q['estimator_prework']), (E.ALL, 4))
        self.assertGreater(q['wall_h']['mid'], 1)
        self.assertIn('codex/m1 has 4/5 pre-work PRs', E.quote_line(q))

    def test_retro_and_after_start_estimates_do_not_count_as_prework(self):
        retro = [row(i, 30 + i, source='retro') for i in range(10)]
        late = [row(20 + i, 50 + i, wall=2, created_h=49 + i, model='m1') for i in range(3)]
        cal = E.calibrate(retro + late + self.pooled_rows(), self.NOW)
        self.assertEqual(cal['groups']['codex/m1']['n'], 13)
        self.assertEqual(cal['groups']['codex/m1']['prework'], 0)
        self.assertEqual(E.quote(cal, 2, estimator='codex/m1')['scope'], E.ALL)

    def test_no_estimator_says_pooled(self):
        cal = E.calibrate(self.pooled_rows(), self.NOW)
        self.assertIn('(pooled: all estimators)', E.quote_line(E.quote(cal, 2)))

    def test_old_calibration_without_prework_field_falls_back(self):
        cal = E.calibrate([row(i, 30 + i) for i in range(6)], self.NOW)
        for g in cal['groups'].values(): g.pop('prework')
        self.assertEqual(E.quote(cal, 2, estimator='codex/m1')['scope'], E.ALL)


class DashboardAccuracy(unittest.TestCase):
    def rows(self, k):
        # alternating 1 h / 2 h PRs every 3 hours, each estimated right at work start
        return [row(i, 3 * (i + 1), wall=1 + i % 2) for i in range(k)]

    def test_backtest_scores_and_pairs_with_the_baseline(self):
        m = E.backtest(self.rows(30))['metrics']['wall_h']
        self.assertGreaterEqual(m['n'], 10); self.assertGreaterEqual(m['paired']['n'], 10)
        self.assertIn('mdape_baseline_last10', m['paired'])

    def test_verdict_uses_only_targets_with_a_baseline(self):
        # Codex r1 repro: the last ten PRs before each late target have no measured cost, so late targets
        # get a forecast error (0%) but no baseline. Unpaired medians would wrongly say "beats it".
        old = [row(i, i + 1, usd=1) for i in range(11)]                # $0.5/SP; targets 6-10 err 0 vs 0
        bad = [row(50 + i, 11 + i * .1, usd=10) for i in range(5)]      # err 90% for forecast and baseline
        late = []
        for i in range(6):  # each exact late target follows ten unpriced PRs: no baseline for it
            h = 20 + 12 * i
            late += [row(100 + 10 * i + j, h + j, usd=None) for j in range(10)] + [row(200 + i, h + 11, usd=1)]
        acc = E.backtest(old + bad + late)
        u = acc['metrics']['usd']
        self.assertEqual((u['n'], u['paired']['n']), (16, 10))
        self.assertLess(u['mdape'], u['mdape_baseline_last10'])          # the misleading unpaired view
        self.assertEqual(u['paired']['mdape'], u['paired']['mdape_baseline_last10'])   # paired: no better
        self.assertEqual(E.backtest(old + late)['metrics']['usd']['paired']['n'], 5)


class Compare(unittest.TestCase):
    def rows_a(self):
        return [row(1, 10, sp=2, wall=4, active=1, usd=4, executor='claude-code/opus', reviews=2),
                row(1, 10, sp=3, wall=4, active=1, usd=4, model='m2', source='retro'),  # same PR, counted once
                row(2, 20, sp=1, wall=2, active=1, usd=None, executor='codex/gpt', reviews=0),
                row(3, 30, sp=2, wall=2, active=2, usd=1, executor='claude-code/opus', reviews=1)]

    def test_summary_counts_each_pr_once(self):
        s = E.project_summary(self.rows_a(), 'me/a')
        self.assertEqual(s['prs'], 3)
        self.assertEqual(s['wall_h_per_sp'], {'median': 2, 'n': 3})     # 4/2, 2/1, 2/2
        self.assertEqual(s['active_h_per_sp'], {'median': 1, 'n': 3})    # .5, 1, 1
        self.assertEqual(s['wait_share'], {'median': .5, 'n': 3})        # .75, .5, 0
        self.assertEqual(s['usd_per_sp'], {'median': 1.25, 'n': 2})      # incomplete cost excluded
        self.assertEqual(s['review_rounds'], {'median': 1, 'n': 3})
        self.assertEqual(s['executors'], {'claude-code/opus': .667, 'codex/gpt': .333})

    def test_empty_project(self):
        s = E.project_summary([], 'empty')
        self.assertEqual((s['prs'], s['wall_h_per_sp']['median'], s['executors']), (0, None, {}))
        self.assertIn('wall h / SP', E.compare_text([s]))

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): E.main(['compare', *argv])
        return out.getvalue()

    def test_cli_one_table_per_project_labelled_by_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = E.Store(Path(tmp) / 'alpha' / '.estimate'); b = E.Store(Path(tmp) / 'beta-data')
            a.append(events(self.rows_a())); a.write_json('retro-packet.json', {'repo': 'me/alpha', 'prs': []})
            b.append(events([row(9, 5, sp=1, wall=3, active=1, usd=2, executor='codex/gpt')]))
            text = self.run_cli('--dir', str(a.dir), '--dir', str(b.dir))
            self.assertIn('me/alpha, merged 2026-10-01 → 2026-10-02', text)
            self.assertIn('beta-data, merged', text)
            self.assertRegex(text, r'wall h / SP\s+2\.00 \(n=3\)')
            self.assertRegex(text, r'\$ / SP\s+\$1\.25 \(n=2\)')
            self.assertRegex(text, r'executor mix\s+claude-code/opus 67%, codex/gpt 33%')
            data = json.loads(self.run_cli('--dir', str(a.dir), '--dir', str(b.dir), '--json'))
            self.assertEqual([d['project'] for d in data], ['me/alpha', 'beta-data'])
            self.assertEqual(data[1]['wait_share']['median'], .667)

    def test_cli_rejects_a_dir_without_ledger(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as cm:
            self.run_cli('--dir', tmp)
        self.assertIn('no ledger', str(cm.exception.code))


if __name__ == '__main__':
    unittest.main()
