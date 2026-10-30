"""Critical-chain plan: lanes, scope, the list scheduler, `estimate plan`, the Ahead section and Depends from Tiden
card text. Synthetic data only; the tiden subprocess is never run."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import estimate as E  # noqa: E402
from test_estimate import T0, row  # noqa: E402

NOW = T0 + timedelta(days=11)
TABLE = '| id | title | effort | value | depends | lane |\n|---|---|---|---|---|---|\n'


def backlog(rows, tail=''):
    """Markdown backlog: rows of (id, sp, value, depends, lane)."""
    return TABLE + ''.join(f'| {i} | card {i} | {sp} | {v} | {d} | {lane} |\n' for i, sp, v, d, lane in rows) + tail


def history(n=8):
    """n merged PRs over the last days: 1 h of work per SP, then a 2 h merge wait (last commit to merge)."""
    rows, commits = [], {}
    for k in range(n):
        merged = 200 + k * 6
        rows.append(row(2, merged, 4.0, active=1.0, usd=1.0, pr=k + 1))
        commits[k + 1] = [T0 + timedelta(hours=merged - 2)]
    return rows, commits


def cal_of(rows):
    return E.calibrate(rows, NOW, 14)


def items(text):
    unsized = []; out = E.parse_backlog(text, unsized)
    return out, unsized


class Lanes(unittest.TestCase):
    def test_lanes_section_and_config_caps_config_wins(self):
        text = backlog([('1', 1, 1, '', 'db')], '\n## Lanes\nOne writer each.\n- db (cap 1): migrations\n'
                       '- `bot.py` (merging cap 1): hot file\n- ci (cap 2 for workflows)\n- web: no cap\n\n## Other\n- x (cap 1)\n')
        caps, problems = E.lane_caps(text, {'lanes': {'ci': {'merging': 1}, 'api': 3}})
        self.assertEqual(caps, {'db': {'open': 1, 'merging': None}, 'bot.py': {'open': None, 'merging': 1},
                                'ci': {'open': None, 'merging': 1}, 'api': {'open': 3, 'merging': None}})
        self.assertEqual(problems, [])

    def test_bad_lane_config_is_reported_and_ignored(self):
        caps, problems = E.lane_caps('', {'lanes': {'db': 0, 'ci': {'open': 'one'}, 'ok': {'open': 1}}})
        self.assertEqual(caps, {'ok': {'open': 1, 'merging': None}})
        self.assertEqual(len(problems), 2)
        self.assertEqual(E.lane_caps('', {'lanes': [1]})[1], ['config.json: lanes must be {"lane": {"open": 1}} or {"lane": 1}; ignored'])

    def test_lane_column_is_read(self):
        self.assertEqual([i['lane'] for i in items(backlog([('1', 1, 1, '', 'db'), ('2', 1, 1, '', '')]))[0]], ['db', ''])


class Scheduler(unittest.TestCase):
    def run_plan(self, deps, work, wait, agents, lanes=None, caps=None, late=None):
        n = len(work); dependents = defaultdict(list)
        for i, d in enumerate(deps):
            for j in d: dependents[j].append(i)
        return E._plan_run(deps, dependents, work, wait, agents, lanes or [''] * n, caps or {},
                           late or [1e9] * n, list(range(n)))

    def test_open_cap_runs_lane_cards_one_after_another(self):
        start, worked, done, _ = self.run_plan([[], [], []], [2, 2, 2], [1, 1, 1], 3, ['db', 'db', ''], {'db': {'open': 1}})
        self.assertEqual((start, done), ([0, 3, 0], [3, 6, 3]))  # the second db card starts only after the first merged

    def test_merging_cap_queues_merges_but_not_work(self):
        start, worked, done, _ = self.run_plan([[], []], [2, 2], [1, 1], 2, ['m', 'm'], {'m': {'merging': 1}})
        self.assertEqual((start, worked, done), ([0, 0], [2, 2], [3, 4]))

    def test_dependents_wait_for_the_merge_and_agents_move_on(self):
        start, worked, done, who = self.run_plan([[], [0], []], [2, 2, 2], [1, 1, 1], 1)
        self.assertEqual(start, [0, 4, 2])  # #2 waits for #0's merge at 3; the free agent takes #2 at 2 first
        self.assertEqual(max(done), 7)

    def test_a_card_past_its_late_start_goes_before_higher_score(self):
        # card 0 has the best Score; card 1 heads a chain and its late start (0) has come
        start = self.run_plan([[], [], [1]], [1, 1, 5], [0, 0, 0], 1, late=[5, 0, 1])[0]
        self.assertEqual(start, [6, 0, 1])  # the chain card 2, ready at 1 and past its late start, also goes first

    def test_events_at_one_time_are_all_handled_before_dispatch(self):
        # review r1: A's zero merge wait must release B before the freed agent takes C
        late = [0, 1, 9, 0, 0]  # A → B → D is the chain; C and X are free
        start = self.run_plan([[], [0], [], [1], []], [1, 8, 8, 8, 5], [0] * 5, 2, late=late)[0]
        self.assertEqual(start[1], 1)


class Plan(unittest.TestCase):
    def plan(self, text, agents=3, **kw):
        rows, commits = history()
        its, unsized = items(text)
        return E.plan_forecast(its, unsized, rows, cal_of(rows), commits, kw.pop('caps', {}), agents, runs=60, **kw)

    def test_unknown_cycle_and_unsized_dependencies_are_excluded_never_crash(self):
        text = backlog([('1', 2, 5, '', ''), ('2', 1, 3, '9', ''), ('3', 1, 3, '4', ''), ('4', 1, 3, '3', ''),
                        ('5', 1, 3, '6', ''), ('6', '', '', '', ''), ('7', 1, 1, '2', '')])
        p = self.plan(text)
        self.assertEqual([c['id'] for c in p['cards']], ['1'])
        self.assertEqual({x['id']: x['reason'] for x in p['excluded']},
                         {'2': 'depends on unknown #9', '3': 'in a dependency cycle', '4': 'in a dependency cycle',
                          '5': 'depends on unsized #6', '7': 'waits for excluded #2'})
        self.assertEqual(p['unsized'], ['6'])
        self.assertIn('skipped 1 unsized card(s), not planned: #6', '\n'.join(E.plan_lines(p, NOW)))

    def test_chain_waves_buffer_and_more_agents_never_later(self):
        text = backlog([('a', 5, 8, '', ''), ('b', 3, 8, 'a', ''), ('c', 3, 8, 'b', ''), ('d', 2, 5, '', ''),
                        ('e', 2, 3, '', ''), ('f', 1, 2, '', '')])
        p = self.plan(text)
        self.assertEqual(p['chain']['ids'], ['a', 'b', 'c'])
        self.assertEqual(p['chain']['sp'], 11)
        ends = [p['agents'][k]['end']['p50'] for k in range(1, E.PLAN_MAX_AGENTS + 1)]
        self.assertTrue(all(x >= y - 1e-9 for x, y in zip(ends, ends[1:])), ends)
        self.assertGreaterEqual(ends[-1], p['chain']['hours'] * .5)  # no agent count beats the chain by much
        a3 = p['agents'][3]
        self.assertAlmostEqual(a3['buffer'], a3['end']['p80'] - a3['end']['p50'])
        self.assertEqual(sorted(x for w in a3['waves'] for x in w['ids']), list('abcdef'))
        self.assertEqual(a3['waves'][0]['ids'][0], 'a')  # a chain card starts first
        dates = [w['p50'] for w in a3['waves']]
        self.assertEqual(dates, sorted(dates))
        self.assertLess(p['useful'], E.PLAN_MAX_AGENTS)
        self.assertEqual(p['blocked'], [{'id': 'b', 'label': 'b', 'waits_for': ['a']}, {'id': 'c', 'label': 'c', 'waits_for': ['b']}])

    def test_card_time_is_sp_times_rate_times_gap_plus_merge_wait(self):
        rows, commits = history()
        s = E.plan_samples(rows, cal_of(rows), commits)
        self.assertEqual(s['pairs'], [(1.0, 2.0)] * 8)
        self.assertAlmostEqual(s['gap'], (NOW - (T0 + timedelta(hours=196))).total_seconds() / 3600 / 32)
        p = self.plan(backlog([('1', 3, 5, '', '')]), agents=1)
        self.assertAlmostEqual(p['agents'][1]['end']['p50'], 3 * s['gap'] + 2)

    def test_lane_cap_slows_the_plan(self):
        text = backlog([('1', 2, 5, '', 'db'), ('2', 2, 5, '', 'db'), ('3', 2, 5, '', 'db')], '\n## Lanes\n- db (cap 1)\n')
        its, unsized = items(text)
        rows, commits = history()
        free = E.plan_forecast(its, unsized, rows, cal_of(rows), commits, {}, 3, runs=40)
        capped = E.plan_forecast(its, unsized, rows, cal_of(rows), commits, E.lane_caps(text)[0], 3, runs=40)
        self.assertGreater(capped['agents'][3]['end']['p50'], 2.5 * free['agents'][3]['end']['p50'])
        self.assertEqual(capped['lanes'], {'db': {'open': 1, 'merging': None}})

    def test_chain_uses_median_card_time_of_intact_pairs(self):
        # review r1: pairs (work/SP, wait) (0,10), (1,0), (10,1): a 1-SP card's median is 10 h, an 8-SP card's 10 h too
        pairs = [(0, 10), (1, 0), (10, 1)] * 2
        its, unsized = items(backlog([('A', 1, 5, '', ''), ('B', 1, 5, 'A', ''), ('C', 8, 5, '', '')]))
        from unittest.mock import patch
        with patch.object(E, 'plan_samples', return_value={'pairs': pairs, 'gap': 1.0}):
            p = E.plan_forecast(its, unsized, [], None, {}, {}, 1, runs=5)
        self.assertEqual((p['chain']['ids'], p['chain']['hours']), (['A', 'B'], 20))

    def test_no_dependencies_means_score_order_and_no_chain(self):
        # review r1: the longest card must not jump ahead of higher-Score work
        p = self.plan(backlog([('quick', 1, 8, '', ''), ('long', 8, 1, '', '')]), agents=1)
        first = min(p['agents'][1]['schedule'], key=lambda s: s['start'])
        self.assertEqual(first['id'], 'quick')
        self.assertEqual(p['chain']['ids'], [])
        self.assertIn('critical chain: none (no dependencies)', E.plan_lines(p, NOW))

    def test_max_useful_agents_is_never_claimed_past_the_search(self):
        # review r1: 60 equal free cards keep gaining from a 9th agent
        p = self.plan(backlog([(str(n), 1, 1, '', '') for n in range(60)]))
        self.assertIsNone(p['useful'])
        self.assertIn(f'max useful agents: over {E.PLAN_MAX_AGENTS - 1}', '\n'.join(E.plan_lines(p, NOW)))

    def test_single_commit_prs_still_scale_card_time_with_sp(self):
        # review r1: the last commit is the measured start, so start→merge is not all merge wait
        rows = [row(2, 200 + k * 6, 4.0, pr=k + 1) for k in range(8)]
        commits = {k + 1: [T0 + timedelta(hours=200 + k * 6 - 4)] for k in range(8)}
        self.assertEqual(E.plan_samples(rows, cal_of(rows), commits)['pairs'], [(2.0, 0.0)] * 8)

    def test_top_takes_the_first_cards_with_their_dependencies(self):
        p = self.plan(backlog([('1', 1, 1, '3', ''), ('2', 1, 8, '', ''), ('3', 1, 2, '', '')]), top=2)
        self.assertEqual(([c['id'] for c in p['cards']], p['of'], p['top']), (['2', '3'], 3, 2))

    def test_honest_states_no_depends_rough_maybe_done_no_history(self):
        text = TABLE.replace('| lane |', '| lane | note |').replace('|---|---|---|---|---|---|', '|---|---|---|---|---|---|---|') + \
            '| 1 | one | 1 | 1 |  |  | maybe-done: 1 PR |\n| 2 | two | 2 | 3 |  |  |  |\n'
        rows, commits = history(3)
        its, unsized = items(text)
        p = E.plan_forecast(its, unsized, rows, cal_of(rows), commits, {}, 3, runs=20)
        self.assertTrue(p['no_depends'] and p['rough'])
        self.assertEqual(p['maybe_done'], ['1'])
        out = '\n'.join(E.plan_lines(p, NOW))
        self.assertIn('no dependencies given, order is Score only', out)
        self.assertIn('3 measured PRs, rough', out)
        self.assertIn('1 of the planned cards may already be done', out)
        bare = E.plan_forecast(its, unsized, [], None, {}, {}, 3, runs=20)
        self.assertNotIn('end', bare['agents'][3])
        self.assertIn('no measured PRs yet', '\n'.join(E.plan_lines(bare, NOW)))

    def test_agents_line_names_what_one_more_agent_saves(self):
        plan = {'agents': {k: {'end': {'p50': h, 'p80': h + 48}} for k, h in ((3, 240.0), (4, 228.0), (5, 227.0))},
                'saves': {3: 12.0, 4: 1.0}}
        now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        self.assertEqual(E.agents_line(plan, 3, now, timezone.utc), '3 agents: done ~Nov 3 (p80 Nov 5); a 4th saves 0.5 d; a 5th saves nothing')
        self.assertEqual(E._a(8), 'an 8th')


class Cli(unittest.TestCase):
    def test_plan_text_and_json(self):
        rows, commits = history()
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d)
            store.append([{**{k: v for k, v in r.items() if k != 'actual'}, 'kind': 'estimate'} for r in rows])
            store.append([{**r['actual'], 'kind': 'actual', 'id': r['id']} for r in rows])
            store.write_json('history.json', [{'number': n, 'commits': [{'authoredDate': E.iso(t)} for t in ts]} for n, ts in commits.items()])
            store.write_json('config.json', {'lanes': {'db': {'open': 1}}})
            path = Path(d) / 'BACKLOG.md'
            path.write_text(backlog([('1', 3, 8, '', 'db'), ('2', 2, 5, '1', 'db'), ('3', 1, 1, '', '')]), encoding='utf-8')
            base = ['--dir', d, '--repo', 'o/r', '--now', E.iso(NOW), 'plan', '--backlog', str(path)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out): E.main(base)
            text = out.getvalue()
            for part in ('plan: 3 cards, 6 SP; 8 measured PRs', '3 agents: done ~', 'max useful agents:', 'project buffer, 3 agents:',
                         'critical chain: 2 cards, 5 SP', 'waves, 3 agents', 'blocked now: 1 card waits for others', 'lanes: db open ≤1'):
                self.assertIn(part, text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out): E.main(base + ['--agents', '4', '--json'])
            data = json.loads(out.getvalue())
            self.assertEqual(data['chosen'], 4)
            self.assertEqual(set(data['agents']['4']['cards']), {'1', '2', '3'})


class Ahead(unittest.TestCase):
    def test_section_shows_chain_waits_buffer_and_switch(self):
        rows, commits = history()
        its, unsized = items(backlog([('1', 3, 8, '', ''), ('2', 2, 5, '1', ''), ('3', 1, 1, '', '')]))
        cal = cal_of(rows)
        plan = E.plan_forecast(its, unsized, rows, cal, commits, {}, top=E.AHEAD_CARDS, runs=30)
        page = E.dashboard_html('o/r', rows, cal, its, NOW, plan=plan)
        self.assertIn('aria-label="Ahead"', page)
        for a in E.PARALLEL: self.assertIn(f'class="v ah{a}"', page)
        self.assertIn('#ag-3:checked~main .v.ah3{display:revert}', page)
        self.assertIn('class="card cc ', page)
        self.assertIn('waits for #1', page)
        self.assertIn('class="track buf"', page)
        self.assertIn('Ahead · next 3 cards', page)
        self.assertNotIn('aria-label="Ahead"', E.dashboard_html('o/r', rows, cal, its, NOW))


class TidenDepends(unittest.TestCase):
    ID = lambda n: f'00000000-0000-4000-8000-{n:012d}'  # noqa: E731
    A, B, C, D = ID(1), ID(2), ID(3), ID(4)

    def specs(self):
        return [{'id': self.A, 'title': 'KEY-05 — Media', 'seqNum': 225, 'status': 'Review', 'content': 'Effort: 3'},
                {'id': self.B, 'title': 'KEY-07 — Rollout', 'seqNum': 227, 'status': 'Review',
                 'content': 'Depends on: BL-34 (ACME-225) for the media part; owner authority. Related: ACME-228.'},
                {'id': self.C, 'title': 'Calendar', 'seqNum': 234, 'status': 'Backlog',
                 'content': 'Depends on: none for code. Live acceptance needs ACME-225.'},
                {'id': self.D, 'title': 'Keys', 'seqNum': 289, 'status': 'Backlog', 'content': 'Depends on: KEY-07, ACME-100 (delivered).'},
                {'id': 'ffffffff-0000-4000-8000-000000000100', 'title': 'Done one', 'seqNum': 100, 'status': 'Done', 'content': ''}]

    def test_depends_on_line_names_keys_and_sequence_refs_in_its_first_sentence(self):
        self.assertEqual(E.depends_refs('Depends on: none for code. Live needs ACME-237.\nDepends on: KEY-44, ACME-1; x'),
                         ['KEY-44', 'ACME-1'])
        cards = {c['id']: c['depends'] for c in E.tiden_cards(self.specs(), ('Backlog', 'Review'), 'ACME')}
        self.assertEqual(cards, {self.A: '', self.B: 'KEY-05', self.C: '', self.D: 'KEY-07'})
        self.assertEqual(E.tiden_cards(self.specs(), ('Backlog', 'Review'))[1]['depends'], '')  # no code: ACME-225 unknown

    def test_pull_fills_only_empty_depends_keeps_lane_and_sections(self):
        local = ('# Backlog\n\n| id | key | title | effort | value | depends | lane | status |\n|---|---|---|---|---|---|---|---|\n'
                 f'| {self.B} | KEY-07 | t | 1 | 2 |  | chat | Review |\n| {self.D} |  | t | 2 | 8 | - | gateway | Backlog |\n'
                 f'| {self.C} |  | t | 3 | 5 | KEY-05 |  | Backlog |\n\n## Lanes\n- chat (cap 1): KEY-07\n')
        text, _ = E.pull_tiden(self.specs(), local, None, NOW, ('Backlog', 'Review'), code='ACME')
        rows = E._local_fields(text)
        self.assertEqual((rows[self.B]['depends'], rows[self.D]['depends'], rows[self.C]['depends']), ('KEY-05', '-', 'KEY-05'))
        self.assertEqual((rows[self.B]['row']['lane'], rows[self.D]['row']['lane']), ('chat', 'gateway'))
        self.assertTrue(text.endswith('\n## Lanes\n- chat (cap 1): KEY-07\n'))
        self.assertEqual(E.lane_caps(text)[0], {'chat': {'open': 1, 'merging': None}})
        again, _ = E.pull_tiden(self.specs(), text, None, NOW, ('Backlog', 'Review'), code='ACME')
        self.assertEqual(again, text)  # a re-pull changes nothing

    def test_refs_from_card_text_use_current_keys_not_the_old_table(self):
        # review r1: A renamed KEY-1 -> KEY-2 and C KEY-2 -> KEY-4; B's text names KEY-2, which is A now
        specs = [{'id': self.A, 'title': 'KEY-2 — a', 'status': 'Backlog', 'content': ''},
                 {'id': self.B, 'title': 'b', 'status': 'Backlog', 'content': 'Depends on: KEY-2.'},
                 {'id': self.C, 'title': 'KEY-4 — c', 'status': 'Backlog', 'content': ''}]
        local = ('| id | key | title | effort | value | depends |\n|---|---|---|---|---|---|\n'
                 f'| {self.A} | KEY-1 | a | 1 | 1 |  |\n| {self.B} |  | b | 1 | 1 |  |\n| {self.C} | KEY-2 | c | 1 | 1 |  |\n'
                 '| KEY-2 |  | mine | 1 | 1 |  |\n')
        rows = E._local_fields(E.pull_tiden(specs, local, None, NOW, ('Backlog',))[0])
        self.assertEqual(rows[self.B]['depends'], self.A)  # the key is also a hand-written row's id: the full id

    def test_product_code_comes_from_the_read_only_product_list(self):
        listing = json.dumps({'products': [{'id': 'p-1', 'name': 'Acme', 'code': 'ACME'}]})
        import subprocess
        from unittest.mock import patch
        with patch.object(E.subprocess, 'run', lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, listing, '')):
            self.assertEqual(E.tiden_product('p-1', []), {'name': 'Acme', 'code': 'ACME'})


if __name__ == '__main__':
    unittest.main()
