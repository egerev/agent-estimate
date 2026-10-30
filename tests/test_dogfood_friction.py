"""W6 dogfood friction: Codex usage echoes and prices, short history, maybe-done and container cards, row labels."""
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import estimate as E  # noqa: E402
from test_estimate import T0, row  # noqa: E402
from test_measurement import pr, write_lines  # noqa: E402

HEAD = '| id | key | title | effort | value | depends | note |\n|---|---|---|---|---|---|---|\n'


class CodexUsage(unittest.TestCase):
    def event(self, kind, second, payload):
        return {'type': kind, 'timestamp': E.iso(T0 + timedelta(seconds=second)), 'payload': payload}

    def session(self, d, lines):
        meta = {'type': 'session_meta', 'payload': {'id': 's', 'git': {
            'branch': 'feature', 'repository_url': 'https://github.com/owner/myrepo'}}}
        write_lines(Path(d) / 's.jsonl', [meta, self.event('turn_context', 0, {'model': 'gpt-6.1-sol'}), *lines])
        return E.attribute([pr()], E.scan_codex_logs(d, 'owner/myrepo'), {}, E.DEFAULT_PRICES)[1]

    def test_token_count_echo_of_an_identified_record_counts_once(self):
        usage = {'input_tokens': 200_000, 'cached_input_tokens': 0, 'output_tokens': 100_000, 'total_tokens': 300_000}
        with tempfile.TemporaryDirectory() as d:
            a = self.session(d, [
                self.event('response_item', 1, {'type': 'function_call', 'name': 'exec'}),
                self.event('token_usage_record', 2, {'response_id': 'resp_1', 'usage': usage}),
                self.event('event_msg', 3, {'type': 'token_count', 'info': {
                    'total_token_usage': usage, 'last_token_usage': usage}})])
        self.assertTrue(a['cost_complete'])
        self.assertEqual(a['missing_usage_msgs'], 0)
        self.assertAlmostEqual(a['usd'], .4 + 1)  # gpt-6.1-sol: $2/M input, $10/M output, counted once

    def test_echo_needs_the_record_right_before_it(self):
        usage = {'output_tokens': 100}
        with tempfile.TemporaryDirectory() as d:
            a = self.session(d, [
                self.event('token_usage_record', 1, {'response_id': 'resp_1', 'usage': usage}),
                self.event('token_usage_record', 2, {'response_id': 'resp_2', 'usage': {'output_tokens': 7}}),
                self.event('event_msg', 3, {'type': 'token_count', 'info': {
                    'total_token_usage': {'output_tokens': 207}, 'last_token_usage': usage}})])
        self.assertFalse(a['cost_complete'])  # not adjacent: still ambiguous, never guessed
        self.assertEqual(a['missing_usage_msgs'], 1)

    def test_equal_usage_after_new_model_output_is_its_own_response(self):
        usage = {'output_tokens': 100_000}
        with tempfile.TemporaryDirectory() as d:
            a = self.session(d, [
                self.event('token_usage_record', 1, {'response_id': 'resp_1', 'usage': usage}),
                self.event('response_item', 2, {'type': 'message', 'role': 'assistant', 'content': []}),
                self.event('event_msg', 3, {'type': 'token_count', 'info': {
                    'total_token_usage': {'output_tokens': 200_000}, 'last_token_usage': usage}})])
        self.assertIsNone(a['usd'])  # never folded into the first response; equal usage without ids stays ambiguous
        self.assertEqual(a['missing_usage_msgs'], 1)

    def test_codex_list_prices_and_long_context_tier(self):
        self.assertEqual(E.model_family('gpt-6-astra', E.DEFAULT_PRICES), 'gpt-6-astra')
        self.assertEqual(E.model_family('gpt-6.1-sol', E.DEFAULT_PRICES), 'gpt-6.1-sol')
        self.assertIsNone(E.model_family('gpt-6.2-sol', E.DEFAULT_PRICES))  # an unknown version is never priced
        price = E.DEFAULT_PRICES['gpt-6-astra']
        cost = lambda u: E.message_cost(u, price) or 0  # noqa: E731
        self.assertAlmostEqual(cost({'input_tokens': 200_000, 'cached_input_tokens': 100_000, 'output_tokens': 0}), 1 + .1)
        self.assertAlmostEqual(cost({'input_tokens': 300_000, 'output_tokens': 0}), 6.0)  # over 272K: $20/M
        self.assertEqual((price['cache_write_5m'], price['cache_write_1h']), (12.5, 25))  # 30-minute, 12-hour writes
        self.assertEqual(E.DEFAULT_PRICES['gpt-6.1-sol']['long_context']['cache_write_1h'], 10)


