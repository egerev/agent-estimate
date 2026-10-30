# Changelog

## 0.4.0

### Changed
- **`dashboard --all` reads the agent logs once**: every project reuses one Claude and Codex scan (prices are
  applied per project), so `--all` takes about as long as one dashboard instead of one per project. All projects
  of a run share one clock.
- **Usage by repo**: a session in a folder with no git repo is counted under its top folder below `$HOME`, named
  as a folder (`~/notes (folder)`), so project directories under one folder pool; a path outside `$HOME` keeps
  its path plus `(folder)`.
- **`gpt-6-sol` price**: Codex transcripts name `gpt-6-sol` (about 210 M tokens in 30 days were unpriced). Added at
  OpenAI's standard list price (input $2, cached $0.20, output $10 per M; above 272K input tokens $4 / $0.40 /
  $15), from https://developers.openai.com/api/docs/pricing, checked 2026-10-26 together with `gpt-6.1-sol` and
  `gpt-6-astra`. Run `estimate record --refresh` to price past PRs that used it.

### Added
- **`estimate usage [--days 7|30|90] [--json] [--no-labels]` and a Usage section on the dashboard** (30 or 90
  days, switched without a script): $ and tokens per repo (a session's cwd maps to its git remote, so worktrees
  pool; a removed worktree follows its siblings, the registry or its pr-links), the top 10 sessions by $ with
  date, agent, model and a 60-character first-line label (`--no-labels` or `"session_labels": false` in
  `config.json` hides it), cache tiles (processed tokens, cached input share, uncached input, output, cache
  savings in $), daily $ by agent and by model as static SVG bars, parallel sessions (median and p80 over active
  15-minute slots, and "you usually run ~N" next to the planner's agent count), merged PRs per session, and a
  model table with $, share, tokens and an approximate flag for a model without a price.
- **Data dir discovery**: without `--dir` / `$ESTIMATE_DIR`, `estimate` uses the nearest `.estimate/` above the
  current directory (with a ledger, history or config), else the entry for the repo's git remote in
  `~/.config/agent-estimate/projects.json` (`$ESTIMATE_REGISTRY`), so every worktree and agent of a repo finds
  the same data. `estimate setup` writes the entry; `estimate link --dir D [--repo R] [--claude-project-dir P]`
  sets it by hand. `--repo` now defaults from the git remote before asking `gh`.
- **`estimate doctor`**: which data dir, repo and Claude transcript dirs were resolved and from where, ledger
  and history counts, calibration n, `gh` auth, the status-line hook, and the full sample audit. Writes nothing.
- **`estimate plan`**: critical-chain forecast of the backlog. A seeded Monte Carlo of a list scheduler with
  K agents gives p50/p80 finish dates per agent count, what one more agent saves, the max useful agent count,
  the project buffer (p80 − p50), the critical chain, waves with dates, and blocked cards. Card time comes from
  the repo's merged PRs (work rate per SP, scaled to the calendar pace, plus the merge wait). `--agents`,
  `--top`, `--json`. Unsized cards are skipped with a warning; unknown or cyclic dependencies are excluded and
  named; maybe-done cards in scope are counted.
- **Lanes**: an optional `lane` column with caps (`open`, `merging`) from a `## Lanes` section of the backlog
  file or `lanes` in `.estimate/config.json` (config wins).
- **Ahead** section on the dashboard: the next 20 cards in agent lanes for the selected agent count, the
  critical chain highlighted, waiting cards dimmed, p50 marker and p80 buffer.
- **Depends from Tiden card text**: `backlog pull` fills an empty depends cell from the card's `Depends on`
  line, now also by key (KEY-44) or product code and number (ACME-225; the code comes from `tiden product
  list`). A hand-filled cell, or `-` for none, is never overwritten.
- **Wall clock lines** in `forecast --backlog`: 1, 2, 3 and 5 agents in parallel, never shorter than the
  longest dependency chain.
- **`record --all`, `dashboard --all`**: every project in `~/.config/agent-estimate/projects.json`, one line
  each (settled, new PRs, calibration n, pre-work n); a failing project is named and the loop goes on; the
  exit status is non-zero only when every project failed. For a nightly job.
- **`no PR` estimates**: `record` marks a branch estimate whose branch has no open or merged PR after 7 days
  (`--no-pr-days N`, 0 never). It stays in the ledger, leaves the pending list and the audit, is listed by
  `estimate doctor`, and a PR merged later still settles it.

### Changed
- The dashboard reads 90 days of agent logs once (the burn rows still count today and 7 days), so it takes a
  few seconds longer on large log folders.
- **Pre-work timing**: agent activity at most 10 minutes before the estimate, with no commit or PR before it,
  is the sizing itself and no longer moves `start_at` ahead of the estimate, so the estimate counts as made
  before work. Run `estimate record --refresh` once to remeasure settled PRs.
- **Wider, calibrated intervals**: p20–p80 quotes are widened to the leave-one-out forecast error of the
  window (the p50 is unchanged), so they hold about 60% of new tasks instead of about 45%.
- **`--claude-project-dir` checkouts**: a checkout path is mapped to its transcript directory under
  `~/.claude/projects`; `link` and `doctor` warn about a path that is neither.
- **Re-pull keeps more**: `backlog pull` keeps a `lane` column and any sections after the table (`## Lanes`).
  An empty depends cell is no longer kept empty: it takes the card's `Depends on` line; write `-` for none.
- **Empty data dir stops**: `forecast` (except logging with `--task`), `forecast --backlog` and `dashboard`
  exit non-zero with one message naming the dir and the 3 ways to point elsewhere, instead of n=0 numbers.
- **Hours wording**: backlog hours read "agent active time, summed (one agent working serially; not wall
  clock)"; the dashboard says the same and labels the "All … SP" rows as wall clock.
