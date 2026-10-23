# Contributing

Thanks for helping. agent-estimate is one stdlib-only Python script (`estimate.py`) plus two agent skills.

## Rules

- Python 3.11+ standard library only; the only external tool is the `gh` CLI. Please do not add
  dependencies.
- Tests and examples use synthetic data only. Never commit real agent logs, `.estimate/` files, repo or
  card names from your own projects, paths or e-mails.
- The dashboard screenshot (`docs/dashboard.png`) is built from `tests/ui/fixtures.py`.
- User-visible changes get a line in `CHANGELOG.md` under "Unreleased".

## Tests

```sh
python3 -m unittest discover -s tests
uv run --with playwright python tests/ui/check_dashboard.py   # optional browser check of the dashboard
```

Run the whole suite before opening a pull request; it takes seconds. A bug fix comes with a test that
fails without it.

## Pull requests

Keep a pull request to one change, say what it changes for a user, and include the test command output.
Security problems: see SECURITY.md, not a public issue.
