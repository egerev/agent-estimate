"""Dashboard: critical path, parallel-agent days, the Teammate page and backlog path. Synthetic fixtures only."""
import re
import sys
import tempfile
import unittest
from unittest import mock
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import estimate as E  # noqa: E402
from test_estimate import T0, row  # noqa: E402

HEAD = '| id | title | effort | value | depends |\n|----|-------|--------|-------|---------|\n'


def items(*rows):
    return E.parse_backlog(HEAD + ''.join(f'| {" | ".join(map(str, r))} |\n' for r in rows))


def path(*rows):
    cp = E.critical_path(items(*rows)); return cp['sp'], cp['chain']


class CriticalPath(unittest.TestCase):
    def test_no_dependencies_is_the_largest_item(self):
        self.assertEqual(path((1, 'a', 3, 5, ''), (2, 'b', 5, 3, '')), (5, ['2']))

    def test_heaviest_chain_wins_over_the_longest(self):
        self.assertEqual(path((1, 'a', 1, 5, ''), (2, 'b', 1, 5, 1), (3, 'c', 1, 5, 2), (4, 'd', 8, 5, ''), (5, 'e', 2, 5, 4)),
                         (10, ['4', '5']))

    def test_diamond_counts_shared_dependency_once(self):
        self.assertEqual(path((1, 'root', 3, 5, ''), (2, 'left', 2, 5, 1), (3, 'right', 5, 5, 1), (4, 'join', 1, 5, '2, 3')),
                         (9, ['1', '3', '4']))

    def test_cycle_is_named_and_left_out_of_the_chain(self):
        cp = E.critical_path(items((1, 'a', 2, 5, 2), (2, 'b', 3, 5, 1), (3, 'c', 1, 5, 99), (4, 'd', 8, 5, 1)))
        self.assertEqual(cp['cycle'], ['2', '1', '2'])        # #2 before #1 before #2; #4 is only behind it
        self.assertEqual((cp['sp'], cp['chain']), (1, ['3']))
        self.assertEqual(cp['unknown'], [('3', ['99'])])
        self.assertEqual(E.critical_path([]), {'sp': 0, 'chain': [], 'cycle': [], 'unknown': []})

    def test_long_chain_needs_no_recursion(self):
        n = 5000
        cp = E.critical_path([{'id': str(k), 'sp': 1, 'depends': [str(k - 1)] if k else []} for k in range(n)])
        self.assertEqual((cp['sp'], len(cp['chain']), cp['cycle']), (n, n, []))


class ParallelDays(unittest.TestCase):
    D = {'lo': 6.0, 'mid': 10.0, 'hi': 20.0}

    def test_one_agent_is_the_serial_forecast(self):
        self.assertEqual(E.parallel_days(self.D, 1, 30, 5), {**self.D, 'limited': False})

    def test_agents_divide_days_until_the_critical_path_floor(self):
        two = E.parallel_days(self.D, 2, 30, 5) or {}
        self.assertEqual((two['lo'], two['mid'], two['hi'], two['limited']), (3, 5, 10, False))
        five = E.parallel_days(self.D, 5, 30, 10) or {}  # the path is a third of the work: floor = days / 3
        self.assertAlmostEqual(five['mid'], 10 / 3); self.assertAlmostEqual(five['hi'], 20 / 3)
        self.assertTrue(five['limited'])

    def test_a_single_chain_never_speeds_up(self):
        self.assertEqual((E.parallel_days(self.D, 5, 13, 13) or {})['mid'], 10)

    def test_one_agent_removes_the_repo_concurrency(self):
        one = E.parallel_days(self.D, 1, 30, 5, concurrency=2.0) or {}
        self.assertEqual((one['lo'], one['mid'], one['hi']), (12, 20, 40))
        two = E.parallel_days(self.D, 2, 30, 5, concurrency=2.0) or {}
        self.assertEqual(two['mid'], 10)                       # at least the repo's own pace with 2 in flight

    def test_no_days_or_empty_backlog(self):
        self.assertIsNone(E.parallel_days(None, 2, 10, 3)); self.assertIsNone(E.parallel_days(self.D, 2, 0, 0))


