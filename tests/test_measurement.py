"""Accounting regressions, using only synthetic logs and temporary git repositories."""
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import estimate as E


T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def pr(n=1, created=0, merged=1, branch='feature', commits=()):
    return {'number': n, 'headRefName': branch, 'createdAt': E.iso(T0 + timedelta(hours=created)),
            'mergedAt': E.iso(T0 + timedelta(hours=merged)),
            'commits': [{'authoredDate': E.iso(T0 + timedelta(hours=h))} for h in commits]}


def record(minutes, model='claude-opus-5-5', usage=None, main=True):
    return (T0 + timedelta(minutes=minutes), 'feature', 'claude-code', model, usage, main)


def assistant(output=1, minute=1, **kwargs):
    return {'type': 'assistant', 'timestamp': E.iso(T0 + timedelta(minutes=minute)),
            'sessionId': 's', 'gitBranch': 'feature', 'requestId': 'r',
            'message': {'id': 'm', 'model': 'claude-opus-5-5', 'usage': {'output_tokens': output}}, **kwargs}


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(line) for line in lines))


class VersionedPricing(unittest.TestCase):
    def cost(self, model, usage):
        key = E.model_family(model, E.DEFAULT_PRICES)
        self.assertIsNotNone(key)
        return E.message_cost(usage, E.DEFAULT_PRICES[key])

    def test_versions_use_their_own_prices(self):
        self.assertEqual(self.cost('claude-opus-4-6', {'input_tokens': 1_000_000}), 5)
        self.assertEqual(self.cost('claude-opus-5-5', {'input_tokens': 1_000_000}), 4)
        self.assertEqual(self.cost('claude-sonnet-5-5', {'cache_read_input_tokens': 1_000_000}), .1)
        self.assertAlmostEqual(self.cost('claude-haiku-5-5', {'input_tokens': 100_000}), .01)

    def test_unknown_versions_and_embedded_names_remain_unpriced(self):
        for model in ('claude-opus-99', 'claude-sonnet-5-50', 'not-claude-opus-5-5', 'claude-opus-5-5-experimental'):
            with self.subTest(model=model):
                self.assertIsNone(E.model_family(model, E.DEFAULT_PRICES))

    def test_dated_model_ids_and_explicit_family_overrides(self):
        self.assertIsNotNone(E.model_family('claude-opus-4-5-20251101', E.DEFAULT_PRICES))
        self.assertIsNotNone(E.model_family('claude-3-5-haiku-20241022', E.DEFAULT_PRICES))
        self.assertEqual(E.model_family('claude-opus-99', {'opus': {'input': 7}}), 'opus')

    def test_haiku_tier_counts_all_prompt_categories(self):
        u = {'input_tokens': 1, 'cache_read_input_tokens': 100_000, 'output_tokens': 10}
        self.assertAlmostEqual(self.cost('claude-haiku-5-5', u), (0.5 + 100_000 * .05 + 10 * 2.5) / 1e6)

    def test_output_only_cached_input_preserves_output_and_claude_cache(self):
        u = {'cached_input_tokens': 0, 'cache_read_input_tokens': 100, 'output_tokens': 50}
        p = {'input': 4, 'cache_read': .2, 'output': 20, 'cache_write_5m': 5, 'cache_write_1h': 8}
        self.assertAlmostEqual(E.message_cost(u, p), (100 * .2 + 50 * 20) / 1e6)

    def test_table_without_cache_write_rates_prices_usage_without_cache_writes(self):
        p = {'input': 1, 'cache_read': .1, 'output': 10}
        u = {'input_tokens': 300, 'cached_input_tokens': 100, 'output_tokens': 10}
        self.assertAlmostEqual(E.message_cost(u, p), (200 * 1 + 100 * .1 + 10 * 10) / 1e6)
        with self.assertRaises(KeyError):
            E.message_cost({'cache_creation_input_tokens': 1}, p)


