"""First-run adoption: env-var defaults and the fresh-repo dashboard nudge. Synthetic data only."""
import os
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


if __name__ == '__main__':
    unittest.main()