class Page(unittest.TestCase):
    NOW = T0 + timedelta(days=8, hours=18)     # 18:00 UTC: 6 hours left today

    def rows(self):
        return [row(3, 20 * h, wall=2 + h % 3, active=1 + h % 2, usd=3.0 + h % 2) for h in range(1, 9)]

    def page(self, backlog, rows=None, source=None, unsized=(), burn=None):
        rs = self.rows() if rows is None else rows
        return E.dashboard_html('me/r', rs, E.calibrate(rs, self.NOW, 14), backlog, self.NOW, source, unsized, burn, timezone.utc)

    @staticmethod
    def titles(page):
        return re.findall(r'<li class="[^"]*"><span class="n">(\d+)</span><span class="t" title="([^"]*)"', page)

    @staticmethod
    def states(page):
        return {(b, int(a)): tuple(map(int, v)) for b, a, *v in re.findall(
            r'<span class="v (t\d)a(\d)"><span data-sure="(\d+)" data-likely="(\d+)" data-maybe="(\d+)">', page)}

    def test_one_answer_the_list_and_nothing_else(self):
        page = self.page(items((1, 'Schema', 3, 5, ''), (2, 'UI <b>x</b>', 2, 8, 1), (3, 'Docs', 1, 1, '')))
        self.assertEqual(len(self.states(page)), 20)                                 # 5 time boxes × 4 agent counts
        radios = re.findall(r'<input type="radio" class="radio" name="(tb|ag)" id="\w+-(\w+)"[^>]*?( checked)?>', page)
        self.assertEqual(radios, [('tb', 't0', ' checked'), ('tb', 't1', ''), ('tb', 't2', ''), ('tb', 't3', ''), ('tb', 't7', ''),
                                  ('ag', '1', ''), ('ag', '2', ''), ('ag', '3', ' checked'), ('ag', '5', '')])  # today, 3 agents
        self.assertIn('Today<small>6 h left</small>', page)
        self.assertRegex(page, r'<span class="v t0a3"><b>(All 6 SP</b> fit|\d+ SP</b> likely done) in the rest of today with 3 agents: '
                               r'about <b>[\d.]+ (h|min)</b> of agent work, <b>\$[\d.,]+</b>')
        self.assertRegex(page, r'Sure: \d+ cards? · likely: \d+ · maybe: \d+\.')
        self.assertIn('Lanes are an ideal schedule in Score order, dependencies first; review and merge waits are not drawn.', page)
        self.assertEqual(page.count('no snapshot yet'), 3)                            # plan windows need snapshots, never guessed
        for gone in ('<script', '<svg', '<table', 'backtest', 'critical path'):
            self.assertNotIn(gone, page)
        self.assertNotIn('<b>x</b>', page)
        self.assertEqual([t for _, t in self.titles(page)], ['Schema', 'UI &lt;b&gt;x&lt;/b&gt;', 'Docs'])  # #2 waits for #1
        self.assertIn('<span class="wait">waits for #1</span>', page)
        self.assertIn('Score is value ÷ effort', page)

    def test_backlog_size_is_visible_next_to_the_forecast(self):
        page = self.page(items((1, 'Schema', 3, 5, ''), (2, 'UI', 2, 8, 1), (3, 'Docs', 1, 1, '')))
        self.assertIn('<small>of 3 cards</small>', page)
        self.assertRegex(page, r'<div>All 6 SP, 1 agent</div><div><b>[\d.]+ d</b><small>[\d.]+–[\d.]+</small>')
        self.assertRegex(page, r'<div>Agent work per SP</div><div><b>[\d.]+ (h|min)</b><small>p20–p80 .* · 8 PRs</small>')
        self.assertRegex(page, r'<div>\$ per SP</div><div><b>\$[\d.]+</b><small>floor · p20–p80 .* · 8 PRs</small>')
        self.assertIn('<div>Tokens per SP</div><div><span class="no">not enough data</span>', page)  # no logged usage here

    def test_missing_hours_and_cost_say_so_quietly(self):
        rs = [row(3, 20 * h, wall=2 + h % 3) for h in range(1, 9)]          # no active hours, no cost
        page = self.page(items((1, 'a', 3, 5, '')), rows=rs)
        for label in ('Agent work per SP', '$ per SP', 'Tokens per SP'):
            self.assertIn(f'<div>{label}</div><div><span class="no">not enough data</span></div>', page)
        self.assertNotIn('of agent work', page)
        self.assertRegex(page, r'likely done in the rest of today with 3 agents\.|All 3 SP</b> fit in the rest of today with 3 agents\.')
        nohist = self.page(items((1, 'a', 3, 5, '')), rows=[])                # no history: the hero already says why
        self.assertNotIn('not enough data', nohist)

    def test_only_unsized_cards_ask_for_sizing(self):
        uns = [{'id': 'u1', 'key': '', 'title': 'One'}, {'id': 'u2', 'key': '', 'title': 'Two'}]
        for backlog in (None, []):
            page = self.page(backlog, unsized=uns)
            self.assertIn('2<small>cards need sizing</small>', page); self.assertIn('estimate-backlog', page)
            self.assertNotIn('No backlog yet', page); self.assertIn('Backlog · 2 cards', page)
            self.assertEqual(page.count('<li class="none">'), 2)
        self.assertIn('1<small>card needs sizing</small>', self.page(None, unsized=uns[:1]))

    def test_more_agents_and_longer_boxes_never_get_less_done(self):
        page = self.page(items(*[(k, f'item {k}', (1, 2, 3)[k % 3], 5, '') for k in range(1, 30)]))
        st = self.states(page)
        for sure, likely, maybe in st.values():
            self.assertLessEqual(sure, likely); self.assertLessEqual(likely, maybe)
        for b in ('t0', 't1', 't2', 't3', 't7'):
            self.assertEqual([st[(b, a)][1] for a in E.PARALLEL], sorted(st[(b, a)][1] for a in E.PARALLEL))
        for a in E.PARALLEL:
            days = [st[(b, a)][1] for b in ('t1', 't2', 't3', 't7')]
            self.assertEqual(days, sorted(days))

    def test_source_line_names_tiden_and_unsized_cards_are_listed_apart(self):
        pulled = T0 + timedelta(days=7, hours=6, minutes=5)
        page = self.page(items((1, 'a', 3, 5, '')), source={'label': 'Tiden · Shop', 'updated': pulled},
                         unsized=[{'id': 'u1', 'key': 'KEY-9', 'title': 'KEY-9 — Not sized'}])
        self.assertIn(f'Tiden · Shop · 2 cards · updated {pulled.day} {pulled:%b} 06:05 UTC', page)
        self.assertIn('<span class="k">KEY-9</span>Not sized</span><span class="s" aria-hidden="true">–</span>'
                      '<span class="p"><span aria-hidden="true">?</span><span class="vh">no size yet</span></span>', page)
        self.assertIn('1 card has no size yet and is not counted.', page)

    def test_top_twelve_then_show_all(self):
        page = self.page(items(*[(k, f'item {k}', 1, 5, '') for k in range(1, 15)]))
        top, rest = page.split('<input type="checkbox" id="all"')
        self.assertEqual(len(self.titles(top)), E.TOP_ROWS); self.assertEqual(len(self.titles(rest)), 2)
        self.assertIn('Show all 14', rest)
        self.assertNotIn('id="all"', self.page(items((1, 'a', 3, 5, ''))))

    def test_no_history_still_shows_the_backlog(self):
        page = self.page(items((1, 'a', 3, 5, '')), rows=[])
        self.assertIn('3<small>SP in 1 card</small>', page); self.assertIn('No history yet', page)
        self.assertEqual(len(self.titles(page)), 1); self.assertNotIn('name="tb"', page); self.assertNotIn('class="lanes"', page)

    def test_cycle_hides_days_and_unknown_dependency_warns(self):
        page = self.page(items((1, 'a', 3, 5, 2), (2, 'b', 2, 5, 1), (3, 'c', 1, 5, 42)))
        self.assertIn('Cards #2 → #1 → #2 wait for each other in a loop', page); self.assertNotIn('name="tb"', page)
        self.assertIn('#3 depends on unknown #42', page)

    def test_no_backlog_explains_where_to_put_it(self):
        page = self.page(None)
        self.assertIn('No backlog yet</h1>', page); self.assertIn('Backlog · no cards', page)