class MeasurementAttribution(unittest.TestCase):
    def attr(self, sessions, prs=None):
        return E.attribute(prs or [pr()], sessions, {}, E.DEFAULT_PRICES)

    def test_unpriced_messages_make_total_unknown_but_preserve_known_cost(self):
        a = self.attr({'a': [record(1, usage={'input_tokens': 1_000_000})],
                       'b': [record(2, model='future-model', usage={'output_tokens': 100})]})[1]
        self.assertIsNone(a['usd'])
        self.assertEqual(a['known_usd'], 4)
        self.assertFalse(a['cost_complete'])
        self.assertEqual(a['unpriced_msgs'], 1)
        self.assertEqual(a['usage_by_model']['future-model'][0]['output_tokens'], 100)
        self.assertIsNone(a['agents']['claude-code/future-model']['usd'])

    def test_no_usage_is_not_a_zero_dollar_measurement(self):
        a = self.attr({'s': [record(1)]})[1]
        self.assertIsNone(a['usd'])
        self.assertFalse(a['cost_complete'])

    def test_total_only_usage_is_not_priced_as_zero(self):
        a = self.attr({'s': [record(1, usage={'total_tokens': 100})]})[1]
        self.assertIsNone(a['usd'])
        self.assertEqual(a['missing_usage_msgs'], 1)
        self.assertEqual(a['agents']['claude-code/claude-opus-5-5']['tokens'], 100)

    def test_interleaved_streams_cannot_create_activity(self):
        a = self.attr({'a': [record(m) for m in (0, 6, 12, 18)],
                       'b': [record(m) for m in (3, 9, 15)]})[1]
        self.assertEqual(a['active_h'], 0)

    def test_overlapping_activity_is_union_of_independent_streams(self):
        a = self.attr({'a': [record(m) for m in (0, 4, 8)],
                       'b': [record(m) for m in (2, 6, 10)]})[1]
        self.assertAlmostEqual(a['active_h'], 10 / 60)

    def test_subagents_cannot_be_executor_even_with_more_usage(self):
        a = self.attr({'main': [record(0), record(2)],
                       'sub': [record(m, model='claude-sonnet-5-5', usage={'input_tokens': 100}, main=False)
                               for m in range(10)]})[1]
        self.assertEqual(a['executor'], 'claude-code/claude-opus-5-5')
        b = self.attr({'sub': [record(1, usage={'input_tokens': 1}, main=False)]})[1]
        self.assertIsNone(b['executor'])

    def test_commits_recover_work_more_than_seven_days_before_opening(self):
        p = pr(created=240, merged=264, commits=(0, 248))
        a = self.attr({'s': [record(0), record(2)]}, [p])[1]
        self.assertEqual(a['first_ts'], T0)
        self.assertAlmostEqual(a['active_h'], 2 / 60)

    def test_branch_logs_before_first_commit_are_start_evidence(self):
        a = self.attr({'s': [record(0), record(2)]}, [pr(created=240, merged=264, commits=(120,))])[1]
        self.assertEqual(a['first_ts'], T0)
        self.assertAlmostEqual(a['active_h'], 2 / 60)

    def test_reused_branch_includes_work_between_previous_merge_and_first_commit(self):
        ps = [pr(1, created=0, merged=1), pr(2, created=3, merged=4, commits=(2,))]
        a = self.attr({'s': [record(m) for m in (10, 12, 90, 92)]}, ps)
        self.assertEqual(a[2]['first_ts'], T0 + timedelta(minutes=90))
        self.assertAlmostEqual(a[2]['active_h'], 2 / 60)

    def test_exact_single_pr_link_accepts_early_work_on_other_branch(self):
        recs = [(T0 + timedelta(minutes=m), 'main', 'claude-code', 'claude-opus-5-5', None, True) for m in (0, 2)]
        a = E.attribute([pr(created=240, merged=264, commits=(120,))], {'s': recs}, {'s': {1}}, E.DEFAULT_PRICES)[1]
        self.assertEqual(a['first_ts'], T0)

    def test_previous_merge_clips_old_commit_dates_for_reused_branch(self):
        ps = [pr(1, created=0, merged=1), pr(2, created=2, merged=3, commits=(0, 2))]
        a = self.attr({'s': [record(m) for m in (10, 12, 130, 132)]}, ps)
        self.assertAlmostEqual(a[1]['active_h'], 2 / 60)
        self.assertAlmostEqual(a[2]['active_h'], 2 / 60)


