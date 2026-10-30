"""Browser check of the generated dashboard (Playwright; not part of the stdlib unittest suite).

    uv run --with playwright python tests/ui/check_dashboard.py      # once: uvx playwright install chromium

Builds synthetic dashboards with fixtures.py in a temp dir, then checks: no console errors; Today × 3 agents is the
default; every time box × agent pair shows exactly its own answer and lanes, sure ≤ likely ≤ maybe, more agents and
longer boxes never get less done; the keyboard moves the planner; each backlog row's waterline stripe follows the
state; the list is in Score order with dependencies first, top 12 then "show all"; thin-data states render; the
light scheme applies; 390 px phones have no horizontal overflow.
"""
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

import estimate as E  # noqa: E402  (fixtures puts the repo root on sys.path)

BOXES = ('t0', 't1', 't2', 't3', 't7')
failures = []


def check(cond, msg):
    print(('ok    ' if cond else 'FAIL  ') + msg)
    if not cond: failures.append(msg)


def open_page(browser, path, errors, width=1280, height=800, scheme='dark'):
    page = browser.new_page(viewport={'width': width, 'height': height}, color_scheme=scheme)
    page.on('console', lambda m: errors.append(f'{path.name}: {m.text}') if m.type == 'error' else None)
    page.on('pageerror', lambda exc: errors.append(f'{path.name}: {exc}'))
    page.goto(path.as_uri()); page.wait_for_load_state('load')
    return page


def visible(page, selector):
    return page.locator(selector).evaluate_all('els => els.filter(e => e.offsetParent !== null).map(e => e.className)')


def stripes(page):
    """How many sized rows show a waterline stripe (their ::before is not transparent)."""
    return page.locator('.list li:not(.none)').evaluate_all(
        "els => els.filter(e => getComputedStyle(e, '::before').backgroundColor !== 'rgba(0, 0, 0, 0)').length")


def planner(page):
    check(page.is_checked('#tb-t0') and page.is_checked('#ag-3'), 'Today × 3 agents is the default')
    if page.locator('label[for=all]').count(): page.click('label[for=all]')
    likely = {}
    for b in BOXES:
        for a in E.PARALLEL:
            page.click(f'label[for=tb-{b}]'); page.click(f'label[for=ag-{a}]')
            key = f'{b}a{a}'
            shown = visible(page, 'h1 .v')
            check(shown == [f'v {key}'], f'{key}: only its answer is visible ({shown})')
            lanes = visible(page, '.tl > .v')
            check(lanes == [f'v {key}', f'v {key}'], f'{key}: only its axis and lanes are visible')
            n = page.locator(f'.v.{key} .lane').count()
            check(n in (0, a), f'{key}: {a} lanes drawn ({n})')
            d = page.locator(f'h1 .v.{key} [data-likely]')
            sure, lk, maybe = (int(d.get_attribute(f'data-{t}')) for t in ('sure', 'likely', 'maybe'))
            check(sure <= lk <= maybe, f'{key}: sure {sure} ≤ likely {lk} ≤ maybe {maybe} SP')
            maybe_cards = int(page.locator(f'.fine .v.{key}').inner_text().rsplit('maybe: ', 1)[1].rstrip('.'))
            check(stripes(page) == maybe_cards, f'{key}: {maybe_cards} backlog rows carry a waterline stripe')
            likely[(b, a)] = lk
    check(all(likely[(b, a)] <= likely[(b, c)] for b in BOXES for a, c in zip(E.PARALLEL, E.PARALLEL[1:])),
          'more agents never get less done')
    check(all(likely[(b, a)] <= likely[(c, a)] for a in E.PARALLEL for b, c in zip(BOXES[1:], BOXES[2:])),
          'longer day boxes never get less done')
    page.click('label[for=tb-t0]'); page.click('label[for=ag-1]')
    page.focus('#ag-1'); page.keyboard.press('ArrowRight')
    check(page.is_checked('#ag-2') and visible(page, 'h1 .v') == ['v t0a2'], 'arrow keys move the agent count')
    outline = page.locator('label[for=ag-2]').evaluate("e => getComputedStyle(e).outlineStyle")
    check(outline == 'solid', 'the focused radio lights its label')


def order(page, backlog_text):
    items = E.parse_backlog(backlog_text); by_title = {i['title']: i for i in items}
    if page.locator('label[for=all]').count() and not page.locator('.rest').is_visible(): page.click('label[for=all]')
    titles = page.locator('.list li:not(.none) .t').evaluate_all('els => els.map(e => e.title)')
    check(sorted(titles) == sorted(by_title), f'every sized card listed once after "show all" ({len(titles)})')
    pos = {by_title[t]['id']: n for n, t in enumerate(titles)}
    check(all(pos[d] < pos[i['id']] for i in items for d in i['depends'] if d in pos), 'every card comes after its dependencies')
    free = [by_title[t]['score'] for t in titles if not by_title[t]['depends']]
    check(free == sorted(free, reverse=True), f'cards without dependencies are in Score order: {free}')
    check(page.locator('.list .wait').count() > 0, 'pushed-down cards say what they wait for')
    check(page.locator('.list li.none').count() == len(fixtures.UNSIZED), 'unsized cards are listed at the end')


