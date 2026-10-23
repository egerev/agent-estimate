# Example backlog (synthetic)

Effort: Fibonacci story points 1, 2, 3, 5, 8 (13+ must be split). Value: 1, 2, 3, 5, 8.
Depends: comma-separated ids that must be done first; leave empty when unsure.
`estimate forecast --backlog examples/backlog.example.md` computes Score = Value / Effort and the order.

| id | title | effort | value | depends |
|----|-------|--------|-------|---------|
| 1 | Audit log table and write path for permission changes | 3 | 5 | |
| 2 | Audit log viewer in the admin panel | 3 | 3 | 1 |
| 3 | Export search results as CSV | 3 | 3 | |
| 4 | Add retry with backoff to the webhook sender | 5 | 5 | |
| 5 | Fix timezone in the daily digest header | 2 | 3 | |
| 6 | Paginate the admin list endpoints | 5 | 3 | |
| 7 | Admin list UI: page controls | 2 | 3 | 6 |
| 8 | Rate-limit login attempts per IP | 3 | 8 | |
| 9 | Rename the confusing config flag, keep the old name as an alias | 1 | 1 | |
| 10 | Flaky test: stabilize the clock in scheduler tests | 2 | 2 | |
| 11 | Offline mode for the mobile client | 13 | 5 | |