class ClaudeLogIdentity(unittest.TestCase):
    def test_remote_urls_with_userinfo_and_ports_match_exact_repository(self):
        for url in ('ssh://git@github.com:22/owner/repo.git', 'https://user@github.com/owner/repo.git',
                    'https://user:token@GitHub.com:443/Owner/Repo/', 'git@github.com:owner/repo.git',
                    'http://github.com/owner/repo'):
            with self.subTest(url=url): self.assertTrue(E.repo_url_matches(url, 'owner/repo'))
        for url in ('https://github.com/owner/repo-other', 'https://gitlab.com/owner/repo',
                    'https://github.com.evil/owner/repo', 'git@github.com:other/owner/repo', 'file:///owner/repo', ''):
            with self.subTest(url=url): self.assertFalse(E.repo_url_matches(url, 'owner/repo'))

    def test_matching_basename_or_scan_all_is_not_repository_identity(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / '-x-myrepo' / 's.jsonl', [assistant()])
            for scan_all in (False, True):
                self.assertEqual(dict(E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', scan_all=scan_all)[0]), {})

    def test_exact_pr_link_authenticates_session_without_name_match(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / '-x-other' / 's.jsonl', [assistant(),
                {'type': 'pr-link', 'sessionId': 's', 'prRepository': 'owner/myrepo', 'prNumber': 1}])
            sessions, links = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo')
            self.assertEqual(sum(len(rs) for rs in sessions.values()), 1)
            self.assertEqual(links['s'], {1})

    def test_cwd_remote_verifies_owner_and_repository(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d) / 'checkout'; cwd.mkdir()
            subprocess.run(['git', 'init', '-q', str(cwd)], check=True)
            subprocess.run(['git', '-C', str(cwd), 'remote', 'add', 'origin', 'git@github.com:owner/myrepo.git'], check=True)
            write_lines(Path(d) / 'logs' / '-x-project' / 's.jsonl', [assistant(cwd=str(cwd))])
            sessions, _ = E.scan_claude_logs(Path(d) / 'logs', 'myrepo', 'owner/myrepo')
            self.assertTrue(sessions)
            self.assertFalse(E.scan_claude_logs(Path(d) / 'logs', 'myrepo', 'other/myrepo')[0])

    def test_contradictory_cwd_cannot_inherit_session_identity(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d) / 'checkout'; cwd.mkdir()
            subprocess.run(['git', 'init', '-q', str(cwd)], check=True)
            subprocess.run(['git', '-C', str(cwd), 'remote', 'add', 'origin', 'git@github.com:other/myrepo.git'], check=True)
            p = Path(d) / 'logs' / '-project'
            wrong = assistant(output=1000, minute=2, cwd=str(cwd)); wrong['message']['id'] = 'wrong'
            write_lines(p / 's.jsonl', [assistant(), wrong,
                {'type': 'pr-link', 'sessionId': 's', 'prRepository': 'owner/myrepo', 'prNumber': 1}])
            sessions, _ = E.scan_claude_logs(Path(d) / 'logs', 'myrepo', 'owner/myrepo')
            self.assertEqual(sum(r[4].get('output_tokens', 0) for rs in sessions.values() for r in rs if r[4]), 1)
            # An explicit project directory is a conscious operator override.
            trusted, _ = E.scan_claude_logs(Path(d) / 'logs', 'myrepo', 'owner/myrepo', project_dirs=[p])
            self.assertEqual(sum(r[4].get('output_tokens', 0) for rs in trusted.values() for r in rs if r[4]), 1001)

    def test_explicit_project_dir_selects_only_exact_directory(self):
        with tempfile.TemporaryDirectory() as d:
            trusted = Path(d) / '-trusted'; untrusted = Path(d) / '-trusted-other'
            write_lines(trusted / 's.jsonl', [assistant()]); write_lines(untrusted / 's.jsonl', [assistant()])
            sessions, _ = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[trusted])
            self.assertEqual(sum(len(rs) for rs in sessions.values()), 1)

    def test_last_usage_snapshot_is_selected_across_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '-project'
            write_lines(p / 'z.jsonl', [assistant(output=1000, minute=2)])
            write_lines(p / 'a.jsonl', [assistant(output=1, minute=1)])
            sessions, _ = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[p])
            usage = [r[4] for rs in sessions.values() for r in rs if r[4]]
            self.assertEqual(len(usage), 1)
            self.assertEqual(usage[0]['output_tokens'], 1000)

    def test_missing_message_ids_do_not_deduplicate_distinct_usage(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '-project'; rows = [assistant(minute=m, output=m) for m in (1, 2)]
            for row in rows:
                row.pop('requestId'); row['message'].pop('id')
            write_lines(p / 's.jsonl', rows)
            sessions, _ = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[p])
            self.assertEqual(sum(bool(r[4]) for rs in sessions.values() for r in rs), 2)

    def test_missing_usage_in_one_response_prevents_complete_cost(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '-project'
            missing = assistant(minute=2)
            missing['message']['id'] = 'missing'; missing['message'].pop('usage')
            write_lines(p / 's.jsonl', [assistant(), missing])
            sessions, links = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[p])
            a = E.attribute([pr()], sessions, links, E.DEFAULT_PRICES)[1]
            self.assertIsNone(a['usd'])
            self.assertGreater(a['known_usd'], 0)
            self.assertEqual(a['missing_usage_msgs'], 1)

    def test_missing_usage_snapshot_does_not_erase_valid_usage(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '-project'
            missing = assistant(minute=2); missing['message'].pop('usage')
            write_lines(p / 's.jsonl', [assistant(), missing])
            sessions, links = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[p])
            a = E.attribute([pr()], sessions, links, E.DEFAULT_PRICES)[1]
            self.assertTrue(a['cost_complete'])
            self.assertEqual(a['missing_usage_msgs'], 0)

    def test_usage_without_model_is_preserved_and_unpriced(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '-project'; msg = assistant(); msg['message'].pop('model')
            write_lines(p / 's.jsonl', [msg])
            sessions, links = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[p])
            a = E.attribute([pr()], sessions, links, E.DEFAULT_PRICES)[1]
            self.assertFalse(a['cost_complete'])
            self.assertEqual(a['usage_by_model']['unknown'][0]['output_tokens'], 1)

    def test_usage_without_model_never_borrows_another_responses_model(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '-project'
            for unknown_first in (False, True):
                with self.subTest(unknown_first=unknown_first):
                    known = assistant(output=1_000_000, minute=2 if unknown_first else 1)
                    missing = assistant(output=1_000_000, minute=1 if unknown_first else 2)
                    missing['message']['id'] = 'unknown'; missing['message'].pop('model')
                    write_lines(p / 's.jsonl', [known, missing])
                    sessions, links = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[p])
                    a = E.attribute([pr()], sessions, links, E.DEFAULT_PRICES)[1]
                    self.assertIsNone(a['usd'])
                    self.assertEqual(a['known_usd'], 20)
                    self.assertEqual(a['unpriced_msgs'], 1)

    def test_subagents_are_distinct_activity_streams(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '-project'
            write_lines(p / 's.jsonl', [assistant(minute=m) for m in (0, 6)])
            write_lines(p / 's' / 'subagents' / 'a.jsonl', [assistant(minute=3, isSidechain=True)])
            sessions, links = E.scan_claude_logs(d, 'myrepo', 'owner/myrepo', project_dirs=[p])
            self.assertEqual(len(sessions), 2)
            self.assertEqual(E.attribute([pr()], sessions, links, E.DEFAULT_PRICES)[1]['active_h'], 0)


class CodexCoverage(unittest.TestCase):
    def meta(self):
        return {'type': 'session_meta', 'payload': {'id': 's', 'git': {
            'branch': 'feature', 'repository_url': 'https://github.com/owner/myrepo'}}}

    def event(self, kind, minute, payload):
        return {'type': kind, 'timestamp': E.iso(T0 + timedelta(minutes=minute)), 'payload': payload}

    def test_same_session_id_in_different_files_does_not_bridge_activity(self):
        with tempfile.TemporaryDirectory() as d:
            for name, minutes in [('a', (0, 6, 12)), ('b', (3, 9, 15))]:
                write_lines(Path(d) / f'{name}.jsonl', [self.meta(), self.event('turn_context', minutes[0], {'model': 'gpt-x'}),
                    *[self.event('token_usage_record', minute, {'usage': {'output_tokens': 1}}) for minute in minutes[1:]]])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            self.assertEqual(len(sessions), 2)
            self.assertEqual(E.attribute([pr()], sessions, {}, E.DEFAULT_PRICES)[1]['active_h'], 0)

    def test_partial_explicit_records_preserve_unmatched_fallback_responses(self):
        with tempfile.TemporaryDirectory() as d:
            lines = [self.meta(), self.event('turn_context', 0, {'model': 'gpt-x'}),
                     self.event('token_usage_record', 1, {'usage': {'output_tokens': 1}})]
            for minute, used, total in ((1, 1, 1), (2, 2, 3)):
                lines.append(self.event('event_msg', minute, {'type': 'token_count', 'info': {
                    'total_token_usage': {'output_tokens': total}, 'last_token_usage': {'output_tokens': used}}}))
            write_lines(Path(d) / 's.jsonl', lines)
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            self.assertEqual(sum(r[4].get('output_tokens', 0) for rs in sessions.values() for r in rs if r[4]), 3)

    def test_fallback_without_cumulative_totals_keeps_each_response(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / 's.jsonl', [self.meta(), *[
                self.event('event_msg', m, {'type': 'token_count', 'info': {'last_token_usage': {'output_tokens': m}}})
                for m in (1, 2)]])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            self.assertEqual(sum(r[4].get('output_tokens', 0) for rs in sessions.values() for r in rs if r[4]), 3)

    def test_equal_usage_at_different_times_is_ambiguous_without_response_ids(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / 's.jsonl', [self.meta(),
                self.event('turn_context', 0, {'model': 'claude-opus-5-5'}),
                self.event('token_usage_record', 1, {'usage': {'output_tokens': 100}}),
                self.event('event_msg', 2, {'type': 'token_count', 'info': {
                    'total_token_usage': {'output_tokens': 200}, 'last_token_usage': {'output_tokens': 100}}})])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            a = E.attribute([pr()], sessions, {}, E.DEFAULT_PRICES)[1]
            self.assertIsNone(a['usd'])
            self.assertAlmostEqual(a['known_usd'], .002)
            self.assertEqual(a['missing_usage_msgs'], 1)

    def test_distinct_response_ids_preserve_equal_usage_responses(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / 's.jsonl', [self.meta(),
                self.event('turn_context', 0, {'model': 'claude-opus-5-5'}),
                self.event('token_usage_record', 1, {'response_id': 'one', 'usage': {'output_tokens': 100}}),
                self.event('event_msg', 2, {'type': 'token_count', 'response_id': 'two', 'info': {
                    'total_token_usage': {'output_tokens': 200}, 'last_token_usage': {'output_tokens': 100}}})])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            a = E.attribute([pr()], sessions, {}, E.DEFAULT_PRICES)[1]
            self.assertTrue(a['cost_complete'])
            self.assertAlmostEqual(a['usd'], .004)

    def test_different_models_with_equal_usage_are_independent(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / 's.jsonl', [self.meta(),
                self.event('turn_context', 0, {'model': 'claude-opus-5-5'}),
                self.event('event_msg', 1, {'type': 'token_count', 'info': {
                    'total_token_usage': {'output_tokens': 100}, 'last_token_usage': {'output_tokens': 100}}}),
                self.event('turn_context', 1, {'model': 'claude-sonnet-5-5'}),
                self.event('token_usage_record', 1, {'usage': {'output_tokens': 100}})])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            a = E.attribute([pr()], sessions, {}, E.DEFAULT_PRICES)[1]
            self.assertTrue(a['cost_complete'])
            self.assertAlmostEqual(a['usd'], .003)

    def test_same_response_id_selects_authoritative_snapshot_before_comparing_usage(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / 's.jsonl', [self.meta(),
                self.event('turn_context', 0, {'model': 'claude-opus-5-5'}),
                self.event('event_msg', 1, {'type': 'token_count', 'response_id': 'one', 'info': {
                    'total_token_usage': {'output_tokens': 1}, 'last_token_usage': {'output_tokens': 1}}}),
                self.event('token_usage_record', 2, {'response_id': 'one', 'usage': {'output_tokens': 100}})])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            a = E.attribute([pr()], sessions, {}, E.DEFAULT_PRICES)[1]
            self.assertTrue(a['cost_complete'])
            self.assertAlmostEqual(a['usd'], .002)
            self.assertEqual(sum(agent['tokens'] for agent in a['agents'].values()), 100)

    def test_same_response_id_with_conflicting_models_is_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / 's.jsonl', [self.meta(),
                self.event('turn_context', 0, {'model': 'claude-opus-5-5'}),
                self.event('event_msg', 1, {'type': 'token_count', 'response_id': 'one', 'info': {
                    'total_token_usage': {'output_tokens': 1}, 'last_token_usage': {'output_tokens': 1}}}),
                self.event('turn_context', 2, {'model': 'claude-sonnet-5-5'}),
                self.event('token_usage_record', 2, {'response_id': 'one', 'usage': {'output_tokens': 100}})])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            a = E.attribute([pr()], sessions, {}, E.DEFAULT_PRICES)[1]
            self.assertFalse(a['cost_complete'])
            self.assertIsNone(a['usd'])
            self.assertEqual(a['missing_usage_msgs'], 1)

    def test_codex_remote_with_port_and_userinfo_keeps_its_logs(self):
        with tempfile.TemporaryDirectory() as d:
            meta = self.meta(); meta['payload']['git']['repository_url'] = 'ssh://git@github.com:22/owner/myrepo.git'
            write_lines(Path(d) / 's.jsonl', [meta, self.event('token_usage_record', 1, {'usage': {'output_tokens': 1}})])
            self.assertEqual(len(E.scan_codex_logs(d, 'owner/myrepo')), 1)

    def test_response_interrupted_before_usage_keeps_cost_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            reply = {'type': 'message', 'role': 'assistant', 'content': []}
            write_lines(Path(d) / 's.jsonl', [self.meta(),
                self.event('turn_context', 0, {'model': 'claude-opus-5-5'}),
                self.event('response_item', 1, reply),
                self.event('event_msg', 1, {'type': 'token_count', 'info': {
                    'total_token_usage': {'output_tokens': 100}, 'last_token_usage': {'output_tokens': 100}}}),
                self.event('response_item', 2, {'type': 'message', 'role': 'user', 'content': []}),
                self.event('response_item', 3, reply),
                self.event('event_msg', 3, {'type': 'turn_aborted'})])
            sessions = E.scan_codex_logs(d, 'owner/myrepo')
            a = E.attribute([pr()], sessions, {}, E.DEFAULT_PRICES)[1]
            self.assertFalse(a['cost_complete'])
            self.assertIsNone(a['usd'])
            self.assertAlmostEqual(a['known_usd'], .002)
            self.assertEqual(a['missing_usage_msgs'], 1)

    def test_responses_with_usage_are_not_marked_missing(self):
        with tempfile.TemporaryDirectory() as d:
            write_lines(Path(d) / 's.jsonl', [self.meta(),
                self.event('turn_context', 0, {'model': 'claude-opus-5-5'}),
                self.event('response_item', 1, {'type': 'function_call', 'name': 'shell'}),
                self.event('event_msg', 1, {'type': 'token_count', 'info': {
                    'total_token_usage': {'output_tokens': 100}, 'last_token_usage': {'output_tokens': 100}}}),
                self.event('event_msg', 2, {'type': 'token_count', 'info': None})])
            a = E.attribute([pr()], E.scan_codex_logs(d, 'owner/myrepo'), {}, E.DEFAULT_PRICES)[1]
            self.assertTrue(a['cost_complete'])
            self.assertEqual(a['missing_usage_msgs'], 0)


if __name__ == '__main__':
    unittest.main()
