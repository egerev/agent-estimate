"""Backlog file formats, Score and dependency-aware order. Synthetic fixtures only."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E  # noqa: E402

HEAD = '| id | title | effort | value | depends |\n|----|-------|--------|-------|---------|\n'


def table(*rows):
    return HEAD + ''.join(f'| {" | ".join(map(str, r))} |\n' for r in rows)


def order(text):
    out, warnings = E.order_backlog(E.parse_backlog(text))
    return [i['id'] for i in out], {i['id']: i.get('waits_for') for i in out}, warnings


class Parse(unittest.TestCase):
    def test_table_with_prose_and_extra_columns(self):
        text = ('# Backlog\nSome prose, 3 words.\n\n| # | Item | Cat | V | E | Score | Depends |\n|---|---|---|---|---|---|---|\n'
                '| 1 | Merge PR | defect | 5 | 2 | 2.5 | - |\n| 2 | Adapters | feature | 8 | 5 | 1.6 | 1, #3 |\n'
                '| 3 | Release | improvement | 5 | 2 | 2.5 | 3-4 |\n')
        items = E.parse_backlog(text)
        self.assertEqual([(i['id'], i['title'], i['sp'], i['value'], i['depends'], i['score']) for i in items],
                         [('1', 'Merge PR', 2, 5, [], 2.5), ('2', 'Adapters', 5, 8, ['1', '3'], 1.6),
                          ('3', 'Release', 2, 5, ['3', '4'], 2.5)])

    def test_score_has_two_decimals_and_missing_value_has_none(self):
        items = E.parse_backlog(table((1, 'a', 3, 2, ''), (2, 'b', 8, 5, ''), (3, 'c', 2, '', '')))
        self.assertEqual([i['score'] for i in items], [0.67, 0.62, None])

    def test_missing_id_column_numbers_rows(self):
        items = E.parse_backlog('| title | effort | value |\n|---|---|---|\n| a | 1 | 1 |\n| b | 2 | 3 |\n')
        self.assertEqual([i['id'] for i in items], ['1', '2'])

    def test_invalid_table_cells_are_rejected(self):
        for row in ((1, 'a', '', 3, ''), (1, 'a', 4, 3, ''), (1, 'a', 3, 4, ''), (1, 'a', 3, 13, ''), (1, 'a', 3, 'high', '')):
            with self.subTest(row=row), self.assertRaises(ValueError): E.parse_backlog(table(row))
        with self.assertRaises(ValueError): E.parse_backlog('| id | name |\n|---|---|\n| 1 | a |\n')  # no backlog header: legacy lines
        self.assertEqual(E.parse_backlog(table((1, 'big', 13, 5, '')))[0]['sp'], 13)  # kept for the split warning

    def test_escaped_pipe_stays_in_the_title(self):
        item = E.parse_backlog(HEAD + '| 1 | Parse a \\| b | 3 | 5 | |\n')[0]
        self.assertEqual((item['id'], item['title'], item['sp'], item['value']), ('1', 'Parse a | b', 3, 5))

    def test_empty_leading_cells_keep_their_columns(self):
        item = E.parse_backlog(HEAD + '||draft|3|5||\n')[0]
        self.assertEqual((item['id'], item['title'], item['sp'], item['value'], item['depends']), ('1', 'draft', 3, 5, []))

    def test_only_the_table_under_a_backlog_header_is_read(self):
        text = ('| Metric | Note |\n|---|---|\n| 3 | unrelated |\n\n' + HEAD + '| 1 | a | 2 | 3 | |\n'
                + '\n| other | 5 |\n')
        self.assertEqual([(i['id'], i['title']) for i in E.parse_backlog(text)], [('1', 'a')])

    def test_legacy_lines_starting_with_a_pipe_still_parse(self):
        items = E.parse_backlog('| Rewrite sync | 5\n3 Add export\n')
        self.assertEqual([(i['sp'], i['title']) for i in items], [(5, 'Rewrite sync'), (3, 'Add export')])

    def test_line_format_still_parses_without_score(self):
        items = E.parse_backlog('# comment\n3 Add export\nRewrite sync | 5\n')
        self.assertEqual([(i['id'], i['sp'], i['title'], i['value'], i['score'], i['depends']) for i in items],
                         [('1', 3, 'Add export', None, None, []), ('2', 5, 'Rewrite sync', None, None, [])])


class Order(unittest.TestCase):
    def test_score_desc_with_file_order_ties_and_unscored_last(self):
        ids, waits, warnings = order(table((1, 'a', 3, 3, ''), (2, 'none', 1, '', ''), (3, 'b', 1, 3, ''),
                                           (4, 'c', 2, 2, ''), (5, 'd', 1, 1, '')))
        self.assertEqual(ids, ['3', '1', '4', '5', '2'])  # equal Scores keep file order
        self.assertEqual(set(waits.values()), {None}); self.assertEqual(warnings, [])

    def test_item_never_precedes_its_dependency_and_says_why(self):
        ids, waits, _ = order(table((1, 'api', 5, 2, ''), (2, 'ui', 1, 8, 1), (3, 'other', 2, 3, '')))
        self.assertEqual(ids, ['3', '1', '2'])
        self.assertEqual(waits, {'3': None, '1': None, '2': '1'})

    def test_waits_for_names_the_last_placed_dependency(self):
        ids, waits, _ = order(table((1, 'x', 5, 1, ''), (2, 'y', 2, 2, ''), (3, 'z', 1, 8, '1, 2')))
        self.assertEqual(ids, ['2', '1', '3']); self.assertEqual(waits['3'], '1')

    def test_transitive_dependencies(self):
        ids, waits, _ = order(table((1, 'c', 1, 8, 2), (2, 'b', 1, 5, 3), (3, 'a', 8, 1, '')))
        self.assertEqual(ids, ['3', '2', '1']); self.assertEqual(waits, {'3': None, '2': None, '1': '2'})  # #2 kept its Score rank

    def test_unknown_id_warns_and_keeps_file_order(self):
        ids, waits, warnings = order(table((1, 'a', 2, 2, 9), (2, 'b', 1, 3, ''), (3, 'c', 1, 8, 1)))
        self.assertEqual(ids, ['2', '1', '3']); self.assertIsNone(waits['1'])
        self.assertEqual(warnings, ['#1 depends on unknown #9: kept in file order',
                                    'dependency cycle (or waiting on an unresolved item): #3 kept in file order'])

    def test_cycle_warns_names_items_and_keeps_file_order(self):
        ids, waits, warnings = order(table((1, 'low', 3, 1, 3), (2, 'free', 1, 2, ''), (3, 'high', 1, 8, 1),
                                           (4, 'after', 1, 5, 3), (5, 'self', 1, 1, 5)))
        self.assertEqual(ids, ['2', '1', '3', '4', '5'])
        self.assertEqual(warnings, ['dependency cycle (or waiting on an unresolved item): #1, #3, #4, #5 kept in file order'])
        self.assertEqual(set(waits.values()), {None})


U1, U2, U3 = ('a1b2c3d4-0000-4000-8000-000000000001', 'ffee0011-0000-4000-8000-000000000002',
              'ffee0011-9999-4000-8000-000000000003')  # U2 and U3 share their first 8 characters


class ShortIds(unittest.TestCase):
    def test_unique_uuid_prefix_or_key_is_the_label_and_the_full_id_stays(self):
        items = E.parse_backlog('| id | key | title | effort | value | depends |\n|---|---|---|---|---|---|\n'
                                f'| {U1} | | a | 2 | 3 | |\n| {U2} | | b | 1 | 3 | |\n| {U3} | KEY-21 | c | 1 | 3 | |\n| 7 | | d | 1 | 1 | |\n')
        self.assertEqual([(i['id'], i['label']) for i in items], [(U1, 'a1b2c3d4'), (U2, U2), (U3, 'KEY-21'), ('7', '7')])

    def test_depends_accepts_prefix_key_and_any_case(self):
        items = E.parse_backlog('| id | key | title | effort | value | depends |\n|---|---|---|---|---|---|\n'
                                f'| {U1} | | a | 2 | 3 | |\n| {U3} | KEY-21 | c | 1 | 3 | |\n'
                                f'| {U2} | | b | 1 | 3 | A1B2C3D4, key-21 |\n| 4 | | d | 1 | 1 | {U3[:13]} |\n')
        self.assertEqual(items[2]['depends'], [U1, U3])
        self.assertEqual(items[3]['depends'], [U3])                            # a longer prefix that is unique

    def test_exact_id_wins_over_case_folding_and_case_collisions_are_rejected(self):
        items = E.parse_backlog(table(('A', 'upper', 1, 1, ''), ('a', 'lower', 8, 1, ''), (3, 'x', 1, 3, 'A'), (4, 'y', 1, 3, 'a')))
        self.assertEqual((items[2]['depends'], items[3]['depends']), (['A'], ['a']))
        items = E.parse_backlog(table(('A', 'upper', 1, 1, ''), ('a', 'lower', 8, 1, ''), ('B', 'b', 1, 1, ''), (4, 'y', 1, 3, 'b')))
        self.assertEqual(items[3]['depends'], ['B'])                           # one case-insensitive match
        _, _, warnings = order(table(('Ab', 'one', 1, 1, ''), ('aB', 'two', 1, 1, ''), (3, 'z', 1, 3, 'ab')))
        self.assertEqual(warnings, ['#3 depends on unknown #ab: kept in file order'])  # ambiguous: never guessed

    def test_ambiguous_or_short_prefix_is_an_unknown_dependency(self):
        text = table((U1, 'a', 2, 3, ''), (U2, 'b', 1, 3, ''), (U3, 'c', 1, 3, ''), (4, 'd', 1, 1, 'ffee0011'), (5, 'e', 1, 1, 'a1b2c3'))
        _, _, warnings = order(text)
        self.assertEqual(warnings, ['#4 depends on unknown #ffee0011: kept in file order',
                                    '#5 depends on unknown #a1b2c3: kept in file order'])


class Unblocks(unittest.TestCase):
    def test_item_placed_before_higher_score_work_that_needs_it_says_so(self):
        out, _ = E.order_backlog(E.parse_backlog(table((1, 'api', 5, 2, ''), (2, 'ui', 1, 8, 1), (3, 'other', 2, 3, ''),
                                                       (4, 'low', 5, 1, 1))))
        marks = {i['id']: (i.get('unblocks'), i.get('waits_for')) for i in out}
        self.assertEqual(marks['1'], (['2'], None))       # #4 also depends on #1 but has a lower Score
        self.assertEqual(marks['2'], (None, '1'))
        self.assertEqual(marks['3'], (None, None))

    def test_transitive_prerequisite_names_the_work_it_unblocks(self):
        # C needs B, B needs A; Scores 3, 0.125, 8: A rises to first only because C waits on the chain
        out, _ = E.order_backlog(E.parse_backlog(table(('A', 'a', 1, 3, ''), ('B', 'b', 8, 1, 'A'), ('C', 'c', 1, 8, 'B'))))
        self.assertEqual([(i['id'], i.get('unblocks'), i.get('waits_for')) for i in out],
                         [('A', ['C'], None), ('B', ['C'], None), ('C', None, 'B')])

    def test_dependency_already_above_its_dependent_has_no_mark(self):
        out, _ = E.order_backlog(E.parse_backlog(table((1, 'base', 1, 8, ''), (2, 'next', 1, 3, 1))))
        self.assertEqual([i.get('unblocks') for i in out], [None, None])


class ForecastCLI(unittest.TestCase):
    def run_cli(self, d, *command):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            E.main(['--repo', 'owner/repo', '--dir', d, '--now', '2026-10-01T00:00:00Z', 'forecast', *command])
        return out.getvalue()

    def test_text_and_json_output(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'backlog.md'
            f.write_text(table((1, 'api', 5, 2, ''), (2, 'ui', 1, 8, 1), (3, 'huge', 13, 5, ''), (4, 'x', 2, 2, 7)))
            text = self.run_cli(d, '--backlog', str(f))
            self.assertIn('backlog: 4 items, 21 SP', text)
            self.assertIn('split first: 13 SP huge', text)
            self.assertRegex(text, r'2\. #2\s+8\.00\s+V8 E1\s+ui\s+waits for #1')
            self.assertIn('warning: #4 depends on unknown #7: kept in file order', text)
            data = json.loads(self.run_cli(d, '--backlog', str(f), '--json'))
            self.assertEqual(data['sp'], 21)
            self.assertEqual([i['id'] for i in data['order']], ['1', '2', '3', '4'])
            self.assertEqual(data['warnings'], ['#4 depends on unknown #7: kept in file order'])

    def test_text_output_uses_short_ids_and_order_marks(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'backlog.md'
            f.write_text(table((U1, 'api', 5, 2, ''), (U2, 'ui', 1, 8, 'a1b2c3d4'), (3, 'other', 2, 3, '')))
            text = self.run_cli(d, '--backlog', str(f))
            self.assertRegex(text, r'2\. #a1b2c3d4\s+0\.40\s+V2 E5\s+api\s+unblocks #ffee0011\n')
            self.assertRegex(text, r'3\. #ffee0011\s+8\.00\s+V8 E1\s+ui\s+waits for #a1b2c3d4\n')
            data = json.loads(self.run_cli(d, '--backlog', str(f), '--json'))
            self.assertEqual([(i['id'], i['label'], i.get('unblocks')) for i in data['order']],
                             [('3', '3', None), (U1, 'a1b2c3d4', [U2]), (U2, 'ffee0011', None)])
            self.assertEqual(data['order'][2]['depends'], [U1])

    def test_example_files_parse(self):
        root = Path(__file__).resolve().parents[1] / 'examples'
        self.assertTrue(all(i['score'] is not None for i in E.parse_backlog((root / 'backlog.example.md').read_text())))
        self.assertTrue(E.parse_backlog((root / 'backlog.example.txt').read_text()))


if __name__ == '__main__': unittest.main()