def main():
    errors = []
    with tempfile.TemporaryDirectory() as d, sync_playwright() as p:
        out = Path(d) / '.estimate'; fixtures.main(out)
        browser = p.chromium.launch(headless=True)
        page = open_page(browser, out / 'dashboard.html', errors)
        check(page.locator('.src').inner_text().startswith('Tiden · Example · 12 cards · updated'), 'source line names Tiden')
        check(page.locator('.list li').count() == 12 and not page.locator('label[for=all]').count(),
              f'{E.TOP_ROWS} cards fit without "show all"')
        bigs = page.locator('.strip .w .big').all_inner_texts()
        check(bigs == ['16% used', '24% used', '22% used'], f'plan windows show their % ({bigs})')
        check('resets' in page.locator('.strip .w').first.inner_text() and 'rough' in page.locator('.strip').inner_text(),
              'window cards name the reset time and a rough window price')
        check(page.locator('.v.t0a3 .mark.lo').is_visible() and '5-hour window resets' in page.locator('.v.t0a3 .mark.lo').inner_text(),
              'Today lanes mark the 5-hour reset')
        check('of a 5-hour window and' in page.locator('.fine .v.t0a3').inner_text(), 'the answer says how much of each window it takes')
        page.click('label[for=tb-t1]')
        check(page.locator('.v.t1a3 .mark.lo').count() == 0, 'day boxes carry no 5-hour reset mark')
        page.click('label[for=tb-t0]')
        kv = page.locator('.grid .kv').first.inner_text()
        check('Subscriptions' in kv and '$400/mo' in kv and 'Claude Max + ChatGPT Pro' in kv, 'subscriptions row once plans are set')
        check('Today' in kv and '7 days' in kv and 'partly unpriced' in kv, 'burn rows show today, 7 days and unpriced tokens')
        check(all(x in kv for x in ('Pace', 'Agent work per SP', '$ per SP', 'Tokens per SP', '24 PRs')), 'rates carry sample sizes')
        planner(page); order(page, fixtures.BACKLOG)
        page = open_page(browser, out / 'no-windows.html', errors)
        check(page.locator('text=no snapshot yet').count() == 3 and page.locator('.strip .w .big').count() == 0,
              'without snapshots the window cards say "no snapshot yet", never a guess')
        check('Subscriptions' not in page.locator('.grid .kv').first.inner_text() and page.locator('.mark.lo').count() == 0,
              'without plans or snapshots: no subscriptions row, no reset mark')
        page = open_page(browser, out / 'dashboard.html', errors, scheme='light')
        bg = page.evaluate('getComputedStyle(document.body).backgroundColor')
        check(bg == 'rgb(244, 244, 251)', f'light scheme applies ({bg})')
        page = open_page(browser, out / 'dashboard.html', errors, 390, 844)
        check(page.evaluate('document.documentElement.scrollWidth') <= 390, 'dashboard at 390 px: no horizontal overflow')
        page = open_page(browser, out / 'empty-backlog.html', errors)
        check(page.locator('text=No backlog yet').is_visible(), 'empty backlog: explains where to put one')
        check(page.locator('text=no local agent logs found').is_visible(), 'no logs: the burn row says so')
        page = open_page(browser, out / 'unsized-only.html', errors)
        check(page.locator('h1').inner_text() == '2cards need sizing', 'only unsized cards: asks for sizing, not for a backlog')
        check(page.locator('.list li.none').count() == 2, 'only unsized cards: they are listed')
        page = open_page(browser, out / 'no-cost.html', errors)
        check('not enough data' in page.locator('.grid .kv').first.inner_text(), 'no cost history: says so quietly')
        page = open_page(browser, out / 'no-metrics.html', errors, 390, 844)
        check(page.evaluate('document.documentElement.scrollWidth') <= 390, 'no hours or cost at 390 px: no overflow')
        page = open_page(browser, out / 'no-history.html', errors)
        check(page.locator('h1').inner_text() == '31SP in 10 cards', 'no history: backlog SP still shown')
        check(page.locator('text=No history yet').is_visible(), 'no history: says why there is no planner')
        check(page.locator('.list li').count() == 10, 'no history: backlog list still renders')
        page = open_page(browser, out / 'few-prs.html', errors)
        check('min–max' in page.locator('.grid .kv').first.inner_text(), 'under 5 PRs: rates are min–max ranges')
        page = open_page(browser, out / 'worst-case.html', errors)
        check(page.evaluate('document.documentElement.scrollWidth') <= 1280, 'worst case: no horizontal page overflow')
        check(page.locator('script').count() == 0, 'worst case: titles are escaped (no <script>)')
        check(page.locator('.list ol').first.locator('li').count() == E.TOP_ROWS and not page.locator('.rest').is_visible(),
              f'worst case: top {E.TOP_ROWS} rows, the rest hidden')
        page.click('label[for=all]')
        check(page.locator('.rest').is_visible() and page.locator('.more .on').is_visible(), '"show all" opens the rest')
        page = open_page(browser, out / 'worst-case.html', errors, 390, 844)
        check(page.evaluate('document.documentElement.scrollWidth') <= 390, 'worst case at 390 px: no horizontal overflow')
        browser.close()
    check(not errors, f'no console errors {errors or ""}')
    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'all checks passed'}")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
