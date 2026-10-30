"""First-run adoption: env-var defaults, data-dir discovery (walk-up, registry, link), doctor, the empty-dir stop,
wall-clock lines, the one-line audit and the fresh-repo dashboard nudge. Synthetic data only."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import estimate as E  # noqa: E402
from test_dashboard import items  # noqa: E402
from test_estimate import T0  # noqa: E402


def parsed(argv, env):
    """The namespace and ctx() that `estimate <argv>` would run with under `env`."""
    seen = {}
    with patch.dict(os.environ, env), patch.object(E, 'cmd_backtest', lambda a: seen.update(args=a, ctx=E.ctx(a))):
        E.main(argv + ['backtest'])
    return seen['args'], seen['ctx']


class EnvDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.a, self.b = str(Path(self.tmp.name) / 'a'), str(Path(self.tmp.name) / 'b')

    def test_env_vars_set_dir_repo_and_trusted_claude_dirs(self):
        args, (repo, store, _) = parsed([], {'ESTIMATE_DIR': self.a, 'ESTIMATE_REPO': 'acme/shop',
                                             'ESTIMATE_CLAUDE_PROJECT_DIR': os.pathsep.join([self.a, self.b])})
        self.assertEqual((repo, str(store.dir), args.claude_project_dir), ('acme/shop', self.a, [self.a, self.b]))

    def test_flags_override_env(self):
        args, (repo, store, _) = parsed(['--dir', self.b, '--repo', 'acme/web', '--claude-project-dir', self.b],
                                        {'ESTIMATE_DIR': self.a, 'ESTIMATE_REPO': 'acme/shop', 'ESTIMATE_CLAUDE_PROJECT_DIR': self.a})
        self.assertEqual((repo, str(store.dir), args.claude_project_dir), ('acme/web', self.b, [self.b]))

    def test_no_env_trusts_no_extra_claude_dir(self):
        env = {k: v for k, v in os.environ.items() if k != 'ESTIMATE_CLAUDE_PROJECT_DIR'}
        with patch.dict(os.environ, env, clear=True):
            args, _ = parsed(['--dir', self.a, '--repo', 'acme/shop'], {})
        self.assertEqual(args.claude_project_dir, [])


class FreshRepoDashboard(unittest.TestCase):
    def test_no_history_points_to_init(self):
        now = T0 + timedelta(days=8)
        page = E.dashboard_html('acme/shop', [], E.calibrate([], now, 14), items((1, 'a', 3, 5, '')), now, None, (), None, timezone.utc)
        self.assertIn('estimate init --last 30', page)

    def test_no_history_and_no_backlog_still_points_to_init(self):
        now = T0 + timedelta(days=8)
        page = E.dashboard_html('acme/shop', [], E.calibrate([], now, 14), None, now, None, (), None, timezone.utc)
        self.assertIn('estimate init --last 30', page)


def git_repo(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    if remote: subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin', remote], check=True)
    return path


def cli(argv, cwd, env=None):
    """(exit code, stdout, stderr) of `estimate <argv>` run in `cwd`; gh is never called."""
    out, err = io.StringIO(), io.StringIO(); old = os.getcwd(); os.chdir(cwd); code = 0
    real = subprocess.run
    fake = lambda cmd, *a, **k: (subprocess.CompletedProcess(cmd, 1, '', 'no gh in tests') if cmd[0] == 'gh' else real(cmd, *a, **k))
    try:
        with patch.dict(os.environ, env or {}), patch.object(E.subprocess, 'run', fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try: E.main(argv)
            except SystemExit as e: code = e.code if isinstance(e.code, int) else 1; err.write(str(e.code or ''))
    finally: os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {'ESTIMATE_REGISTRY': str(self.root / 'projects.json')}); env.start(); self.addCleanup(env.stop)
        for k in ('ESTIMATE_DIR', 'ESTIMATE_REPO', 'ESTIMATE_CLAUDE_PROJECT_DIR'): os.environ.pop(k, None)
        self.data = self.root / 'data'; self.data.mkdir(); (self.data / 'history.json').write_text('[]')

    def test_remote_gives_repo_in_any_worktree_form(self):
        for url in ('git@github.com:acme/shop.git', 'https://github.com/acme/shop', 'ssh://git@github.com/acme/shop.git'):
            repo = git_repo(self.root / url.replace('/', '_').replace(':', '_'), url)
            self.assertEqual(E.remote_repo(repo), 'acme/shop')
        self.assertIsNone(E.remote_repo(git_repo(self.root / 'local', str(self.root / 'elsewhere'))))   # a path, not a host
        for url in ('git@gitlab.com:acme/shop.git', 'file://localhost/tmp/acme/shop.git', 'https://github.com/a/b/c'):
            self.assertIsNone(E.remote_repo(git_repo(self.root / url.replace('/', '_').replace(':', '_'), url)), url)   # never another repo's key
        self.assertIsNone(E.remote_repo(git_repo(self.root / 'none')))

    def test_walk_up_finds_the_nearest_data_dir_and_skips_empty_ones(self):
        top = self.root / 'proj'; (top / '.estimate').mkdir(parents=True); (top / '.estimate' / 'config.json').write_text('{}')
        sub = top / 'a' / 'b'; (sub / '.estimate').mkdir(parents=True); (sub / '.estimate' / 'calibration.json').write_text('{}')
        self.assertEqual(E.walk_up_dir(sub), top / '.estimate')    # a stray dir with only calibration.json is skipped

    def test_link_then_every_worktree_resolves_dir_repo_and_claude_dir(self):
        a = git_repo(self.root / 'main', 'git@github.com:acme/shop.git')
        code, out, _ = cli(['link', '--dir', str(self.data), '--claude-project-dir', str(self.root / 'cp')], a)
        self.assertEqual(code, 0, out); self.assertIn('linked acme/shop', out)
        self.assertEqual(json.loads((self.root / 'projects.json').read_text()),
                         {'acme/shop': {'dir': str(self.data), 'claude_project_dir': str(self.root / 'cp')}})
        wt = git_repo(self.root / 'wt', 'https://github.com/acme/shop.git')   # another checkout of the same repo
        seen = {}
        with patch.object(E, 'cmd_backtest', lambda args: seen.update(r=E.resolve(args), cp=args.claude_project_dir)):
            os.chdir(wt); self.addCleanup(os.chdir, Path(__file__).parent)
            E.main(['backtest'])
        self.assertEqual((seen['r']['dir'], seen['r']['repo'], seen['r']['repo_from'], seen['cp']),
                         (str(self.data), 'acme/shop', 'git remote', [str(self.root / 'cp')]))
        self.assertIn('registry', seen['r']['dir_from'])

    def test_flag_and_env_beat_the_registry(self):
        a = git_repo(self.root / 'main', 'git@github.com:acme/shop.git')
        E.write_registry('acme/shop', {'dir': str(self.data)})
        other = self.root / 'other'; other.mkdir()
        _, out, _ = cli(['--dir', str(other), 'doctor'], a)
        self.assertIn(f'data dir     {other}  (from --dir)', out)
        _, out, _ = cli(['doctor'], a, {'ESTIMATE_DIR': str(other)})
        self.assertIn('(from $ESTIMATE_DIR)', out)

    def test_damaged_registry_fields_are_ignored_not_fatal(self):
        (self.root / 'projects.json').write_text(json.dumps({'acme/shop': {'dir': 42, 'claude_project_dir': 42}, 'x/y': 'oops'}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err): self.assertEqual(E.read_registry(), {})
        self.assertIn("ignoring damaged fields of 'acme/shop'", err.getvalue())
        a = git_repo(self.root / 'main', 'git@github.com:acme/shop.git')
        code, out, _ = cli(['--dir', str(self.data), 'doctor'], a)
        self.assertEqual(code, 0); self.assertIn('(from --dir)', out)

    def test_unwritable_registry_never_fails_setup(self):
        a = git_repo(self.root / 'main', 'git@github.com:acme/shop.git')
        with patch.object(E, 'write_registry', side_effect=PermissionError(13, 'Permission denied')):
            code, out, err = cli(['setup', '--source', 'markdown'], a)
        self.assertEqual(code, 0); self.assertIn('next steps', out); self.assertIn('could not write', err)
        self.assertTrue((a / '.estimate' / 'config.json').exists())

    def test_setup_registers_the_repo(self):
        a = git_repo(self.root / 'main', 'git@github.com:acme/shop.git')
        code, out, _ = cli(['setup', '--source', 'markdown'], a)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.root / 'projects.json').read_text())['acme/shop']['dir'], str(a / '.estimate'))

    def test_link_needs_a_dir_and_a_repo(self):
        code, _, err = cli(['link', '--dir', str(self.data)], self.root)
        self.assertNotEqual(code, 0); self.assertIn('pass --repo owner/name', err)


class EmptyDataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {'ESTIMATE_REGISTRY': str(self.d / 'projects.json')}); env.start(); self.addCleanup(env.stop)
        (self.d / 'b.md').write_text('| id | title | effort |\n|---|---|---|\n| 1 | a | 2 |\n')

    def test_backlog_dashboard_and_bare_quote_stop_with_one_message(self):
        for argv in (['forecast', '--backlog', str(self.d / 'b.md')], ['forecast', '--sp', '3'],
                     ['dashboard', '--backlog', str(self.d / 'b.md'), '--no-burn']):
            code, out, err = cli(['--repo', 'acme/shop', '--dir', str(self.d), *argv], self.d)
            self.assertNotEqual(code, 0, argv); self.assertNotIn('n=0', out + err)
            self.assertIn(f'empty data dir {self.d} (from --dir)', err)
            for way in ('--dir DIR', 'export ESTIMATE_DIR=DIR', 'estimate link --dir DIR'): self.assertIn(way, err)
        self.assertFalse((self.d / 'calibration.json').exists())   # the stop comes before anything is written

    def test_logging_an_estimate_still_starts_a_fresh_ledger(self):
        code, _, _ = cli(['--repo', 'acme/shop', '--dir', str(self.d), 'forecast', '--sp', '3', '--task', 'feat/x'], self.d)
        self.assertEqual(code, 0); self.assertTrue((self.d / 'ledger.jsonl').exists())

    def test_doctor_reports_an_empty_dir_and_writes_nothing(self):
        code, out, _ = cli(['--repo', 'acme/shop', '--dir', str(self.d), 'doctor'], self.d)
        self.assertEqual(code, 0)
        self.assertIn('ledger       empty', out); self.assertIn('repo         acme/shop  (from --repo)', out)
        self.assertIn('gh auth      not logged in', out); self.assertIn('status line  not installed', out)
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ['b.md'])


class OneLineAudit(unittest.TestCase):
    AUDIT = {'used': 15, 'sized_prs': 27, 'dropped': {'merged before the 14-day window': [1, 2, 3]}, 'pending': ['feat/a'],
             'metrics': {'active_h': {'label': 'observed active h', 'n': 11, 'of': 15, 'missing': {'no agent logs matched the PR branch': [33, 34, 35, 39]}},
                         'usd': {'label': 'API-equivalent $', 'n': 10, 'of': 15,
                                 'missing': {'no agent logs matched the PR branch': [33, 34, 35, 39], 'no price for codex/x': [40]}}}}

    def test_forecasts_get_one_line_and_doctor_the_list(self):
        self.assertEqual(E.audit_summary(self.AUDIT),
                         'calibration n=15 of 27 sized PRs; 3 outside the window, 4 PRs without agent logs, '
                         '1 PR without a full $ or active-time measure, 1 estimate awaiting `estimate record`: see `estimate doctor`')
        self.assertIn('#33, #34, #35, #39', '\n'.join(E.audit_lines(self.AUDIT)))
        self.assertIsNone(E.audit_summary({**self.AUDIT, 'dropped': {}, 'pending': [], 'metrics': {}}))
        self.assertIn('2 repeated estimates of the same PRs count once',
                      E.audit_summary({**self.AUDIT, 'dropped': {}, 'pending': [], 'metrics': {}, 'duplicates': ['#7'], 'extra_estimates': 1}))


class WallClock(unittest.TestCase):
    DAYS = {'lo': 2.0, 'mid': 4.0, 'hi': 8.0, 'kind': 'p20-p80'}

    def test_one_two_three_five_agents_never_below_the_critical_path(self):
        chain = [{'id': '1', 'sp': 5}, {'id': '2', 'sp': 3, 'depends': ['1']}, {'id': '3', 'sp': 1}, {'id': '4', 'sp': 1}]
        lines = E.wall_clock_lines(chain, self.DAYS)
        self.assertEqual([l.split()[2] for l in lines[:4]], ['1', '2', '3', '5'])
        self.assertTrue(lines[0].startswith('wall clock, 1 agent:  2–8 d (median 4 d; p20-p80'))
        self.assertIn('wall clock, 2 agents: 1.6–6.4 d (median 3.2 d; p20-p80; the 8 SP dependency chain sets the floor)', lines)
        self.assertIn('wall clock, 5 agents: 1.6–6.4 d', lines[3])      # 8 of 10 SP are one chain: more agents do not help
        self.assertIn('lower bound', lines[-1])

    def test_a_loop_says_so_instead_of_numbers(self):
        loop = [{'id': 'a', 'sp': 2, 'depends': ['b']}, {'id': 'b', 'sp': 2, 'depends': ['a']}]
        self.assertIn('dependencies form a loop', E.wall_clock_lines(loop, self.DAYS)[0])

    def test_backlog_output_labels_summed_hours(self):
        self.assertEqual(E.ACTIVE_LABEL, 'agent active time, summed (one agent working serially; not wall clock)')


if __name__ == '__main__':
    unittest.main()
