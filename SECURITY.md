# Security

Please report vulnerabilities privately through GitHub: the repository's **Security** tab →
**Report a vulnerability** (a private security advisory). Do not open a public issue.

Include the version (`estimate --version`), what you ran and what happened. Do not attach `.estimate/`
files, agent logs or anything with tokens, paths or private repo names; a minimal synthetic reproduction
is best.

agent-estimate runs locally: it reads your agent logs, calls `gh` against your own GitHub (and the
`tiden` CLI read-only, only for `estimate backlog pull`), writes only local files, and sends nothing
anywhere else. Reports about data leaving the machine, files written
outside `.estimate/` and the install symlinks, or HTML injection into the dashboard are especially welcome.
