"""Backlog sources: config, Markdown path, read-only Tiden pull. The tiden subprocess is always mocked."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E  # noqa: E402

FIXTURE = (Path(__file__).parent / 'fixtures' / 'tiden_specs.json').read_text(encoding='utf-8')
SPECS = json.loads(FIXTURE)['requirements']
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
ID = lambda n: f'00000000-0000-4000-8000-{n:012d}'  # noqa: E731
BR = ('Backlog', 'Review')  # the fixture's open statuses, also the default


def fake_run(root, tiden=None, calls=None):
    """subprocess.run stand-in: `git rev-parse` answers `root`; `tiden` answers `tiden` (or is missing)."""
    def run(cmd, **kw):
        if calls is not None: calls.append((cmd, kw))
        if cmd[0] == 'git': return subprocess.CompletedProcess(cmd, 0, f'{root}\n', '')
        if tiden is None: raise FileNotFoundError(cmd[0])
        return tiden(cmd)
    return run


def ok(stdout=FIXTURE):
    return lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout, '')


class Config(unittest.TestCase):
    def test_missing_config_is_markdown_backlog_at_git_root(self):
        with tempfile.TemporaryDirectory() as d, patch.object(E, 'git_root', return_value=d):
            cfg = E.backlog_config(E.Store(Path(d) / '.estimate'))
        self.assertEqual((cfg['source'], cfg['path']), ('markdown', Path(d) / 'BACKLOG.md'))

    def test_markdown_path_is_relative_to_git_root_and_tiden_uses_the_pulled_file(self):
        with tempfile.TemporaryDirectory() as d, patch.object(E, 'git_root', return_value=d):
            store = E.Store(Path(d) / '.estimate')
            store.write_json('config.json', {'since': None, 'backlog': {'source': 'markdown', 'path': 'docs/todo.md'}})
            self.assertEqual(E.backlog_config(store)['path'], Path(d) / 'docs' / 'todo.md')
            store.write_json('config.json', {'backlog': {'source': 'tiden', 'product_id': 'p-1'}})
            cfg = E.backlog_config(store)
            self.assertEqual((cfg['source'], cfg['product_id'], cfg['path']), ('tiden', 'p-1', store.dir / 'backlog.md'))
            store.write_json('config.json', {'backlog': {'source': 'tiden'}})
            self.assertIsNone(E.backlog_config(store)['product_id'])  # the linked repo decides

    def test_bad_config_shapes_give_one_clear_error(self):
        for raw in ('[1]', '{"backlog": "BACKLOG.md"}', '{"backlog": {"source": "markdown", "path": 3}}',
                    '{"backlog": {"source": "tiden", "product_id": ["p"]}}', '{"backlog": {"source": 1}}', '{not json'):
            with self.subTest(raw), tempfile.TemporaryDirectory() as d:
                (Path(d) / 'config.json').write_text(raw, encoding='utf-8')
                with self.assertRaises(ValueError) as err: E.backlog_config(E.Store(d))
                self.assertIn('expected {"backlog"', str(err.exception))

    def test_unknown_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d); store.write_json('config.json', {'backlog': {'source': 'linear'}})
            with self.assertRaises(ValueError): E.backlog_config(store)


class TidenCards(unittest.TestCase):
    def test_open_leaf_cards_with_scale_values_and_known_dependencies(self):
        cards = {c['id']: c for c in E.tiden_cards(SPECS, BR)}
        self.assertEqual(list(cards), [ID(2), ID(3), ID(4)])  # epic with open children, Active and Done are out
        self.assertEqual({k: cards[ID(2)][k] for k in ('effort', 'value', 'depends', 'status')},
                         {'effort': '3', 'value': '5', 'depends': '', 'status': 'Backlog'})
        self.assertEqual((cards[ID(3)]['effort'], cards[ID(3)]['value'], cards[ID(3)]['depends']), ('', '3', ID(2)))
        self.assertEqual((cards[ID(4)]['effort'], cards[ID(4)]['depends']), ('', ''))  # 22 is off scale; #5 is delivered

    def test_container_without_open_children_is_still_not_a_card(self):
        specs = [s for s in SPECS if s['id'] not in (ID(2), ID(3))]  # childrenCount 2: its children are the work
        self.assertEqual([c['id'] for c in E.tiden_cards(specs, BR)], [ID(4)])

    def test_linked_pr_or_code_marks_an_open_card_maybe_done(self):
        specs = [{**s, 'sources': [{'sourceType': 'pull_request'}, {'sourceType': 'repo_file'}, {'sourceType': 'repo_file'}]}
                 if s['id'] == ID(2) else {**s, 'sources': [{'sourceType': 'github_local_id'}]} if s['id'] == ID(4) else s
                 for s in SPECS]
        notes = {c['id']: c['note'] for c in E.tiden_cards(specs, BR)}
        self.assertEqual(notes, {ID(2): 'maybe-done: 1 PR, 2 code anchors', ID(3): '', ID(4): ''})  # an id locator is not code


class Pull(unittest.TestCase):
    def test_first_pull_writes_a_table_the_backlog_parser_reads(self):
        text, notes = E.pull_tiden(SPECS, '', 'p-1', NOW, BR)
        self.assertEqual(notes, [f'left out 1 container card(s) with child cards, never sized or counted (their children are the work): {ID(1)}'])
        self.assertIn('Left out 1 container card(s)', text)
        self.assertIn('Audit log table \\| write path', text)
        self.assertEqual(list(E._local_fields(text)), [ID(2), ID(3), ID(4)])
        with self.assertRaises(ValueError): E.parse_backlog(text)  # unsized cards must be sized first
        unsized = []; sized = E.parse_backlog(text, unsized)     # the dashboard lists them apart instead
        self.assertEqual(([i['id'] for i in sized], [u['id'] for u in unsized]), ([ID(2)], [ID(3), ID(4)]))

    def test_header_names_the_product_and_the_pull_time(self):
        text, _ = E.pull_tiden(SPECS, '', 'p-1', NOW, BR, product_name='Shop')
        self.assertTrue(text.startswith('# Backlog from Tiden product Shop (p-1)\n'))
        self.assertEqual(E.pulled_source(text), ('Shop', NOW))
        self.assertEqual(E.pulled_source(E.pull_tiden(SPECS, '', 'p-1', NOW, BR)[0]), (None, NOW))  # name unknown

    def test_product_name_is_read_only_and_never_fails_the_pull(self):
        listing = json.dumps({'products': [{'id': 'p-0', 'name': 'other'}, {'id': 'p-1', 'name': ' Deed\nKit '}]})
        calls = []
        with patch.object(E.subprocess, 'run', fake_run('/r', ok(listing), calls)):
            self.assertEqual(E.tiden_product_name('p-1', []), 'Deed Kit')
            self.assertEqual(E.tiden_product_name(None, [{'productId': 'p-0'}]), 'other')
            self.assertIsNone(E.tiden_product_name('p-9', []))
        self.assertEqual(calls[0][0], ['tiden', 'product', 'list', '--format', 'json'])
        with patch.object(E.subprocess, 'run', fake_run('/r', None)):
            self.assertIsNone(E.tiden_product_name('p-1', []))
        with patch.object(E.subprocess, 'run', fake_run('/r', ok('not json'))):
            self.assertIsNone(E.tiden_product_name('p-1', []))

    def test_repull_keeps_local_cells_even_empty_and_drops_cards_gone_from_tiden(self):
        local = ('| id | title | effort | value | depends |\n|---|---|---|---|---|\n'
                 f'| {ID(3)} | old title | 2 | 5 | {ID(2)} |\n| {ID(4)} | Export | 5 | | |\n| {ID(9)} | gone | 1 | 1 | |\n')
        text, notes = E.pull_tiden(SPECS, local, 'p-1', NOW, BR)
        items = {i['id']: i for i in E.parse_backlog(text)}
        self.assertEqual(list(items), [ID(2), ID(3), ID(4)])
        self.assertEqual((items[ID(3)]['title'], items[ID(3)]['sp'], items[ID(3)]['value'], items[ID(3)]['depends']),
                         ('Audit log viewer', 2, 5, [ID(2)]))     # local wins; title follows Tiden
        self.assertEqual((items[ID(4)]['sp'], items[ID(4)]['value']), (5, None))  # a cleared value stays cleared
        self.assertEqual((items[ID(2)]['sp'], items[ID(2)]['value']), (3, 5))     # a new row takes the card's values
        self.assertEqual(notes, [f'dropped 1 card(s) no longer in Tiden: {ID(9)}', f'left out 1 container card(s) with child cards, never sized or counted (their children are the work): {ID(1)}', 'added 1 new card(s)'])

    def test_repeat_pull_keeps_hand_written_rows_with_their_cells(self):
        text, _ = E.pull_tiden(SPECS, '', 'p-1', NOW, BR)
        manual = '| 1 | OPS-437 | KEY-17 part F \\| sync | 3 | 2 | KEY-9 | Backlog | Low | User Story |  |\n'
        text = text + manual
        for _ in range(2):  # pull twice: the row survives every pull, cells untouched
            text, notes = E.pull_tiden(SPECS, text, 'p-1', NOW, BR)
            self.assertIn(manual, text)
            self.assertIn('kept 1 hand-written row(s) without a Tiden id: 1', notes)
            self.assertFalse(any('no longer in Tiden' in n for n in notes))
        unsized = []
        row = next(i for i in E.parse_backlog(text, unsized) if i['id'] == '1')
        self.assertEqual((row['sp'], row['value'], row['key'], row['title'], row['label']), (3, 2, 'OPS-437', 'KEY-17 part F | sync', '1'))
        gone = text.replace(manual, f'| {ID(9)} |  | deleted card | 1 | 1 |  | Backlog |  |  |  |\n')
        self.assertEqual(E.pull_tiden(SPECS, gone, 'p-1', NOW, BR)[1][0], f'dropped 1 card(s) no longer in Tiden: {ID(9)}')

    def test_hand_written_duplicates_extra_columns_and_spaces_survive(self):
        local = ('| id | key | title | effort | value | depends | owner |\n|---|---|---|---|---|---|---|\n'
                 f'| {ID(2)} |  | old | 3 | 5 |  | ann |\n| 1 |  | printf "a  b" | 2 | 3 |  | bob |\n| 1 |  | twin | 1 | 1 |  | |\n')
        text, notes = E.pull_tiden(SPECS, local, 'p-1', NOW, BR)
        self.assertIn('kept 2 hand-written row(s) without a Tiden id: 1, 1', notes)
        self.assertIn('| type | note | owner |', text)
        self.assertIn(f'| {ID(2)} |  | Audit log table \\| write path | 3 | 5 |', text)
        self.assertRegex(text, r'\| User Story \|  \| ann \|\n')  # a card keeps its hand-added cell
        self.assertIn('| 1 |  | printf "a  b" | 2 | 3 |  |  |  |  |  | bob |', text)
        self.assertIn('| 1 |  | twin | 1 | 1 |  |  |  |  |  |  |', text)

    def test_hand_written_id_equal_to_a_card_key_never_captures_its_dependents(self):
        specs = [{**s, 'key': 'KEY-2'} if s['id'] == ID(2) else s for s in SPECS]
        local = ('| id | key | title | effort | value | depends |\n|---|---|---|---|---|---|\n'
                 f'| {ID(3)} |  | viewer | 2 | 3 | {ID(2)} |\n| KEY-2 |  | mine | 1 | 1 | {ID(2)} |\n')
        text, _ = E.pull_tiden(specs, local, 'p-1', NOW, BR)
        items = {i['id']: i for i in E.parse_backlog(text, [])}
        self.assertEqual((items[ID(3)]['depends'], items['KEY-2']['depends']), ([ID(2)], [ID(2)]))

    def test_cleared_depends_is_not_refilled_from_the_card(self):
        # A cell cleared by hand is `-` (none); an empty cell is not filled yet and takes the card's Depends on line.
        local = f'| id | title | effort | value | depends |\n|---|---|---|---|---|\n| {ID(3)} | v | 2 | 3 | - |\n'
        self.assertEqual(E._local_fields(E.pull_tiden(SPECS, local, None, NOW, BR)[0])[ID(3)]['depends'], '-')  # card says #2

    def test_delivered_cards_leave_depends_and_unknown_ids_stay(self):
        local = ('| id | title | effort | value | depends |\n|---|---|---|---|---|\n'
                 f'| {ID(3)} | v | 2 | 3 | {ID(2)}, {ID(5)}, {ID(9)} |\n| {ID(6)} | old import | 1 | 1 | |\n')
        text, notes = E.pull_tiden(SPECS, local, None, NOW, BR)
        self.assertEqual(E._local_fields(text)[ID(3)]['depends'], f'{ID(2)}, {ID(9)}')  # #5 is Active
        self.assertEqual(notes, [f'dropped 1 delivered card(s) (Active/Done), also removed from depends: {ID(6)}',
                                 f'left out 1 container card(s) with child cards, never sized or counted (their children are the work): {ID(1)}', 'added 2 new card(s)'])
        self.assertIn(f'depends on unknown #{ID(9)}', ' '.join(E.order_backlog(E.parse_backlog(
            text.replace(f'| {ID(4)} |  | Export to CSV |  | 3 |', f'| {ID(4)} |  | Export to CSV | 2 | 3 |')))[1]))


class Statuses(unittest.TestCase):
    def test_default_pulls_backlog_and_review(self):
        self.assertEqual(E.TIDEN_OPEN, BR)
        self.assertEqual([c['id'] for c in E.tiden_cards(SPECS)], [ID(2), ID(3), ID(4)])  # #3 is in Review
        self.assertIn('(open cards: status Backlog or Review)', E.pull_tiden(SPECS, '', None, NOW)[0])

    def test_narrower_statuses_explain_dropped_cards(self):
        self.assertEqual([c['id'] for c in E.tiden_cards(SPECS, ('Backlog',))], [ID(2), ID(4)])
        local = f'| id | title | effort | value | depends |\n|---|---|---|---|---|\n| {ID(3)} | v | 2 | 3 | |\n'
        text, notes = E.pull_tiden(SPECS, local, None, NOW, ('Backlog',))
        self.assertIn('(open cards: status Backlog)', text)
        self.assertEqual(notes[0], f'dropped 1 card(s) not in status Backlog: {ID(3)}')

    def test_config_statuses(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(d)
            store.write_json('config.json', {'backlog': {'source': 'tiden'}})
            self.assertEqual(E.backlog_config(store)['statuses'], BR)
            store.write_json('config.json', {'backlog': {'source': 'tiden', 'statuses': ['Backlog', 'Review', 'Review']}})
            self.assertEqual(E.backlog_config(store)['statuses'], ('Backlog', 'Review'))
            for bad in ('Backlog', [], [1], ['Backlog', 'Done']):
                store.write_json('config.json', {'backlog': {'source': 'tiden', 'statuses': bad}})
                with self.subTest(bad=bad), self.assertRaises(ValueError) as err: E.backlog_config(store)
                self.assertIn('backlog.statuses', str(err.exception))


class Keys(unittest.TestCase):
    A, B, C = 'aaaa1111-0000-4000-8000-000000000001', 'bbbb2222-0000-4000-8000-000000000002', 'cccc3333-0000-4000-8000-000000000003'

    def specs(self):
        return [{'id': self.A, 'title': 'KEY-21 — Checkout retries', 'status': 'Backlog', 'content': ''},
                {'id': self.B, 'title': 'Viewer', 'key': 'BL-7', 'status': 'Backlog', 'content': f'Depends on: {self.A}, {self.C}'},
                {'id': self.C, 'title': 'Export', 'status': 'Backlog', 'content': f'Depends on: {self.B}'},
                {'id': 'dddd4444-0000-4000-8000-000000000004', 'title': 'Login', 'status': 'Done', 'content': ''}]

    def test_key_column_and_depends_by_label(self):
        text, _ = E.pull_tiden(self.specs(), '', None, NOW)
        self.assertIn(f'| {self.A} | KEY-21 | KEY-21 — Checkout retries |', text)
        cards = E._local_fields(text)
        self.assertEqual((cards[self.B]['depends'], cards[self.C]['depends']), ('KEY-21, cccc3333', 'BL-7'))
        items = {i['id']: i for i in E.parse_backlog(text.replace(' |  |  |  | Backlog', ' | 1 | 1 |  | Backlog')
                                                     .replace('|  |  | KEY-21, cccc3333 |', '| 2 | 3 | KEY-21, cccc3333 |')
                                                     .replace('|  |  | BL-7 |', '| 1 | 2 | BL-7 |'))}
        self.assertEqual(items[self.B]['depends'], [self.A, self.C])      # stored ids resolve back to full ones
        self.assertEqual((items[self.A]['label'], items[self.C]['label']), ('KEY-21', 'cccc3333'))

    def test_renamed_key_keeps_the_dependency_edge(self):
        first, _ = E.pull_tiden(self.specs(), '', None, NOW)
        self.assertEqual(E._local_fields(first)[self.B]['depends'], 'KEY-21, cccc3333')
        renamed = [{**s, 'title': 'KEY-1 — Checkout retries'} if s['id'] == self.A else s for s in self.specs()]
        text, _ = E.pull_tiden(renamed, first, None, NOW)
        self.assertEqual(E._local_fields(text)[self.B]['depends'], 'KEY-1, cccc3333')
        self.assertIn(f'| {self.A} | KEY-1 | KEY-1 — Checkout retries |', text)

    def test_short_refs_to_delivered_cards_leave_depends(self):
        local = f'| id | title | effort | value | depends |\n|---|---|---|---|---|\n| {self.C} | Export | 2 | 3 | dddd4444, BL-7 |\n'
        text, _ = E.pull_tiden(self.specs(), local, None, NOW)
        self.assertEqual(E._local_fields(text)[self.C]['depends'], 'BL-7')


class TidenProcess(unittest.TestCase):
    def test_runs_read_only_spec_list_with_the_configured_product(self):
        calls = []
        with patch.object(E.subprocess, 'run', side_effect=fake_run('/r', ok(), calls)):
            self.assertEqual(len(E.run_tiden('p-1', cwd='/r')), 6)
            E.run_tiden(None)
        self.assertEqual([c[0] for c in calls], [['tiden', 'spec', 'list', '--format', 'json', '--include-sources', '--page-size', '100', '--product-id', 'p-1'],
                                                 ['tiden', 'spec', 'list', '--format', 'json', '--include-sources', '--page-size', '100']])

    def test_an_older_tiden_without_include_sources_is_asked_again_without_it(self):
        calls = []
        old = lambda cmd: (subprocess.CompletedProcess(cmd, 1, '', 'Error: unknown flag: --include-sources')  # noqa: E731
                           if '--include-sources' in cmd else subprocess.CompletedProcess(cmd, 0, FIXTURE, ''))
        with patch.object(E.subprocess, 'run', side_effect=fake_run('/r', old, calls)):
            self.assertEqual(len(E.run_tiden('p-1')), 6)
        self.assertEqual(['--include-sources' in c[0] for c in calls], [True, False])

    def test_missing_unauthorized_or_garbled_tiden_is_a_clear_error(self):
        cases = {'not installed': None,
                 'tiden setup': lambda c: subprocess.CompletedProcess(c, 2, '', 'error: authentication failed\n'),
                 'did not return JSON': ok('not json'), 'no "requirements"': ok('{"items": []}')}
        for needle, tiden in cases.items():
            with self.subTest(needle), patch.object(E.subprocess, 'run', side_effect=fake_run('/r', tiden)):
                with self.assertRaises(RuntimeError) as err: E.run_tiden('p-1')
                self.assertIn(needle, str(err.exception))


def paged(fail_at=None, total=6, size=2):
    """tiden stand-in serving SPECS in pages of `size`; page `fail_at` exits non-zero."""
    def tiden(cmd):
        n = int(cmd[cmd.index('--page-token') + 1]) if '--page-token' in cmd else 0
        if n == fail_at: return subprocess.CompletedProcess(cmd, 1, '', 'error: server unavailable')
        nxt = str(n + 1) if (n + 1) * size < len(SPECS) else ''
        body = {'requirements': SPECS[n * size:(n + 1) * size], 'pagination': {'nextPageToken': nxt, 'totalCount': total}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), '')
    return tiden


class Paging(unittest.TestCase):
    def test_every_page_is_fetched(self):
        calls = []
        with patch.object(E.subprocess, 'run', side_effect=fake_run('/r', paged(), calls)):
            self.assertEqual([s['id'] for s in E.run_tiden('p-1')], [s['id'] for s in SPECS])
        self.assertEqual([c[0][-2:] for c in calls[1:]], [['--page-token', '1'], ['--page-token', '2']])

    def test_failed_or_short_listing_fails_whole(self):
        for tiden, needle in ((paged(fail_at=2), 'server unavailable'), (paged(total=7), '6 of 7 specs')):
            with self.subTest(needle), patch.object(E.subprocess, 'run', side_effect=fake_run('/r', tiden)):
                with self.assertRaises(RuntimeError) as err: E.run_tiden('p-1')
                self.assertIn(needle, str(err.exception))


class AtomicWrite(unittest.TestCase):
    def test_unique_temp_file_is_removed_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'backlog.md'; path.write_text('old', encoding='utf-8')
            with patch.object(E.os, 'replace', side_effect=OSError('disk full')), self.assertRaises(OSError):
                E.write_atomic(path, 'new')
            self.assertEqual((sorted(p.name for p in Path(d).iterdir()), path.read_text(encoding='utf-8')), (['backlog.md'], 'old'))
            E.write_atomic(path, 'new')
            self.assertEqual((sorted(p.name for p in Path(d).iterdir()), path.read_text(encoding='utf-8')), (['backlog.md'], 'new'))


class CLI(unittest.TestCase):
    def cli(self, root, *command, tiden=None, repo=True):
        out = io.StringIO(); cfg = Path(root) / '.estimate' / 'config.json'
        if command[0] == 'forecast' and not cfg.exists(): E.Store(cfg.parent).write_json('config.json', {})   # a set-up data dir
        with patch.object(E.subprocess, 'run', side_effect=fake_run(root, tiden)), contextlib.redirect_stdout(out):
            E.main([*(['--repo', 'owner/repo'] if repo else []), '--now', E.iso(NOW), *command])
        return out.getvalue()

    def test_pull_then_repull_through_the_cli(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(Path(d) / '.estimate'); store.write_json('config.json', {'backlog': {'source': 'tiden', 'statuses': list(BR)}})
            out = self.cli(d, 'backlog', 'pull', tiden=ok(), repo=False)
            self.assertIn('3 open card(s) of 6 specs', out)
            path = store.dir / 'backlog.md'
            path.write_text(path.read_text(encoding='utf-8').replace(f'| {ID(4)} |  | Export to CSV |  |',
                                                                     f'| {ID(4)} |  | Export to CSV | 8 |'), encoding='utf-8')
            gone = json.dumps({'requirements': [s for s in SPECS if s['id'] != ID(2)]})
            out = self.cli(d, 'backlog', 'pull', tiden=ok(gone), repo=False)
            self.assertIn(f'note: dropped 1 card(s) no longer in Tiden: {ID(2)}', out)
            self.assertEqual(E._local_fields(path.read_text(encoding='utf-8'))[ID(4)]['effort'], '8')

    def test_pull_errors_exit_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(Path(d) / '.estimate')
            with self.assertRaises(SystemExit) as err: self.cli(d, 'backlog', 'pull', repo=False)
            self.assertIn('pull reads Tiden', str(err.exception.code))          # default source is Markdown
            store.write_json('config.json', {'backlog': {'source': 'tiden'}})
            with self.assertRaises(SystemExit) as err: self.cli(d, 'backlog', 'pull', repo=False)
            self.assertIn('`tiden` is not installed', str(err.exception.code))
            self.assertFalse((store.dir / 'backlog.md').exists())

    def test_custom_data_dir_needs_a_product_id(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            store = E.Store(data); store.write_json('config.json', {'backlog': {'source': 'tiden'}})
            (Path(data) / 'backlog.md').write_text('keep me', encoding='utf-8')
            calls = []
            with self.assertRaises(SystemExit) as err:
                self.cli(root, '--dir', data, 'backlog', 'pull', tiden=lambda c: calls.append(c) or ok()(c), repo=False)
            self.assertIn('Set backlog.product_id', str(err.exception.code))
            self.assertEqual(((Path(data) / 'backlog.md').read_text(encoding='utf-8'), calls), ('keep me', []))
            store.write_json('config.json', {'backlog': {'source': 'tiden', 'product_id': 'p-1', 'statuses': list(BR)}})
            self.assertIn('3 open card(s)', self.cli(root, '--dir', data, 'backlog', 'pull', tiden=lambda c: calls.append(c) or ok()(c), repo=False))
            self.assertEqual(calls[0][-2:], ['--product-id', 'p-1'])

    def test_failed_page_leaves_the_pulled_file_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(Path(d) / '.estimate'); store.write_json('config.json', {'backlog': {'source': 'tiden', 'statuses': list(BR)}})
            self.cli(d, 'backlog', 'pull', tiden=paged(), repo=False)
            path = store.dir / 'backlog.md'; before = path.read_text(encoding='utf-8')
            with self.assertRaises(SystemExit) as err: self.cli(d, 'backlog', 'pull', tiden=paged(fail_at=1), repo=False)
            self.assertIn('server unavailable', str(err.exception.code))
            self.assertEqual(path.read_text(encoding='utf-8'), before)

    def test_forecast_backlog_without_file_uses_the_configured_source(self):
        with tempfile.TemporaryDirectory() as d:
            store = E.Store(Path(d) / '.estimate')
            (Path(d) / 'BACKLOG.md').write_text('| id | title | effort | value |\n|---|---|---|---|\n| 7 | a | 2 | 3 |\n')
            self.assertIn('#7', self.cli(d, 'forecast', '--backlog'))
            store.write_json('config.json', {'backlog': {'source': 'tiden'}})
            with self.assertRaises(SystemExit) as err: self.cli(d, 'forecast', '--backlog')
            self.assertIn('run `estimate backlog pull` first', str(err.exception.code))
            (store.dir / 'backlog.md').write_text(E.pull_tiden(SPECS, '', None, NOW)[0], encoding='utf-8')
            with self.assertRaises(SystemExit) as err: self.cli(d, 'forecast', '--backlog')
            self.assertIn('size it first', str(err.exception.code))
            self.assertIn('--skip-unsized', str(err.exception.code))

    def test_skip_unsized_leaves_them_out_with_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'BACKLOG.md').write_text(
                '| id | key | title | effort | value | depends |\n|---|---|---|---|---|---|\n'
                f'| {ID(1)} | KEY-1 | sized | 2 | 3 | |\n| {ID(2)} | | unsized |  | 5 | |\n'
                f'| 3 | | after unsized | 3 | 5 | {ID(2)} |\n')
            out = self.cli(d, 'forecast', '--backlog', '--skip-unsized')
            self.assertIn('backlog: 2 items, 5 SP', out)
            self.assertIn(f'warning: skipped 1 unsized item, not in the totals or the order: #{ID(2)}', out)
            self.assertIn(f'warning: #3 depends on unsized #{ID(2)}: kept in file order', out)
            self.assertNotIn('unknown', out)
            data = json.loads(self.cli(d, 'forecast', '--backlog', '--skip-unsized', '--json'))
            self.assertEqual([u['id'] for u in data['unsized']], [ID(2)])
            self.assertEqual([i['label'] for i in data['order']], ['KEY-1', '3'])
            self.assertTrue(data['warnings'][0].startswith('skipped 1 unsized item'))

    def test_skip_unsized_keeps_implicit_ids_on_file_rows(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'BACKLOG.md').write_text('| title | effort | value | depends |\n|---|---|---|---|\n'
                                                '| first |  | 5 | |\n| second | 2 | 3 | |\n| third | 3 | 5 | 1 |\n')
            data = json.loads(self.cli(d, 'forecast', '--backlog', '--skip-unsized', '--json'))
            self.assertEqual([u['id'] for u in data['unsized']], ['1'])
            self.assertEqual([(i['id'], i['title']) for i in data['order']], [('2', 'second'), ('3', 'third')])
            self.assertIn('#3 depends on unsized #1: kept in file order', data['warnings'])

    def test_skip_unsized_with_every_item_unsized(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'BACKLOG.md').write_text('| id | title | effort | value |\n|---|---|---|---|\n| 1 | a |  | 3 |\n')
            out = self.cli(d, 'forecast', '--backlog', '--skip-unsized')
            self.assertIn('backlog: 0 items, 0 SP', out)
            self.assertIn('skipped 1 unsized item', out)


if __name__ == '__main__':
    unittest.main()