- **Quieter forecasts**: the sample audit (PRs without agent logs, out of the window, pending estimates) is
  one summary line in `forecast`; the full list is in `estimate doctor` and `estimate record`.
- **Fresh repo nudge** also shows on a dashboard with no backlog yet.
- Skills `estimate-task` and `estimate-backlog` start with `estimate doctor` when unsure of the data dir.

### Fixed
- **Removed worktrees**: a Claude session whose `cwd` was removed after merge (a worktree) is attributed again
  when a session of the same transcript directory in that `cwd` was identified; remeasuring after cleanup
  used to drop it and undercount active hours and dollars.

## 0.3.0

Teammate dashboard, plan windows, and fixes from the first outside setups.

### Added
- **Plan windows**: the dashboard's window cards show the Claude 5-hour and weekly % with reset times and
  the Codex week (plan, reset, snapshot age). `estimate install --statusline` (opt-in) wraps the Claude
  Code status line with `estimate statusline-log`, which logs window %, reset times and a one-way account
  tag, then runs your own status line; `estimate uninstall` restores it. A status line's
  `~/.claude/data/rate_history.jsonl` is read too. A passed reset or an old % is never shown as current.
- **Window price**: rough API-equivalent $ per 5-hour window and per week from clean hours of snapshots
  (one account, no reset), hidden under 10 clean hours. With it, the planner says how much of each window
  a box would take, and the Today lanes mark the 5-hour reset.
- **Subscriptions**: plans typed into `.estimate/config.json` (`subscriptions`: name, `usd_per_month`)
  show their monthly total against 7 days of API-equivalent use; the row is hidden until set.
- **`ESTIMATE_CLAUDE_PROJECT_DIR`** sets the default for `--claude-project-dir` (several joined with
  `:`), next to the existing `ESTIMATE_DIR` and `ESTIMATE_REPO`: data kept outside the repo needs no alias
  or wrapper. The README shows them and a shell function for several projects.
- **Fresh repo nudge**: with a backlog but no settled history, the dashboard points to
  `estimate init --last 30` to seed the pace from recent merged PRs.
- **Open-source files**: CONTRIBUTING.md, SECURITY.md (private advisories), a bug report template, and
  a Privacy summary near the top of the README.
- **`maybe-done` cards**: `backlog pull` reads Tiden sources (`--include-sources`) and notes open cards
  linked to a pull request or code anchors (`note` column). They stay in the totals; `forecast` marks
  them `maybe done` with a warning, and the dashboard names them in one line.
- **Short history warning**: under 7 days of merge history, `forecast` and the dashboard say calendar
  days are unreliable.
- **Codex prices**: `gpt-6-astra` and `gpt-6.1-sol` at OpenAI list prices as of 2026-10, with the
  long-context tier.
- **`forecast --backlog --skip-unsized`** leaves rows without effort out of the totals and the order,
  with a warning that names them, instead of stopping at the first one. An item that depends on a
  skipped row is kept in file order with its own warning; `--json` lists them under `unsized`.

- **Burn rows**: `estimate dashboard` reads the last 7 days of local Claude Code transcripts
  (`message.usage`, one count per message and request id) and Codex sessions (`token_count` deltas) of
  every repo, and shows tokens, API-equivalent $ and agent hours for today and 7 days, per agent and top
  models. Models without a price stay unpriced. Only counts, $ and model names reach the page; `--no-burn`
  skips the scan, and unreadable logs never stop the dashboard.

