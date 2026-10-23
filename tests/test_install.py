"""install / uninstall / setup: temp HOME and temp project dirs only; never the real home, gh or network."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted(d.name for d in (ROOT / 'skills').iterdir() if d.is_dir())


def gh(returncode=0):
    return lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, '', '')


def run(argv, answers=(), gh_rc=0):
    out, err = io.StringIO(), io.StringIO()
    real_run = subprocess.run
    def fake_run(cmd, *a, **k):
        return gh(gh_rc)(cmd) if cmd[0] == 'gh' else real_run(cmd, *a, **k)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            patch.object(E.subprocess, 'run', fake_run), patch('builtins.input', side_effect=list(answers)):
        try: E.main(argv)
        except SystemExit as e: return e.code, out.getvalue(), err.getvalue()
    return 0, out.getvalue(), err.getvalue()


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'home'; self.home.mkdir()
        self.bin = self.home / '.local' / 'bin'
        env = patch.dict(os.environ, {'HOME': str(self.home), 'PATH': f'{self.bin}{os.pathsep}/usr/bin'})
        env.start(); self.addCleanup(env.stop); os.environ.pop('CLAUDE_CONFIG_DIR', None)  # never the real Claude settings

    def agent_dirs(self, *names):
        for n in names: (self.home / n / 'skills').mkdir(parents=True)


class Install(HomeCase):
    def test_links_cli_and_skills_into_existing_agent_dirs_only(self):
        self.agent_dirs('.claude', '.codex')
        code, out, err = run(['install'])
        self.assertEqual(code, 0)
        self.assertEqual(os.path.realpath(self.bin / 'estimate'), str(ROOT / 'estimate.py'))
        for home in ('.claude', '.codex'):
            for s in SKILLS: self.assertEqual(os.path.realpath(self.home / home / 'skills' / s), str(ROOT / 'skills' / s))
        self.assertFalse((self.home / '.agents').exists())
        self.assertIn('gh: logged in', out)
        self.assertNotIn('not on PATH', err)

    def test_idempotent(self):
        self.agent_dirs('.claude')
        run(['install'])
        code, out, err = run(['install'])
        self.assertEqual(code, 0)
        self.assertIn('already linked', out)
        self.assertEqual(err, '')

    def test_never_overwrites_a_file_or_a_foreign_symlink(self):
        self.agent_dirs('.claude')
        skills = self.home / '.claude' / 'skills'
        (skills / SKILLS[0]).mkdir()
        (skills / SKILLS[0] / 'keep.txt').write_text('mine')
        elsewhere = self.home / 'elsewhere'; elsewhere.mkdir()
        self.bin.mkdir(parents=True); (self.bin / 'estimate').symlink_to(elsewhere)
        code, out, err = run(['install'])
        self.assertEqual(code, 0)
        self.assertTrue((skills / SKILLS[0] / 'keep.txt').exists())
        self.assertFalse((skills / SKILLS[0]).is_symlink())
        self.assertEqual(os.readlink(self.bin / 'estimate'), str(elsewhere))
        self.assertIn('is not a symlink', err); self.assertIn('points to', err)
        for s in SKILLS[1:]: self.assertTrue((skills / s).is_symlink())

    def test_dry_run_changes_nothing(self):
        self.agent_dirs('.claude')
        code, out, err = run(['install', '--dry-run'])
        self.assertEqual(code, 0)
        self.assertIn('would link', out)
        self.assertFalse(self.bin.exists())
        self.assertEqual(list((self.home / '.claude' / 'skills').iterdir()), [])

    def test_custom_bin_dir_and_path_warning(self):
        custom = self.home / 'tools'
        code, out, err = run(['install', '--bin-dir', str(custom)])
        self.assertTrue((custom / 'estimate').is_symlink())
        self.assertIn(f'{custom} is not on PATH', err)

    def test_reports_gh_problems_without_failing(self):
        code, out, err = run(['install'], gh_rc=1)
        self.assertEqual(code, 0)
        self.assertIn('gh is not logged in', err)
        with patch.object(E.subprocess, 'run', side_effect=FileNotFoundError):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
                E.main(['install'])
        self.assertIn('`gh` was not found', buf.getvalue())


class Uninstall(HomeCase):
    def test_removes_only_links_into_this_repo(self):
        self.agent_dirs('.claude')
        run(['install'])
        skills = self.home / '.claude' / 'skills'
        foreign = self.home / 'other-skill'; foreign.mkdir()
        (skills / SKILLS[0]).unlink(); (skills / SKILLS[0]).symlink_to(foreign)
        real = skills / SKILLS[1]
        code, out, err = run(['uninstall', '--dry-run'])
        self.assertTrue(real.is_symlink()); self.assertIn('would remove', out)
        code, out, err = run(['uninstall'])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.lexists(self.bin / 'estimate'))
        self.assertFalse(os.path.lexists(real))
        self.assertEqual(os.readlink(skills / SKILLS[0]), str(foreign))
        self.assertIn('does not point to', err)

    def test_keeps_links_into_the_checkout_that_are_not_this_slots_target(self):
        self.agent_dirs('.claude')
        skills = self.home / '.claude' / 'skills'
        (skills / SKILLS[0]).symlink_to(ROOT / 'skills' / SKILLS[1])  # inside the repo, wrong skill
        self.bin.mkdir(parents=True); (self.bin / 'estimate').symlink_to(ROOT / 'README.md')
        code, out, err = run(['uninstall'])
        self.assertTrue(os.path.islink(skills / SKILLS[0])); self.assertTrue(os.path.islink(self.bin / 'estimate'))
        self.assertEqual(err.count('does not point to'), 2)

    def test_leaves_real_files_alone(self):
        self.agent_dirs('.claude')
        (self.home / '.claude' / 'skills' / SKILLS[0]).mkdir()
        self.bin.mkdir(parents=True); (self.bin / 'estimate').write_text('#!/bin/sh\n')
        run(['uninstall'])
        self.assertTrue((self.home / '.claude' / 'skills' / SKILLS[0]).is_dir())
        self.assertTrue((self.bin / 'estimate').is_file())


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name).resolve() / 'proj'; self.proj.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.proj)], check=True)
        cwd = os.getcwd(); os.chdir(self.proj); self.addCleanup(os.chdir, cwd)
        env = patch.dict(os.environ); env.start(); self.addCleanup(env.stop)
        os.environ.pop('ESTIMATE_DIR', None)

    def config(self):
        return json.loads((self.proj / '.estimate' / 'config.json').read_text())

    def test_flags_markdown_default_path_does_not_create_file(self):
        code, out, err = run(['setup', '--source', 'markdown'])
        self.assertEqual(code, 0)
        self.assertEqual(self.config(), {'backlog': {'source': 'markdown', 'path': 'BACKLOG.md'}})
        self.assertFalse((self.proj / 'BACKLOG.md').exists())
        self.assertIn('does not exist yet', err)
        self.assertIn('estimate forecast --backlog', out); self.assertIn('estimate dashboard', out)
        self.assertIn('estimate the backlog', out)

    def test_path_flag_implies_markdown(self):
        run(['setup', '--path', 'docs/TODO.md'])
        self.assertEqual(self.config()['backlog'], {'source': 'markdown', 'path': 'docs/TODO.md'})

    def test_create_backlog_from_example_header_parses_empty(self):
        run(['setup', '--source', 'markdown', '--create-backlog'])
        text = (self.proj / 'BACKLOG.md').read_text()
        self.assertTrue(text.startswith('# Backlog'))
        self.assertIn('| id | title | effort | value | depends |', text)
        self.assertNotIn('Audit log', text)
        self.assertEqual(E.parse_backlog(text), [])

    def test_existing_backlog_is_never_overwritten(self):
        (self.proj / 'BACKLOG.md').write_text('mine')
        run(['setup', '--source', 'markdown', '--create-backlog'])
        self.assertEqual((self.proj / 'BACKLOG.md').read_text(), 'mine')

    def test_tiden_with_and_without_product(self):
        run(['setup', '--source', 'tiden', '--product-id', 'p-42'])
        self.assertEqual(self.config(), {'backlog': {'source': 'tiden', 'product_id': 'p-42'}})
        (self.proj / '.estimate' / 'config.json').unlink()
        run(['setup', '--source', 'tiden'])
        self.assertEqual(self.config(), {'backlog': {'source': 'tiden'}})

    def test_interactive_markdown_creates_file_on_yes(self):
        code, out, err = run(['setup'], answers=['1', '', 'y'])
        self.assertEqual(code, 0)
        self.assertEqual(self.config()['backlog'], {'source': 'markdown', 'path': 'BACKLOG.md'})
        self.assertTrue((self.proj / 'BACKLOG.md').exists())

    def test_interactive_markdown_declines_creation_by_default(self):
        run(['setup'], answers=['', 'notes/b.md', ''])
        self.assertEqual(self.config()['backlog']['path'], 'notes/b.md')
        self.assertFalse((self.proj / 'notes').exists())

    def test_interactive_tiden(self):
        run(['setup'], answers=['2', 'p-7'])
        self.assertEqual(self.config()['backlog'], {'source': 'tiden', 'product_id': 'p-7'})
        (self.proj / '.estimate' / 'config.json').unlink()
        run(['setup'], answers=['2', ''])
        self.assertEqual(self.config()['backlog'], {'source': 'tiden'})

    def test_keeps_other_config_keys(self):
        store = E.Store(self.proj / '.estimate'); store.write_json('config.json', {'since': '2026-10-01T00:00:00Z'})
        run(['setup', '--source', 'tiden'])
        self.assertEqual(self.config(), {'since': '2026-10-01T00:00:00Z', 'backlog': {'source': 'tiden'}})

    def test_rerun_keeps_values_for_omitted_options(self):
        run(['setup', '--source', 'tiden', '--product-id', 'p-42'])
        run(['setup', '--source', 'tiden'])
        self.assertEqual(self.config()['backlog'], {'source': 'tiden', 'product_id': 'p-42'})
        run(['setup'], answers=['', ''])  # interactive: Enter keeps the current source and product
        self.assertEqual(self.config()['backlog'], {'source': 'tiden', 'product_id': 'p-42'})
        run(['setup', '--source', 'markdown', '--path', 'docs/TODO.md'])
        run(['setup', '--source', 'markdown'])
        self.assertEqual(self.config()['backlog'], {'source': 'markdown', 'path': 'docs/TODO.md'})
        run(['setup'], answers=['1', '', ''])
        self.assertEqual(self.config()['backlog'], {'source': 'markdown', 'path': 'docs/TODO.md'})

    def test_next_steps_follow_the_source(self):
        md = run(['setup', '--source', 'markdown'])[1]
        self.assertIn('estimate forecast --backlog', md); self.assertNotIn('backlog pull', md)
        td = run(['setup', '--source', 'tiden'])[1]
        self.assertLess(td.index('estimate backlog pull'), td.index('estimate forecast --backlog'))

    def test_conflicting_flags_and_bad_choice_are_errors(self):
        self.assertNotEqual(run(['setup', '--source', 'tiden', '--path', 'x.md'])[0], 0)
        self.assertNotEqual(run(['setup', '--source', 'markdown', '--product-id', 'p'])[0], 0)
        self.assertNotEqual(run(['setup'], answers=['3'])[0], 0)
        self.assertFalse((self.proj / '.estimate' / 'config.json').exists())

    def test_invalid_existing_config_is_reported_not_overwritten(self):
        (self.proj / '.estimate').mkdir(); (self.proj / '.estimate' / 'config.json').write_text('{oops')
        code, out, err = run(['setup', '--source', 'tiden'])
        self.assertNotEqual(code, 0)
        self.assertEqual((self.proj / '.estimate' / 'config.json').read_text(), '{oops')


if __name__ == '__main__':
    unittest.main()
