---
name: estimate-backlog
description: Size and value every item of a backlog file (Effort in Fibonacci story points, Value, Depends, optional Lane), write the numbers back, and get the Score order and measured totals from the `estimate` script. Use when asked to estimate, score, prioritize or order a backlog, "оцени бэклог", or before planning several tasks. The model gives points and value only; hours and dollars come from the script.
---

# Estimate a backlog

You fill three columns per item, and a lane where one applies, with your knowledge of the product and the code. The `estimate`
script computes Score = Value / Effort, the order, and hour and dollar totals from this repo's
merged PRs. You never state hours, days or dollars yourself, and you do not order the list by hand.

## 1. Find the backlog

First, if unsure which data dir the script uses, run `estimate doctor`: it prints the data dir, repo
and Claude transcript dirs it resolved and from where (`--dir`, `$ESTIMATE_DIR`, the nearest `.estimate/`,
or the registry entry `estimate link --dir <dir>` wrote for this repo, valid in every worktree), and the
calibration n. An empty data dir stops `forecast` with one message; fix the dir instead of quoting n=0.

Read `config.json` in that data dir (usually `.estimate/` at the git root), key `backlog`. The data directory may live elsewhere:
if the user named one, or `$ESTIMATE_DIR` is set, read `<dir>/config.json` and pass the same
`--dir <dir>` (and `--repo owner/name` when not run inside that repo) to every `estimate` command,
before the subcommand: `estimate --dir <dir> --repo owner/name forecast --backlog`.

- `"source": "tiden"`: run `estimate backlog pull` first. It copies open Tiden cards into
  `<data dir>/backlog.md` (id = Tiden id, `key` = a human key such as KEY-21 when the card has one)
  and keeps effort, value and depends you set earlier. Containers (cards with child cards) are left
  out: size their children, never the container. A row with the note `maybe-done` is linked to merged
  code in Tiden while still open: ask the user whether it is delivered (then they close it in Tiden and
  pull again) before you size it. Open means status Backlog or Review; to pull
  fewer, list them in config: `{"backlog": {"source": "tiden", "statuses": ["Backlog"]}}`. Work in that file;
  nothing is written back to Tiden. `--repo` does not choose the Tiden product: without
  `product_id`, Tiden lists the product of the checkout you run in. So with a custom data dir, set
  `"product_id"` in that dir's config (the pull refuses otherwise); without one, run the pull from
  the target repo's checkout with its own `.estimate`.
- `"source": "markdown"` or no config: the file at `path` (default `BACKLOG.md` at the git root).
  If it does not exist, ask where the backlog is. When you work in another file, save it as
  `{"backlog": {"source": "markdown", "path": "<file>"}}` in `.estimate/config.json` (keep the
  other keys), or pass it as `--backlog <file>` in step 5.

The file is a Markdown table:

```markdown
| id | title | effort | value | depends |
|----|-------|--------|-------|---------|
| 1 | Audit log table and write path | 3 | 5 | |
| 2 | Audit log viewer in the admin panel | 3 | 3 | 1 |
```

Other columns and prose around the table are allowed and ignored. A plain list (`<SP> <title>`
per line) also works but carries no value or dependencies: convert it to the table first.
Keep existing ids; give new rows the next free number. Never shorten a stored id: in `depends`
you may write the full id, the `key`, or the first 8 characters of a UUID id (the script shows
that short form when it is unique in the file).

## 2. Effort (story points)

One of **1, 2, 3, 5, 8**. Sizes are relative: a 2 is about twice a 1.

**Size the remaining work, not the whole feature.** Before sizing a card, check the code and the
merged PRs. If part of it is already built, size only what is left (wiring, the missing test, the
rollout check). If it is fully built and only acceptance remains, a 1 is usually right; tell the user
which cards look delivered so they can close them instead.

**Epics and containers.** A card that has child cards is not work by itself: do not size it. Size
the children. If the container row is in the file anyway, delete it from the table or ask the user;
never give it the sum of its children, which would count the work twice.