### Changed
- **Teammate dashboard** replaces dashboard v2. The planner answers "which cards get done in this time
  box with K agents": Today (hours left) · 1, 2, 3 days · Week × 1, 2, 3, 5 agents, 20 precomputed
  states switched by CSS (no script). It shows cards and SP with sure, likely and maybe waterlines (p80,
  median, p20 of wall hours per SP or one agent's merge pace), the agent hours, $ and tokens of the
  likely SP, and about how many agents all SP would need in that box (or that a card or chain is longer than the box).
  Agent lanes draw the schedule: cards never split, dependencies first ("likely by
  here"; waits are not drawn); the backlog list (top 12) marks each row's waterline. Rates carry their
  sample sizes: SP/day, agent work, $ and tokens per SP, days for all SP with 1, 2, 3, 5 agents. Plan
  windows say "no snapshot yet" until window snapshots are collected. Dark first, light variant.
- **Containers are left out of the pull**: a Tiden card with child cards (`childrenCount` > 0) is never
  sized or counted, even when its children are closed; the pull names them in a note and the file header.
- **Labels**: an id written by hand (`1`) is its own label even when the row has a key; the key shows
  next to the title. Keys still name UUID ids.
- **Dashboard v2: one simple screen.** "Backlog: about N days with [1 2 3 5] agents" (one agent by
  default), the range, agent hours and cost, one plain line on how far to trust it, and the backlog by
  Score (top 10, then "show all"; "waits for" as a small mark). The old charts, per-estimator tables,
  price list, backtest block and open PRs are gone; that detail stays in the CLI. No scripts; building
  it makes no network calls (`--offline` is accepted and does nothing).
- **Dashboard source line** names Tiden and the product for a pulled backlog
  (`Tiden · Shop · 52 cards · updated <pull time>`), never the local `backlog.md`. `backlog pull`
  reads the product name read-only from `tiden product list`.
- **Unsized cards** no longer stop the dashboard: they are listed at the end and left out of the totals.
- **Tiden pull takes status Backlog and Review** by default. Set `"statuses"` under `backlog` in
  `.estimate/config.json` to pull fewer or others. The pulled table gains a `key` column (e.g. KEY-21) when a card has a human key.
- **Short ids**: UUID ids show as their first 8 characters when unique in the file (or as the key),
  and `depends` accepts the full id, the key or that prefix. The full id stays stored.
- **Order marks**: an item placed before higher-Score work that depends on it shows `unblocks #n`.
- **Skill `estimate-backlog`**: size the remaining work only, never size epics/containers, and pass
  `--dir`/`--repo` when the data directory lives elsewhere. Retro packet and skill state that both
  use the same points (13 only for past PRs, which cannot be split).
- **Retro packet**: PRs with 50+ files or mostly lockfile/snapshot/draft/generated lines are marked
  **Mechanical?**.

### Fixed
- **Backlog totals** no longer shrink with backlog length: a two-level bootstrap adds the uncertainty of
  rates known from n PRs; confidence follows the per-item spread.
- **Calendar days** keep merges on the partial first and last days of the window (they were dropped once
  three full days existed, which could make days several times slower than the window's SP/day).
- **Explained numbers:** what calendar days and agent hours are based on, a "waits dominate" warning,
  why calibration and $ sample sizes are below the sized PRs (per reason, with PR numbers), and what
  `record` checked when nothing settled.
- **Retro packet boundaries**: Markdown headings in a PR body (`## What`, `## Follow-up`) read like
  the start of another PR. They are now escaped, each body is labelled `Body of PR #n`, and every PR
  ends with `(end of PR #n)` and a rule.
- **A re-pull deleted hand-written rows**: `backlog pull` dropped every row whose id is not in Tiden, so a
  manual row (id `1`) vanished as "no longer in Tiden". Only rows imported from Tiden (a UUID id) are
  dropped now; hand-written rows are kept with all their cells.
- **Codex $ was missing for most PRs**: Codex logs every `token_usage_record` again as a `token_count`
  without ids a moment later; that echo was treated as an ambiguous duplicate and made the PR's cost
  unknown. An id-less `token_count` right after an identified record with the same usage and model is
  now counted as that response (in one repo: $ known for 29 of 30 PRs, was 3).

## 0.2.0

First version meant to be shared.

### Added
- **Backlog model**: items have effort, value and depends. `forecast --backlog` computes
  Score = value ÷ effort and orders by it, never placing an item before its dependencies
  (shows `waits for #n`). Markdown table format; the old `<SP> <title>` lines still work.
  New skill `estimate-backlog`.
- **Learning loop**: once an estimator (agent and model) has 5 settled pre-work estimates, its
  forecasts use its own hours and $ per SP. The dashboard shows backtest accuracy against a
  "median of the last 10 PRs" baseline. `estimate compare` puts several projects side by side.
- **Backlog sources**: Markdown (default) and a read-only pull of open Tiden cards
  (`estimate backlog pull`), chosen in `.estimate/config.json`.
- **`estimate install`, `uninstall`, `setup`**: put `estimate` on PATH, link the skills into
  Claude Code, Codex and `.agents`, and ask where the backlog lives.
- **Dashboard v1**: backlog total (SP, agent hours, calendar days with 1/2/3/5 parallel agents
  floored by the dependency critical path, $), backlog in Score order, and a "how much to trust this" block.

### Fixed
- **Measurement**: time attribution to PRs, cost completeness (an incomplete $ total is no longer
  used in forecasts), forecast confidence, repository identity checks for agent logs.

### Changed
- README rewritten for first-time readers; dashboard screenshot from synthetic data.

## 0.1.1

Initial v0: Fibonacci story points per estimator, measured actuals per merged PR, static dashboard.
