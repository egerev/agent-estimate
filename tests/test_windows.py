"""Plan windows: status-line snapshots, the opt-in status line hook, window prices, cards, subscriptions. Synthetic data;
a temp HOME and temp files only, never the real Claude settings or logs."""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import estimate as E  # noqa: E402
from test_estimate import T0, row  # noqa: E402

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
STATUS = {'session_id': 's', 'cwd': '/secret/path', 'model': {'id': 'claude-opus-5-5'},
          'rate_limits': {'five_hour': {'used_percentage': 16, 'resets_at': int((NOW + timedelta(hours=2)).timestamp())},
                          'seven_day': {'used_percentage': 24, 'resets_at': int((NOW + timedelta(days=5)).timestamp())}}}


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'ESTIMATE_WINDOWS_FILE': str(self.home / 'w.jsonl'),
                                           'ESTIMATE_RATE_HISTORY': str(self.home / 'r.jsonl')})
        env.start(); self.addCleanup(env.stop); os.environ.pop('CLAUDE_CONFIG_DIR', None)


class Snapshots(TempHome):
    def test_only_window_numbers_and_an_account_tag_are_kept(self):
        snap = E.window_snapshot(STATUS, NOW, 'abcd1234')
        self.assertEqual(snap, {'ts': E.iso(NOW), 'agent': 'claude-code', 'account': 'abcd1234',
                                'five_hour': {'pct': 16.0, 'resets_at': E.iso(NOW + timedelta(hours=2))},
                                'seven_day': {'pct': 24.0, 'resets_at': E.iso(NOW + timedelta(days=5))}})
        self.assertIsNone(E.window_snapshot({'model': {}}, NOW))                     # an API-key session: no rate_limits
        self.assertIsNone(E.window_snapshot({'rate_limits': {'five_hour': {'used_percentage': True}}}, NOW))
        self.assertIsNone(E.window_snapshot('nonsense', NOW))

    def test_unchanged_snapshots_are_logged_every_five_minutes(self):
        path = self.home / 'w.jsonl'
        at = lambda m, pct=16: E.window_snapshot({'rate_limits': {'five_hour': {'used_percentage': pct}}}, NOW + timedelta(minutes=m))
        self.assertEqual([E.append_snapshot(path, at(m)) for m in (0, 1, 4)], [True, False, False])
        self.assertTrue(E.append_snapshot(path, at(6))); self.assertTrue(E.append_snapshot(path, at(6.5, 17)))
        self.assertEqual(len(path.read_text().splitlines()), 3)

    def test_the_account_tag_is_a_hash_never_the_email(self):
        (self.home / '.claude.json').write_text(json.dumps({'oauthAccount': {'accountUuid': 'u-1', 'emailAddress': 'me@example.com'}}))
        tag = E.claude_account_tag()
        self.assertRegex(tag or '', r'^[0-9a-f]{8}$'); self.assertNotIn('example', tag or '')
        cfg = self.home / 'other'; cfg.mkdir(); (cfg / '.claude.json').write_text(json.dumps({'oauthAccount': {'accountUuid': 'u-2'}}))
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(cfg)}):
            self.assertNotEqual(E.claude_account_tag(), tag)
        (self.home / '.claude.json').write_text('{')
        self.assertIsNone(E.claude_account_tag())

    def statusline(self, stdin, *argv):
        out = io.BytesIO(); text = io.TextIOWrapper(out, encoding='utf-8', write_through=True)
        fake_in = mock.Mock(); fake_in.buffer = io.BytesIO(stdin)
        with mock.patch.object(E.sys, 'stdin', fake_in), contextlib.redirect_stdout(text):
            E.main(['statusline-log', *argv])
        return out.getvalue().decode()

    def test_statusline_log_records_then_runs_the_users_status_line(self):
        raw = json.dumps(STATUS).encode()
        shown = self.statusline(raw, '--then', 'cat >/dev/null; echo mine')
        self.assertEqual(shown.strip(), 'mine')
        line = json.loads((self.home / 'w.jsonl').read_text())
        self.assertEqual((line['five_hour']['pct'], line['seven_day']['pct']), (16.0, 24.0))
        self.assertNotIn('secret', (self.home / 'w.jsonl').read_text())                  # nothing but the windows
        self.assertEqual(self.statusline(raw, '--then', 'wc -c').strip(), str(len(raw)))   # the same input reaches it

    def test_statusline_log_never_fails(self):
        self.assertEqual(self.statusline(b'not json', '--then', 'echo still').strip(), 'still')
        self.assertEqual(self.statusline(json.dumps(STATUS).encode()).strip(), '5h 16% · week 24%')
        with mock.patch.dict(os.environ, {'ESTIMATE_WINDOWS_FILE': '/proc/nowhere/w.jsonl'}):
            self.assertEqual(self.statusline(json.dumps(STATUS).encode(), '--then', 'echo ok').strip(), 'ok')