- **1** a one-line fix, a config or copy change, a version bump
- **2** a small bug fix in one module, with a test
- **3** a feature slice in one module, with tests
- **5** a change across a few modules, or a new command or endpoint with tests and docs
- **8** a new subsystem slice, a migration plus code plus tests, or a cross-cutting refactor

**One scale with retro-sizing.** These are the same points `estimate init` asks for when it
retro-sizes merged PRs. That packet allows 13 because a merged PR cannot be split; here 13 means the
item must be split first. Both mean the same amount of work, so the history stays comparable.

**Split rule:** 13 or more is too big to estimate. Replace the row with parts of at most 8 SP
each, with their own ids, and link them through `depends` where one needs another. Then retarget
every item that depended on the old id to the part(s) that must finish first, usually the last one.

## 3. Value

One of **1, 2, 3, 5, 8**: what the item is worth to users or the business, relative to the rest
of this backlog.

- **1** nice to have, nobody is waiting for it
- **2** small polish or internal convenience
- **3** a clear improvement for some users or for the team's speed
- **5** an important feature, or a defect users hit regularly
- **8** blocks a launch, revenue, data safety or most users

Judge value apart from effort; the script divides them.

## 4. Depends (required)

Fill depends for **every** item you size: comma-separated ids that must be merged before it can
start, or `-` when it needs nothing. An empty cell means "not checked yet", and `estimate plan`
then plans the item as if it could start now. Only real ordering constraints, not "would be nice
first". Check each item against these:

- both items touch the same files or paths, and one builds on the other's change
- data model or migration before the UI or report that reads it
- API or server change before the client that calls it
- a Tiden card's "Depends on" line (`backlog pull` fills an empty cell from it when it names open
  cards; check it), or an explicit "after X" in the item text

A dependency on delivered work (a merged PR, a Done card) is not a dependency: write `-`. If you
cannot tell, write `-` and name the item to the user. The script honors only what is written; it
infers nothing.

**Lane (optional).** When several items edit the same hot file or one migration sequence, give
them the same `lane` (a column such as `bot.py` or `migration`) and add its cap below the table:

```markdown
## Lanes
- migration (merging cap 1): migrations number in sequence
- bot.py (cap 1): one open PR at a time
```

`cap N` limits the lane's items started and not yet merged; `merging cap N` limits only the
items waiting to merge. Items without a lane run in parallel. Do not invent lanes for items that
merely live in the same area.

## 5. Write back and run

Write effort, value and depends into the file, then:

```sh
estimate forecast --backlog                 # the configured backlog; or --backlog <file>; --json for scripts
estimate plan                               # dates for 1, 2, 3, 5 agents, critical chain, waves; --agents K, --top N
```

It prints the totals (SP, agent active hours, API-equivalent $, calendar days, each with its
sample size and confidence), then the order: Score descending, and an item never before what it
depends on. An item pushed down by a dependency shows `waits for #n`. Unknown ids and cycles
give warnings; fix the file and rerun. An item placed before higher-Score work that depends on it
shows `unblocks #n`. Quote the script's output; do not reorder or re-rank it.

Agent hours are agent active time **summed**: one agent working serially, not wall clock. For elapsed
time quote the `wall clock, N agents` lines (1, 2, 3, 5 agents; never shorter than the longest
dependency chain; for 2+ agents a lower bound). The one `calibration n=…` line counts PRs left out
of the sample; `estimate doctor` lists them.

A row without effort stops the forecast. Size it. `--skip-unsized` gives partial totals that leave
such rows out with a warning; use it only for a quick look, and say which items the totals omit.

## 6. Forbidden

- Hours, days or dollars that did not come from the script.
- Human-time phrasing ("2-3 weeks", "a sprint").
- Effort of 13 or more left unsplit.
- Sizing an epic or container, or sizing work that is already merged.
- Inventing dependencies to force an order you prefer. If the order looks wrong, recheck value.
- Leaving depends empty on an item you sized: write the ids or `-`.
