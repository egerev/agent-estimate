---
name: estimate-task
description: Size a coding task in Fibonacci story points and get measured hour and dollar ranges from the `estimate` script. Use when asked to estimate a task, "how long will this take", "сколько займёт", story points, or before starting a plan with several parts. The model gives story points only; hours and dollars come from the script's measurements of this repo.
---

# Estimate a task in story points

You size work. The `estimate` script prices it from what past merged PRs in this repo really cost,
grouped by who estimated them (agent + model). You never state hours, days or dollars yourself:
agents are known to over-predict their own runtime several times over, and people's calendars do
not apply to agents.

## 0. Check where the data are

If unsure which data the script uses (a worktree, a data dir outside the repo, a new shell), run
`estimate doctor` first. It names the data dir, repo and Claude transcript dirs it resolved and from
where, with the calibration n. To point it elsewhere: `--dir` (before the subcommand),
`export ESTIMATE_DIR=...`, or once per repo `estimate link --dir <dir>` (every worktree then finds it).
An empty data dir stops `forecast` with that message: fix the dir, do not quote numbers without one.

## 1. Size the task

Pick one Fibonacci number: **1, 2, 3, 5, 8, 13**. Judge scope, uncertainty and risk the way you
would before starting. Sizes are relative: a 2 is about twice a 1; a 5 is a real chunk of work.

Rough generic anchors (any repo):

- **1** a one-line fix, a config or copy change, a version bump
- **2** a small bug fix in one module, with a test
- **3** a feature slice in one module, with tests
- **5** a change across a few modules, or a new command or endpoint with tests and docs
- **8** a new subsystem slice, a migration plus code plus tests, or a cross-cutting refactor
- **13** too big to estimate: **split it**

Rule: a task of 13 or more must be split into parts of at most 8 SP before work starts. Size
and forecast each part on its own. The script refuses to price 13.

Optional: if a past PR in this repo feels like the same size, name it as the closest PR
(`--closest 123`). `estimate refs --paths <likely paths> --sp N` lists candidates. This is
optional; it is recorded so the repo can later learn whether anchors make estimates more
consistent.

## 2. Get the forecast and log the estimate

```sh
estimate forecast --sp 3 --task <PR number or branch> --model <your exact model id> [--agent <harness>] [--closest 123]
```

- `--task` logs the estimate; after the PR merges, `estimate record` measures the actuals
  (time, cost, who executed it) on its own. Use the branch name if no PR exists yet.
- `--model` is your own model id (for example `claude-opus-5-5`). `--agent` is detected
  (`claude-code`, `codex`), or pass it.
- Without history the script still logs the estimate: measuring starts with it.

Quote the script's line verbatim, for example:

`3 SP · observed active ≈25–70 min (n=9, medium, p20-p80) · wall 1.2–3.5 h (n=9, medium, p20-p80) · $4–11 (n=3, low, min-max) · low: n≥3 (all estimators), window 14 d`

Preserve each metric's sample count and confidence. Active time is a log-gap heuristic; dollars
are API-equivalent observed usage, with incomplete costs excluded. Neither is a subscription bill
or a measured total of all project effort.

For a list of tasks, use the `estimate-backlog` skill (effort, value and dependencies per item,
then `estimate forecast --backlog <file>`).

## 3. Forbidden

- Hours, days or dollars that did not come from the script.
- Human-time phrasing ("2-3 weeks", "a sprint").
- Changing a logged estimate after work starts. Log a new one if scope changed.
- Estimating 13 or more without splitting.

## 4. Housekeeping

- `estimate calibrate --check` tells whether there is enough settled history (n ≥ 5) for
  intervals. Low confidence is normal at first: say so.
- `estimate record` after merges (or at session start) settles estimates from merged PRs.
- `estimate record --refresh` recalculates tracked actuals after importing logs or changing prices;
  original estimates and their timestamps remain unchanged.
- `estimate dashboard` writes `.estimate/dashboard.html` (local, no network).