class StatuslineInstall(TempHome):
    def settings(self):
        return json.loads((self.home / '.claude' / 'settings.json').read_text())

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(E.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            E.main([*argv, '--bin-dir', str(self.home / 'bin')])
        return out.getvalue(), err.getvalue()

    def test_wraps_the_existing_status_line_and_uninstall_restores_it(self):
        (self.home / '.claude').mkdir()
        (self.home / '.claude' / 'settings.json').write_text(json.dumps({'model': 'opus', 'statusLine': {
            'type': 'command', 'command': "python3 ~/my line.py --x 'a b'", 'padding': 0}}))
        out, _ = self.run_cli('install', '--dry-run', '--statusline')
        self.assertIn('would wrap', out); self.assertEqual(self.settings()['statusLine']['command'], "python3 ~/my line.py --x 'a b'")
        out, _ = self.run_cli('install', '--statusline')
        cmd = self.settings()['statusLine']['command']
        self.assertTrue(cmd.startswith(f"{self.home / 'bin' / 'estimate'} statusline-log --then "))
        self.assertEqual(E.shlex.split(cmd)[-1], "python3 ~/my line.py --x 'a b'")
        self.assertEqual((self.settings()['model'], self.settings()['statusLine']['padding']), ('opus', 0))
        self.assertTrue((self.home / '.claude' / 'settings.json.estimate-backup').exists())
        out, _ = self.run_cli('install', '--statusline')
        self.assertIn('already logs plan windows', out); self.assertEqual(self.settings()['statusLine']['command'], cmd)
        self.run_cli('uninstall')
        self.assertEqual(self.settings()['statusLine'], {'type': 'command', 'command': "python3 ~/my line.py --x 'a b'", 'padding': 0})

    def test_without_a_status_line_it_adds_one_and_uninstall_removes_it(self):
        out, _ = self.run_cli('install')
        self.assertIn('estimate install --statusline', out); self.assertFalse((self.home / '.claude' / 'settings.json').exists())
        self.run_cli('install', '--statusline')
        self.assertEqual(self.settings()['statusLine']['command'], f"{self.home / 'bin' / 'estimate'} statusline-log")
        self.run_cli('uninstall'); self.assertNotIn('statusLine', self.settings())

    def test_a_foreign_launcher_is_never_wired_in_and_paths_are_absolute(self):
        (self.home / 'bin').mkdir(); (self.home / 'bin' / 'estimate').write_text('#!/bin/sh\n')       # someone else's `estimate`
        _, err = self.run_cli('install', '--statusline')
        self.assertIn("is not this checkout's `estimate`", err); self.assertFalse((self.home / '.claude' / 'settings.json').exists())
        (self.home / 'bin' / 'estimate').unlink()
        with contextlib.chdir(self.home), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(E.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            E.main(['install', '--statusline', '--bin-dir', 'bin'])
        self.assertTrue(self.settings()['statusLine']['command'].startswith(f"{self.home / 'bin' / 'estimate'} statusline-log"))

    def test_uninstall_keeps_the_launcher_while_the_status_line_still_calls_it(self):
        self.run_cli('install', '--statusline')
        with mock.patch.object(E, 'write_atomic', side_effect=PermissionError(13, 'Permission denied')):
            _, err = self.run_cli('uninstall')
        self.assertIn('could not restore the status line', err)
        self.assertTrue((self.home / 'bin' / 'estimate').is_symlink())
        self.assertIn('statusline-log', self.settings()['statusLine']['command'])
        path = self.home / '.claude' / 'settings.json'; good = path.read_text()
        path.write_text(good[:-3])                                                       # broken JSON that still calls estimate
        _, err = self.run_cli('uninstall')
        self.assertIn('not valid JSON', err); self.assertTrue((self.home / 'bin' / 'estimate').is_symlink())
        path.write_text(good)
        with mock.patch.object(E.Path, 'read_text', side_effect=PermissionError(13, 'Permission denied')):
            _, err = self.run_cli('uninstall')
        self.assertIn('could not read', err); self.assertTrue((self.home / 'bin' / 'estimate').is_symlink())
        self.run_cli('uninstall')
        self.assertFalse((self.home / 'bin' / 'estimate').exists()); self.assertNotIn('statusLine', self.settings())

    def test_never_touches_invalid_settings_or_a_foreign_status_line(self):
        (self.home / '.claude').mkdir(); path = self.home / '.claude' / 'settings.json'
        path.write_text('{oops'); _, err = self.run_cli('install', '--statusline')
        self.assertIn('not a JSON object', err); self.assertEqual(path.read_text(), '{oops')
        path.write_text(json.dumps({'statusLine': {'type': 'command', 'command': 'other statusline-log'}}))
        self.run_cli('uninstall'); self.assertEqual(self.settings()['statusLine']['command'], 'other statusline-log')


def snap(minutes, h5, w7, account='a', resets=None, legacy=False):
    ts = NOW - timedelta(minutes=minutes)
    return {'ts': ts, 'account': account, 'h5': (h5, resets) if h5 is not None else None,
            'w7': (w7, None) if w7 is not None else None, 'legacy': legacy}


def spend(minutes, usd, agent='claude-code'):
    return (NOW - timedelta(minutes=minutes), agent, 'm', 1000, usd, ('s',), ('k', minutes))


class WindowPrice(unittest.TestCase):
    def hours(self, n, **kw):
        """`n` clean clock hours: snapshots at :05 and :50 with a 2-point 5-hour and 1-point weekly rise, $4 spent."""
        snaps, recs = [], []
        for h in range(n):
            base = 60 * (h + 2) + 0                    # minutes ago of the hour's start, hours ending before NOW
            snaps += [snap(base - 5, 10 + 2 * h, 30 + h, **kw), snap(base - 50, 12 + 2 * h, 31 + h, **kw)]
            recs += [spend(base - 20, 1.5), spend(base - 30, 2.5)]
        return snaps, recs

    def test_price_is_summed_dollars_over_summed_rise(self):
        p = E.window_price(*self.hours(10))
        self.assertEqual(p['hours'], {'h5': 10, 'w7': 10})
        self.assertAlmostEqual(p['h5'], 40 / 20); self.assertAlmostEqual(p['w7'], 40 / 10)

    def test_hidden_under_ten_clean_hours(self):
        p = E.window_price(*self.hours(9))
        self.assertEqual((p['h5'], p['w7'], p['hours']['h5']), (None, None, 9))

    def test_resets_other_accounts_and_unpriced_hours_are_not_clean(self):
        snaps, recs = self.hours(10)
        base = 60 * 2
        bad = [snap(base - 10, 50, 40, account='b')]                                        # a second account in hour 0
        reset = [s if s['ts'] != NOW - timedelta(minutes=60 * 3 - 50) else {**s, 'h5': (1, None)} for s in snaps]  # hour 1 resets
        self.assertEqual(E.window_price(snaps + bad, recs)['hours']['h5'], 9)
        self.assertEqual(E.window_price(reset, recs)['hours']['h5'], 9)
        self.assertEqual(E.window_price(snaps, recs + [spend(60 * 2 - 25, None)])['hours']['h5'], 9)
        stale = snaps + [snap(60 * 2 - 30, 9, 30)]                                           # 1 point lower: an older session
        self.assertEqual(E.window_price(stale, recs)['hours']['h5'], 10)
        codex = recs + [spend(60 * 2 - 25, 100.0, agent='codex')]                             # Codex spend is not Claude's window
        self.assertAlmostEqual(E.window_price(snaps, codex)['h5'], 2.0)

    def test_mixed_offsets_share_one_hour_and_equal_times_sort(self):
        snaps, recs = self.hours(10)
        shifted = [{**x, 'ts': x['ts'].astimezone(timezone(timedelta(hours=5, minutes=30)))} for x in snaps]
        self.assertEqual(E.window_price(shifted, recs)['hours']['h5'], 10)
        tie = recs + [(recs[0][0], 'claude-code', 'm', 1, None, ('s',), 'x')]                  # same instant, one unpriced
        self.assertEqual(E.window_price(snaps, tie)['hours']['h5'], 9)

    def test_legacy_rows_fill_hours_without_own_snapshots(self):
        snaps, recs = self.hours(10, account=None, legacy=True)
        self.assertEqual(E.window_price(snaps, recs)['hours']['h5'], 10)


class ReadSnapshots(TempHome):
    def test_own_snapshots_and_a_status_line_history(self):
        E.append_snapshot(self.home / 'w.jsonl', E.window_snapshot(STATUS, NOW - timedelta(minutes=3), 'tag'))
        (self.home / 'r.jsonl').write_text('\n'.join([
            json.dumps({'ts': (NOW - timedelta(minutes=9)).astimezone(timezone(timedelta(hours=8))).isoformat(), 'sid': 'x', 'pc': 3,
                        '5h_pct': 12, '7d_pct': 20}),
            json.dumps({'ts': E.iso(NOW - timedelta(days=30)), '5h_pct': 1, '7d_pct': 1}), 'garbage']))
        snaps = E.read_snapshots(NOW)
        self.assertEqual([(s['legacy'], s['account'], s['h5'][0]) for s in snaps], [(True, None, 12.0), (False, 'tag', 16.0)])
        self.assertEqual(snaps[1]['h5'][1], NOW + timedelta(hours=2))


class Cards(unittest.TestCase):
    def cards(self, *snaps, codex=None, price=None, accounts=0, now=NOW):
        win = {**E.window_report(list(snaps), [], codex, now), 'price': price or {}, 'accounts': accounts}
        return E.window_cards(win, now, timezone.utc)

    def test_fresh_snapshot_with_reset_times_and_price(self):
        s = {'ts': NOW - timedelta(minutes=2), 'account': 'a', 'h5': (16.0, NOW + timedelta(hours=1, minutes=15)),
             'w7': (24.0, NOW + timedelta(days=5, hours=20)), 'legacy': False}
        h5, w7, cx = self.cards(s, price={'h5': 1.76, 'w7': 19.0}, accounts=2)
        self.assertEqual(h5, ('Claude · 5-hour window', 'resets 19:15', 16.0,
                              '≈ $176 API-equivalent per window, rough · 1 h 15 min left · latest of 2 accounts'))
        self.assertEqual(w7[1:3], ('resets Wed 14 Oct, 14:00', 24.0)); self.assertIn('≈ $1,900 API-equivalent per week, rough', w7[3])
        self.assertEqual(cx, ('Codex · week', 'no snapshot yet', None, ''))

    def test_a_passed_reset_or_an_old_percent_is_never_shown_as_current(self):
        s = {'ts': NOW - timedelta(hours=3), 'account': 'a', 'h5': (90.0, NOW - timedelta(minutes=5)), 'w7': None, 'legacy': False}
        h5, w7, _ = self.cards(s)
        self.assertEqual(h5, ('Claude · 5-hour window', 'reset 17:55', None, 'no snapshot since the reset'))
        self.assertEqual(w7[1:3], ('no snapshot yet', None))
        old = {'ts': NOW - timedelta(hours=6), 'account': None, 'h5': (40.0, None), 'w7': (30.0, None), 'legacy': True}
        h5, w7, _ = self.cards(old)
        self.assertEqual(h5, ('Claude · 5-hour window', 'no recent snapshot', None, 'last 40% 6 h ago'))
        self.assertEqual(w7, ('Claude · week', 'as of 12:00', 30.0, 'snapshot 6 h ago'))

    def test_a_known_reset_beats_a_newer_history_row_until_it_passes(self):
        own = {'ts': NOW - timedelta(hours=2), 'account': 'a', 'h5': (90.0, NOW + timedelta(minutes=30)), 'w7': None, 'legacy': False}
        row_ = {'ts': NOW - timedelta(minutes=5), 'account': None, 'h5': (95.0, None), 'w7': None, 'legacy': True}
        self.assertEqual(self.cards(own, row_)[0][1:3], ('resets 18:30', 90.0))          # the row may be another account
        passed = {**own, 'h5': (90.0, NOW - timedelta(minutes=30))}
        self.assertEqual(self.cards(passed, row_)[0][1:3], ('as of 17:55', 95.0))        # newer than the reset: current
        early = {**row_, 'ts': NOW - timedelta(minutes=40)}
        self.assertEqual(self.cards(passed, early)[0][1:3], ('reset 17:30', None))      # older than the reset: not current

    def test_codex_week_with_plan_and_age(self):
        lim = E.codex_limits(NOW - timedelta(minutes=30), {'plan_type': 'pro', 'credits': {'balance': '99'}, 'secondary': None,
                                                           'primary': {'used_percent': 22.0, 'window_minutes': 10080,
                                                                       'resets_at': int((NOW + timedelta(days=4, hours=20)).timestamp())}})
        self.assertNotIn('credits', lim)
        cx = self.cards(codex=lim)[2]
        self.assertEqual(cx, ('Codex · week', 'resets Tue 13 Oct, 14:00', 22.0, 'Pro plan, no 5-hour window · 4 d 20 h left · snapshot 30 min ago'))


class PageWindows(unittest.TestCase):
    def page(self, windows=None, subs=(), burn=None):
        rs = [row(3, 20 * h, wall=2 + h % 3, active=1 + h % 2, usd=3.0 + h % 2) for h in range(1, 9)]
        now = T0 + timedelta(days=8, hours=18)
        backlog = E.parse_backlog('| id | title | effort | value |\n|---|---|---|---|\n' + ''.join(f'| {k} | c{k} | 2 | 3 |\n' for k in range(1, 9)))
        return E.dashboard_html('me/r', rs, E.calibrate(rs, now, 14), backlog, now, None, (), burn, timezone.utc, windows, subs), now

    def test_planner_says_how_much_of_each_window_a_box_takes(self):
        page, now = self.page({'claude': None, 'codex': None, 'accounts': 0, 'price': {'h5': 1.0, 'w7': 20.0, 'hours': {'h5': 12, 'w7': 12}}})
        usd = [float(x) for x in re.findall(r'<span class="v t0a1">[^<]*<b>[\d]+ SP</b> likely done[^$]*<b>\$([\d.,]+)</b>', page)]
        self.assertTrue(usd)
        self.assertIn(f'<span class="v t0a1">That is <em>{usd[0]:.0f}%</em> of a 5-hour window and <em>{usd[0] / 20:.0f}%</em> of the week.', page)
        self.assertIn('(12 for the 5-hour window, 12 for the week)', page)
        self.assertNotIn('That is', self.page({'claude': None, 'codex': None, 'accounts': 0, 'price': {'hours': {'h5': 3, 'w7': 3}}})[0])

    def test_today_lanes_mark_the_5_hour_reset(self):
        now = T0 + timedelta(days=8, hours=18)
        s = {'ts': now - timedelta(minutes=1), 'account': 'a', 'h5': (40.0, now + timedelta(hours=1, minutes=30)), 'w7': None, 'legacy': False}
        page, _ = self.page(E.window_report([s], [], None, now))
        marks = re.findall(r'<div class="mark lo" style="--x:([\d.]+)"><b>5-hour window resets 19:30</b>', page)
        self.assertEqual(len(marks), 4); self.assertAlmostEqual(float(marks[0]), 1.5 / 6, places=3)   # Today only, every agent count
        self.assertIn('<div class="big disp">40<small>% used</small></div>', page)

    def test_subscriptions_row_only_when_set(self):
        self.assertNotIn('Subscriptions', self.page()[0])
        burn = E.burn_report([(T0 + timedelta(days=8, hours=17), 'claude-code', 'm', 10, 1400.0, ('s',), 'k')], T0 + timedelta(days=8, hours=18),
                             timezone.utc)
        page, _ = self.page(subs=[{'name': 'Claude Max', 'usd': 200.0}, {'name': 'ChatGPT Pro', 'usd': 200.0}], burn=burn)
        self.assertIn('<div>Subscriptions</div><div><b>$400/mo</b><small>set by you</small><small class="ln">Claude Max + ChatGPT Pro'
                      ' · 7 days of use ≈ 3.5× the monthly price</small>', page)

    def test_subscriptions_config_is_checked(self):
        self.assertEqual(E.subscriptions({}), ([], None))
        self.assertEqual(E.subscriptions({'subscriptions': [{'name': ' Claude  Max ', 'usd_per_month': 200}]}),
                         ([{'name': 'Claude Max', 'usd': 200.0}], None))
        for bad in ([], [{'name': 'x'}], [{'name': '', 'usd_per_month': 5}], [{'name': 'x', 'usd_per_month': True}], 'Max'):
            self.assertEqual(E.subscriptions({'subscriptions': bad})[0], [])
            self.assertIn('the row is hidden', E.subscriptions({'subscriptions': bad})[1])


class CodexLimits(unittest.TestCase):
    def test_the_newest_rate_limits_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); f = root / 'x.jsonl'
            ev = lambda m, pct, info=None: json.dumps({'type': 'event_msg', 'timestamp': E.iso(NOW - timedelta(minutes=m)), 'payload': {
                'type': 'token_count', 'info': info, 'rate_limits': {'plan_type': 'pro', 'primary': {'used_percent': pct, 'window_minutes': 10080,
                                                                                                  'resets_at': 1793083161}}}})
            f.write_text('\n'.join([json.dumps({'type': 'session_meta', 'payload': {'id': 'c'}}), ev(30, 20.0), ev(10, 22.0), ev(20, 21.0)]))
            limits = []
            E.scan_burn(root / 'none', root, NOW - timedelta(days=7), E.DEFAULT_PRICES, limits)
        self.assertEqual([(x['plan'], x['windows'][0]['pct']) for x in limits], [('pro', 22.0)])


if __name__ == '__main__':
    unittest.main()
