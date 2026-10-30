"""Write synthetic dashboards for the UI check: python3 tests/ui/fixtures.py OUT_DIR. Stdlib only."""
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import estimate as E  # noqa: E402

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=12, hours=15)   # 15:00 UTC: 9 hours left today
HEAD = '| id | title | effort | value | depends |\n|----|-------|--------|-------|---------|\n'
BACKLOG = HEAD + """| 1 | Audit log table and write path for permission changes | 3 | 5 | |
| 2 | Audit log viewer in the admin panel | 3 | 3 | 1 |
| 3 | Export search results as CSV | 3 | 3 | |
| 4 | Add retry with backoff to the webhook sender | 5 | 5 | |
| 5 | Fix timezone in the daily digest header | 2 | 3 | |
| 6 | Paginate the admin list endpoints | 5 | 3 | |
| 7 | Admin list UI: page controls | 2 | 3 | 6 |
| 8 | Rate-limit login attempts per IP | 3 | 8 | |
| 9 | Admin list UI: saved filters | 3 | 2 | 7 |
| 10 | Flaky test: stabilize the clock in scheduler tests | 2 | 2 | |
"""


def history(n=24, seed=3):
    rnd = random.Random(seed); rows = []
    for k in range(n):
        sp = rnd.choice((1, 2, 3, 3, 5, 8)); merged = T0 + timedelta(hours=10 + k * 11)
        wall = sp * rnd.uniform(.6, 2.5); active = wall * rnd.uniform(.3, .7)
        model = ('claude-opus-5-5', 'gpt-5', 'claude-sonnet-5-5')[k % 3]
        rows.append({'id': f'e{k}', 'sp': sp, 'source': 'agent', 'closest_pr': None,
                     'created_at': E.iso(merged - timedelta(hours=wall + .1)),
                     'estimator': {'agent': 'codex' if model == 'gpt-5' else 'claude-code', 'model': model},
                     'actual': {'pr': 100 + k, 'merged_at': E.iso(merged), 'start_at': E.iso(merged - timedelta(hours=wall)),
                                'wall_h': round(wall, 2), 'active_h': round(active, 2), 'usd': round(active * rnd.uniform(2, 6), 2),
                                'cost_complete': True, 'review_rounds': 1, 'executor': model, 'title': f'Synthetic PR {k}', 'areas': [],
                                'usage_by_model': {model: [{'input_tokens': 10, 'output_tokens': 2000,
                                                            'cache_read_input_tokens': int(sp * rnd.uniform(1e6, 4e6))}]}}})
    return rows


UNSIZED = [{'id': '11', 'key': '', 'title': 'Single sign-on for the admin panel'},
           {'id': '12', 'key': '', 'title': 'Weekly usage report e-mail'}]


def burn(seed=5):
    """Synthetic responses over the last week from three parallel agents; one Codex model has no price."""
    rnd = random.Random(seed); recs = []
    for k in range(3000):
        ts = NOW - timedelta(minutes=rnd.uniform(0, 7 * 24 * 60))
        agent, model, usd = rnd.choice((('claude-code', 'claude-opus-5-5', .09), ('claude-code', 'claude-sonnet-5-5', .02),
                                        ('codex', 'gpt-6.1-sol', .01), ('codex', 'gpt-6-sol', None)))
        recs.append((ts, agent, model, rnd.randint(20_000, 160_000), usd and usd * rnd.uniform(.5, 1.5), f'stream-{k % 3}'))
    return E.burn_report(sorted(recs), NOW, timezone.utc)