class ShortHistory(unittest.TestCase):
    def test_warning_under_seven_days(self):
        self.assertIn('only 3 days of merge history', E.short_history({'span_days': 2.58}) or '')
        self.assertIsNone(E.short_history({'span_days': 7.0}))
        self.assertIsNone(E.short_history(None))

    def page(self, hours_apart):
        rs = [row(3, hours_apart * h, wall=2, active=1, usd=3.0) for h in range(1, 9)]
        now = T0 + timedelta(hours=hours_apart * 9)
        return E.dashboard_html('me/r', rs, E.calibrate(rs, now, 30), E.parse_backlog(HEAD + '| 1 | | a | 3 | 5 | | |\n'), now)

    def test_dashboard_says_it_in_one_quiet_line(self):
        self.assertEqual(self.page(8).count('merge history'), 1)
        self.assertIn('<p class="warn">Only 3 days of merge history', self.page(8))
        self.assertNotIn('merge history', self.page(30))


class Labels(unittest.TestCase):
    def test_a_written_id_is_the_label_and_keys_name_uuid_or_unnumbered_rows(self):
        uuid = '645073fd-dda3-42b5-b49a-ec6924cf9f82'
        items = E.parse_backlog(HEAD + f'| {uuid} | KEY-39 | a | 1 | 1 | | |\n| 1 | OPS-437 | b | 3 | 2 | KEY-39 | |\n'
                                       f'| 2 | | c | 1 | 1 | OPS-437 | |\n')
        self.assertEqual([i['label'] for i in items], ['KEY-39', '1', '2'])
        self.assertEqual([i['depends'] for i in items], [[], [uuid], ['1']])  # depends still accepts the key
        no_ids = E.parse_backlog('| key | title | effort |\n|---|---|---|\n| BL-3 | a | 1 |\n')
        self.assertEqual(no_ids[0]['label'], 'BL-3')


class MaybeDone(unittest.TestCase):
    TABLE = HEAD + '| 1 | | shipped? | 2 | 3 | | maybe-done: 1 PR, 4 code anchors |\n| 2 | BL-7 | open | 3 | 3 | | |\n'

    def test_forecast_marks_and_warns_but_still_counts(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'b.md'; f.write_text(self.TABLE, encoding='utf-8'); (Path(d) / 'config.json').write_text('{}')   # a set-up data dir
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                E.main(['--repo', 'o/r', '--dir', d, '--now', E.iso(T0), 'forecast', '--backlog', str(f)])
        out = out.getvalue()
        self.assertIn('backlog: 2 items, 5 SP', out)
        self.assertRegex(out, r'#1 +[\d.–-]+ +V3 E2  shipped\?  maybe done')
        self.assertRegex(out, r'#2 +[\d.–-]+ +V3 E3  BL-7 · open\n')
        self.assertIn('warning: 1 card may already be done (Tiden links merged code, status still open), still counted: #1', out)

    def test_dashboard_names_them_in_one_line(self):
        page = E.dashboard_html('me/r', [], None, E.parse_backlog(self.TABLE), T0)
        self.assertEqual(page.count('may already be done'), 1)

    def test_unsized_cards_are_named_too(self):
        unsized = []
        E.parse_backlog(HEAD + '| 1 | | shipped? | | 3 | | maybe-done: 1 PR |\n', unsized)
        page = E.dashboard_html('me/r', [], None, [], T0, unsized=unsized)
        self.assertIn('1 card may already be done (Tiden links merged code, status still open), not sized, not counted: #1', page)
        unsized = []; items = E.parse_backlog(self.TABLE.replace('| 2 | BL-7 | open | 3 |', '| 2 | BL-7 | open |  |')
                                              .replace('| 2 | BL-7 | open |  | 3 | | |', '| 2 | BL-7 | open |  | 3 | | maybe-done: 2 code anchors |'), unsized)
        self.assertIn('2 cards may already be done (Tiden links merged code, status still open), 1 of them still counted: #1, #2',
                      E.maybe_done_warning(items, unsized) or '')


if __name__ == '__main__':
    unittest.main()
