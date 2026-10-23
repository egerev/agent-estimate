# agent-estimate agent rules

## Project rules

- Python 3.11+ stdlib only; the only external tool is `gh`. Do not add dependencies.
- Tests: `python3 -m unittest discover -s tests`. The browser check
  (`uv run --with playwright python tests/ui/check_dashboard.py`) is optional.
- Examples and test fixtures are synthetic; the dashboard screenshot is built from
  `tests/ui/fixtures.py`.
- User-visible changes go to CHANGELOG.md.