def usage(seed=7, labels=True):
    """Synthetic sessions over 90 days in four repos: Claude Code and Codex, parallel workers, one unpriced model."""
    rnd = random.Random(seed); recs = []; meta = {}; repos = {}
    names = ('example/shop', 'example/shop', 'example/blog', 'example/infra', '~/notes')
    tasks = ('Add retry with backoff to the webhook sender', 'Review the diff on this branch', 'Paginate the admin list endpoints',
             'Fix timezone in the daily digest header', 'Audit log viewer in the admin panel', '/review')
    models = (('claude-code', 'claude-opus-5-5'), ('claude-code', 'claude-sonnet-5-5'), ('codex', 'gpt-6.1-sol'), ('codex', 'gpt-6-sol'))
    for k in range(260):
        day = rnd.choice(range(90)) if k % 3 else rnd.choice(range(30))
        start = NOW - timedelta(days=day, hours=rnd.uniform(0, 10)); sid = f's{k}'
        agent, model = models[0 if k % 5 < 2 else 1 if k % 5 == 2 else 2 if k % 7 else 3]
        meta[sid] = {'label': tasks[k % len(tasks)] if labels else None}; repos[sid] = names[k % len(names)]
        for n in range(rnd.randint(10, 80)):
            ts = start + timedelta(minutes=n * rnd.uniform(1, 4))
            if ts > NOW: break
            u = E.normalize_usage({'input_tokens': rnd.randint(5, 400), 'output_tokens': rnd.randint(200, 3000),
                                   'cache_read_input_tokens': rnd.randint(40_000, 160_000), 'cache_creation_input_tokens': rnd.randint(0, 6000)})
            fam = E.model_family(model, E.DEFAULT_PRICES)
            usd = E.message_cost(u, E.DEFAULT_PRICES[fam]) if fam else None
            recs.append((ts, agent, model, u['total_tokens'], usd, (sid,), (sid, n), u, sid))
    merged = {'example/shop': [NOW - timedelta(hours=7 * k) for k in range(120)], 'example/blog': [NOW - timedelta(days=2 * k) for k in range(20)]}
    return {d: E.usage_report(sorted(recs), meta, repos, NOW, d, E.DEFAULT_PRICES, timezone.utc, labels=labels, merged=merged)
            for d in E.USAGE_PERIODS}


def windows():
    """A fresh Claude snapshot with reset times (the 5-hour window resets in 2 h 20 min), a Codex week and window prices."""
    ts = NOW - timedelta(minutes=3)
    claude = {'h5': {'pct': 16.0, 'resets_at': NOW + timedelta(hours=2, minutes=20), 'ts': ts},
              'w7': {'pct': 24.0, 'resets_at': NOW + timedelta(days=5, hours=20, minutes=18), 'ts': ts}}
    codex = E.codex_limits(NOW - timedelta(minutes=40), {'plan_type': 'pro', 'primary': {
        'used_percent': 22.0, 'window_minutes': 10080, 'resets_at': int((NOW + timedelta(days=4, hours=20)).timestamp())}})
    return {'claude': claude, 'codex': codex, 'accounts': 1, 'price': {'h5': 1.8, 'w7': 16.0, 'hours': {'h5': 24, 'w7': 31}}}


SUBSCRIPTIONS = [{'name': 'Claude Max', 'usd': 200.0}, {'name': 'ChatGPT Pro', 'usd': 200.0}]


def page(rows, backlog_text, unsized=(), with_burn=True, plan_windows=False, usage=None):
    cal = E.calibrate(rows, NOW, 14)
    backlog = E.parse_backlog(backlog_text) if backlog_text is not None else None
    source = {'label': 'Tiden · Example', 'updated': NOW - timedelta(hours=2)}
    return E.dashboard_html('example/repo', rows, cal, backlog, NOW, source, unsized, burn() if with_burn else None, timezone.utc,
                            windows() if plan_windows else None, SUBSCRIPTIONS if plan_windows else (), usage=usage)


def worst_backlog(n=200):
    long = 'Migrate every legacy permission check in the admin, billing and reporting modules to the new policy engine without downtime ' * 2
    rows = [f'| {k} | {long if k % 7 == 0 else "Item " + str(k)}{"<script>x</script>" if k == 3 else ""} | {(1, 2, 3, 5, 8, 13)[k % 6]} | {(1, 2, 3, 5, 8)[k % 5]} | {k - 1 if k % 4 == 0 else ""} |'
            for k in range(1, n + 1)]
    return HEAD + '\n'.join(rows) + '\n'


def main(out):
    out = Path(out); out.mkdir(parents=True, exist_ok=True); rows = history()
    no_cost = [{**r, 'actual': {**r['actual'], 'usd': None, 'cost_complete': False}} for r in rows]
    cases = {'dashboard.html': page(rows, BACKLOG, UNSIZED, plan_windows=True, usage=usage()),
             'usage-no-labels.html': page(rows, None, with_burn=False, usage=usage(labels=False)),
             'no-windows.html': page(rows, BACKLOG, UNSIZED),
             'unsized-only.html': page(rows, None, UNSIZED),
             'no-cost.html': page(no_cost, BACKLOG),
             'no-metrics.html': page([{**r, 'actual': {**r['actual'], 'active_h': None}} for r in no_cost], BACKLOG),
             'empty-backlog.html': page(rows, None, with_burn=False),
             'no-history.html': page([], BACKLOG),
             'worst-case.html': page(rows, worst_backlog()),
             'few-prs.html': page(rows[:3], BACKLOG)}
    for name, html in cases.items(): (out / name).write_text(html, encoding='utf-8')
    print('\n'.join(str(out / n) for n in cases))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.estimate')
