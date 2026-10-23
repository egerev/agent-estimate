"""Blind retro-sizing packet: PR boundaries, the shared SP scale, mechanical-PR flags. Synthetic PRs only."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimate as E  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def pr(number, title, body='', files=(), changed=None):
    files = [{'path': p, 'additions': a, 'deletions': d} for p, a, d in files]
    return {'number': number, 'title': title, 'body': body, 'files': files, 'commits': [{}],
            'changedFiles': changed if changed is not None else len(files),
            'additions': sum(f['additions'] for f in files), 'deletions': sum(f['deletions'] for f in files)}


def sections(md):
    """PR number -> its text, cut at the packet's own `## PR #n` headings only."""
    parts = re.split(r'(?m)^## PR #(\d+): .*$', md)
    return {int(n): text for n, text in zip(parts[1::2], parts[2::2])}


class Boundaries(unittest.TestCase):
    def test_each_body_stays_under_its_own_pr(self):
        prs = [pr(27, 'Snapshot plus work', '## What this is\r\nBody of 27.\r\n## PR #28: fake heading\r\nmore 27', [('a.py', 5, 1)]),
               pr(28, 'Overhaul', 'Body of 28.\n\n## Follow-up\nlater', [('b.py', 3, 0)]),
               pr(43, 'Test only', '', [('t.py', 4, 0)])]
        md = E.retro_packet_md('o/r', prs, 'sizes.json')
        got = sections(md)
        self.assertEqual(sorted(got), [27, 28, 43])                        # the fake heading in #27 is escaped
        self.assertIn('Body of 27.', got[27]); self.assertIn('more 27', got[27]); self.assertNotIn('Body of 28', got[27])
        self.assertIn('> \\## PR #28: fake heading', got[27])
        self.assertIn('> \\## Follow-up', got[28]); self.assertNotIn('Body of 27', got[28])
        self.assertIn('> (empty)', got[43])
        for n in (27, 28, 43):
            self.assertIn(f'Body of PR #{n}:', got[n]); self.assertIn(f'(end of PR #{n})', got[n])
        self.assertNotIn('\r', md)

    def test_long_body_is_cut_inside_its_own_pr(self):
        md = E.retro_packet_md('o/r', [pr(1, 'a', 'x' * 5000), pr(2, 'b', 'y')], 'sizes.json')
        got = sections(md)
        self.assertIn('x' * E.PACKET_BODY + ' …', got[1]); self.assertNotIn('x' * (E.PACKET_BODY + 1), md)
        self.assertIn('(end of PR #1)', got[1])


class Scale(unittest.TestCase):
    def test_packet_and_backlog_skill_say_the_points_are_one_unit(self):
        md = E.retro_packet_md('o/r', [], 'sizes.json')
        self.assertIn('(1, 2, 3, 5, 8, 13)', md)
        self.assertIn('same unit as `estimate-task` and `estimate-backlog`', md)
        self.assertIn('a merged PR cannot be split', md)
        skill = (ROOT / 'skills' / 'estimate-backlog' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('allows 13 because a merged PR cannot be split', skill)


class Mechanical(unittest.TestCase):
    def test_many_files_bulk_paths_or_title_words_are_flagged(self):
        self.assertEqual(E.mechanical_reasons(pr(1, 'feat: x', files=[('src/a.py', 10, 2)], changed=120)), ['120 files'])
        lock = pr(2, 'feat: deploy', files=[('web/package-lock.json', 400, 300), ('web/app.ts', 50, 10)])
        self.assertEqual(E.mechanical_reasons(lock), ['92% of listed lines in lockfiles, snapshots, drafts, generated or binary files'])
        self.assertEqual(E.mechanical_reasons(pr(3, 'docs: commit research drafts', files=[('docs/x.md', 9, 0)])),
                         ['title mentions drafts'])
        self.assertEqual(E.mechanical_reasons(pr(4, 'big', 'an inherited WIP snapshot', files=[('a.py', 1, 1)], changed=60)),
                         ['60 files', 'body mentions snapshot, wip'])

    def test_ordinary_pr_is_not_flagged_by_body_words_alone(self):
        p = pr(5, 'feat(calls): menu keys', 'Updated the test snapshot; regenerated the schema.', [('src/calls.py', 80, 20)])
        self.assertEqual(E.mechanical_reasons(p), [])
        self.assertNotIn('Mechanical?', sections(E.retro_packet_md('o/r', [p], 's.json'))[5])

    def test_flag_is_printed_under_the_pr(self):
        md = E.retro_packet_md('o/r', [pr(36, 'docs: commit drafts', files=[('drafts/a.md', 900, 0)], changed=1100)], 's.json')
        self.assertIn('**Mechanical?** 1100 files; 100% of listed lines in lockfiles, snapshots, drafts, generated or binary '
                      'files; title mentions drafts. Line counts may overstate the work.', sections(md)[36])


if __name__ == '__main__':
    unittest.main()