class Source(unittest.TestCase):
    def test_pulled_tiden_table_never_shows_the_local_file_name(self):
        with tempfile.TemporaryDirectory() as d:
            st = E.Store(d); st.ensure(); st.write_json('config.json', {'backlog': {'source': 'tiden', 'product_id': 'p-1'}})
            text = '# Backlog from Tiden product Shop (p-1)\n\nPulled 2026-10-22T06:00:48Z by `estimate backlog pull` (x).\n'
            path = st.dir / E.PULLED_BACKLOG; path.write_text(text)
            src = E.dashboard_source(st, path, text, 'acme/shop')
            self.assertEqual(src, {'label': 'Tiden · Shop', 'updated': E.utc('2026-10-22T06:00:48Z')})
            old = '# Backlog from Tiden product p-1\n\nPulled 2026-10-22T06:00:48Z by `estimate backlog pull` (x).\n'
            self.assertEqual(E.dashboard_source(st, path, old, 'acme/shop')['label'], 'Tiden · shop')
            other = Path(d) / 'plan.md'; other.write_text('x')
            self.assertEqual(E.dashboard_source(st, other, 'x', 'o/r')['label'], 'plan.md')     # an explicit --backlog file


class Concurrency(unittest.TestCase):
    def test_overlap_over_union(self):
        rs = [row(3, 10, wall=4, pr=1), row(3, 10, wall=4, pr=2), row(3, 30, wall=4, pr=3)]  # two in parallel, then one alone
        c = E.observed_concurrency(rs, E.calibrate(rs, T0 + timedelta(days=3), 14)) or {}
        self.assertAlmostEqual(c['value'], 12 / 8); self.assertEqual(c['prs'], 3)

    def test_serial_work_is_one_and_no_data_is_none(self):
        rs = [row(3, 10 * k, wall=2) for k in range(1, 4)]
        self.assertEqual((E.observed_concurrency(rs, E.calibrate(rs, T0 + timedelta(days=3), 14)) or {})['value'], 1.0)
        self.assertIsNone(E.observed_concurrency([], E.calibrate([], T0, 14))); self.assertIsNone(E.observed_concurrency([], None))


class BacklogPath(unittest.TestCase):
    def test_paths_resolve_from_the_git_root_not_the_data_dir(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(E, 'git_root', return_value=str(Path(d) / 'repo')):
            root = Path(d) / 'repo'; root.mkdir(); st = E.Store(Path(d) / 'elsewhere' / 'data'); st.ensure()
            self.assertEqual(E.dashboard_backlog_path(st), root / 'BACKLOG.md')            # missing: empty state
            (st.dir / 'backlog.txt').write_text('3 x\n')
            self.assertEqual(E.dashboard_backlog_path(st), st.dir / 'backlog.txt')        # legacy fallback
            (root / 'BACKLOG.md').write_text(HEAD)
            self.assertEqual(E.dashboard_backlog_path(st), root / 'BACKLOG.md')
            st.write_json('config.json', {'backlog': {'source': 'markdown', 'path': 'docs/plan.md'}})
            self.assertEqual(E.dashboard_backlog_path(st), root / 'docs' / 'plan.md')
            st.write_json('config.json', {'backlog': {'source': 'tiden'}})
            self.assertEqual(E.dashboard_backlog_path(st), st.dir / E.PULLED_BACKLOG)


if __name__ == '__main__':
    unittest.main()
