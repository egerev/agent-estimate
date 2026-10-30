#!/usr/bin/env python3
"""estimate: Fibonacci story points for coding agents, measured per estimator (agent + model).

An agent sizes a task in story points. This script records who estimated (agent harness + model),
measures what the merged PR actually cost (time, $, who executed it) from git, gh and local agent
logs, and turns points into hour and dollar ranges per estimator. The model never states hours.
Stdlib only; `gh` runs as a subprocess. Data stay in <repo>/.estimate/ (or --dir / $ESTIMATE_DIR).
No network calls except `gh` to your own GitHub. No telemetry.
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import hashlib
import heapq
import html
import io
import json
import math
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

__version__ = '0.4.0'
FIB = (1, 2, 3, 5, 8, 13)
MAX_SP = 8            # a task above this must be split into parts of at most MAX_SP before work
VALUES = (1, 2, 3, 5, 8)  # backlog item Value scale; Score = Value / Effort
MIN_INTERVAL_N = 5    # fewer samples: a min-max range and low confidence, never a p20-p80 interval
ESTIMATOR_MIN_N = 5   # settled pre-work estimates before an estimator's own rates replace the pooled ones
SIZING_GRACE_MIN = 10  # agent-log activity this soon before the estimate, with no commit or PR yet, is the sizing itself
NO_PR_DAYS = 7        # a branch estimate with no PR after this many days is marked 'no PR' by `record`
METRICS = ('active_h', 'wall_h', 'usd', 'review_rounds')
SCALED = ('active_h', 'wall_h', 'usd')  # scale with SP; review rounds do not
OPEN_FIELDS = 'number,title,body,createdAt,headRefName,isDraft'
# USD per million tokens: standard API-equivalent list prices, verified 2026-10-20 at
# https://platform.claude.com/docs/en/about-claude/pricing and, for the Codex (OpenAI) models, as of 2026-10
# (verified 2026-10-22) at https://developers.openai.com/api/docs/pricing (not subscription expenditure).
# OpenAI's 30-minute and 12-hour cache writes cost 1.25x and 2.5x input (`_openai_price`).
# Built-in rows match exact model versions and optional date suffixes, never unknown future versions.
# An explicit prices.json may still intentionally use family keys such as "opus".
PRICING_BASIS = 'standard API-equivalent list price (Claude 2026-10-20, Codex models as of 2026-10); excludes subscription billing'


def _price(input_usd, output_usd, cache_read):
    return {'input': input_usd, 'output': output_usd, 'cache_read': cache_read,
            'cache_write_5m': input_usd * 1.25, 'cache_write_1h': input_usd * 2}


def _openai_price(input_usd, output_usd, cache_read):
    """OpenAI rows: the 30-minute write fills the short-write slot, the 12-hour write the long one."""
    return {**_price(input_usd, output_usd, cache_read), 'cache_write_1h': input_usd * 2.5}


# Codex rows: OpenAI standard tier, $ per 1M tokens, https://developers.openai.com/api/docs/pricing (fetched 2026-10-26);
# long context above 272K input tokens.
DEFAULT_PRICES = {
    'claude-opus-5-5': _price(4, 20, .2),
    'claude-sonnet-5-5': _price(2, 10, .1),
    'claude-haiku-5-5': {**_price(.1, .5, .01), 'long_context_threshold': 100_000,
                         'long_context': _price(.5, 2.5, .05)},
    'claude-opus-5': _price(5, 25, .5),
    **{f'claude-opus-4-{v}': _price(5, 25, .5) for v in (5, 6, 7, 8)},
    'claude-fable-5': _price(10, 50, 1),
    'claude-fable-5-1': _price(10, 50, .25),
    'claude-mythos-5': _price(10, 50, 1),
    'claude-mythos-5-1': _price(10, 50, .25),
    'claude-sonnet-5': _price(2, 10, .2),
    'claude-sonnet-4': _price(3, 15, .3),
    **{f'claude-sonnet-4-{v}': _price(3, 15, .3) for v in (5, 6)},
    'claude-haiku-4-5': _price(1, 5, .1),
    'claude-haiku-3-5': _price(.8, 4, .08),
    'claude-opus-4': _price(15, 75, 1.5),
    'claude-opus-4-1': _price(15, 75, 1.5),
    'gpt-6-astra': {**_openai_price(10, 50, 1), 'long_context_threshold': 272_000, 'long_context': _openai_price(20, 75, 2)},
    'gpt-6.1-sol': {**_openai_price(2, 10, .1), 'long_context_threshold': 272_000, 'long_context': _openai_price(4, 15, .2)},
    'gpt-6-sol': {**_openai_price(2, 10, .2), 'long_context_threshold': 272_000, 'long_context': _openai_price(4, 15, .4)},
}
SP_LINE = re.compile(r'(?im)^\s*(?:story\s*points?|sp|estimate)\s*[:=]\s*(\d+)\s*(?:sp)?\s*$')
ALL = 'all estimators'


# ---------- time and math ----------

def opt_utc(s):
    if not s: return None
    try: d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError: return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def utc(s) -> datetime:
    if isinstance(s, datetime): return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    d = opt_utc(s)
    if d is None: raise ValueError(f'not a timestamp: {s!r}')
    return d


def iso(d): return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def hours(a, b): return (b - a).total_seconds() / 3600


def percentile(values, q):
    """Linear interpolation between closest ranks (the common 'type 7' definition)."""
    xs = sorted(values)
    if not xs: return None
    k = (len(xs) - 1) * q
    f = math.floor(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def spread(values):
    """p20/p50/p80, or min/median/max when there are fewer than MIN_INTERVAL_N values."""
    xs = [v for v in values if v is not None]
    if not xs: return None
    if len(xs) < MIN_INTERVAL_N:
        return {'lo': min(xs), 'mid': statistics.median(xs), 'hi': max(xs), 'n': len(xs), 'kind': 'min-max'}
    return {'lo': percentile(xs, .2), 'mid': percentile(xs, .5), 'hi': percentile(xs, .8), 'n': len(xs),
            'kind': 'p20-p80'}


def ratio_p80_p20(values):
    """Consistency: how many times wider p80 is than p20 (1 = perfectly consistent). None below n=5."""
    xs = [v for v in values if v is not None]
    if len(xs) < MIN_INTERVAL_N: return None
    lo = percentile(xs, .2); hi = percentile(xs, .8)
    return round(hi / lo, 2) if lo and lo > 0 else None


def active_hours(stamps, idle_min=5.0):
    """Log-gap activity heuristic for ONE stream, excluding gaps longer than idle_min."""
    return interval_hours(activity_intervals(stamps, idle_min))


def activity_intervals(stamps, idle_min=5.0):
    xs = sorted(set(stamps)); gap = idle_min * 60
    return [(a, b) for a, b in zip(xs, xs[1:]) if 0 < (b - a).total_seconds() <= gap]


def interval_hours(intervals):
    """Union intervals already formed independently in each stream, without bridging gaps."""
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]: merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else: merged.append((lo, hi))
    return sum(hours(lo, hi) for lo, hi in merged)


def confidence(n, ratio, age_days, drifted):
    """From sample size, interval width (p80/p20) and calibration age; never from the model's opinion."""
    if n < MIN_INTERVAL_N or ratio is None or ratio > 10: return 'low'
    level = 2 if n >= 15 and ratio is not None and ratio <= 4 else 1
    if age_days > 7 or drifted: level -= 1
    return ('low', 'medium', 'high')[max(level, 0)]


# ---------- prices and agent logs ----------

def model_family(model, prices):
    m = (model or '').lower()
    if m.startswith('claude-3-5-haiku'): m = m.replace('claude-3-5-haiku', 'claude-haiku-3-5', 1)
    for k in sorted(prices, key=len, reverse=True):
        if k in DEFAULT_PRICES:
            if re.fullmatch(re.escape(k) + r'(?:-\d{8})?', m): return k
        elif k.lower() in m: return k  # explicit user family overrides remain supported
    return None


def message_cost(u, p):
    """Usage block -> API-equivalent USD, preserving per-request long-context tiers."""
    u = normalize_usage(u)
    if u.get('usage_missing'): return None
    prompt = u['input_tokens'] + u['cache_creation_input_tokens'] + u['cache_read_input_tokens']
    if p.get('long_context') and prompt > p.get('long_context_threshold', float('inf')): p = p['long_context']
    c1 = u['cache_creation']['ephemeral_1h_input_tokens']
    c5 = u['cache_creation']['ephemeral_5m_input_tokens']
    counts = {'input': u['input_tokens'], 'cache_write_5m': c5, 'cache_write_1h': c1,
              'cache_read': u['cache_read_input_tokens'], 'output': u['output_tokens']}
    # A rate is required only for a category that was used: older tables omit cache-write rates.
    return sum(n * p[k] for k, n in counts.items() if n) / 1e6


def normalize_usage(u):
    """Canonical Claude-shaped usage; Codex input includes cache reads and Claude input excludes them."""
    token_fields = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens',
                    'cached_input_tokens', 'cache_creation')
    if u.get('usage_missing') or (u.get('total_tokens') and not any(k in u for k in token_fields)):
        return {'usage_missing': True, **{k: u[k] for k in ('total_tokens', 'reported_usage', 'reason') if u.get(k)}}
    input_tokens = u.get('input_tokens') or 0
    creation = u.get('cache_creation') or {}
    c1 = creation.get('ephemeral_1h_input_tokens') or 0
    c5 = creation.get('ephemeral_5m_input_tokens') or 0
    cw = max(u.get('cache_creation_input_tokens') or 0, c1 + c5)
    c5 = max(cw - c1, 0)
    cached = u.get('cache_read_input_tokens') or 0
    if 'cached_input_tokens' in u and not any(k in u for k in
            ('cache_read_input_tokens', 'cache_creation_input_tokens', 'cache_creation')):
        cached = u.get('cached_input_tokens') or 0
        input_tokens = max(input_tokens - cached, 0)
    output_tokens = u.get('output_tokens') or 0
    return {'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'cache_creation_input_tokens': cw, 'cache_read_input_tokens': cached,
            'cache_creation': {'ephemeral_5m_input_tokens': c5, 'ephemeral_1h_input_tokens': c1},
            'total_tokens': u.get('total_tokens', input_tokens + cw + cached + output_tokens)}


def usage_tokens(u):
    return normalize_usage(u).get('total_tokens') or 0


def log_dir_matches(name, repo_name):
    return f'-{repo_name.lower()}' in name.lower()


def repo_url_matches(url, repo_full):
    """GitHub remote for exactly `repo_full`: URL or scp-like form, any userinfo and port."""
    u = (url or '').strip()
    if '://' in u:
        parts = urlsplit(u)
        if parts.scheme.lower() not in ('http', 'https', 'ssh', 'git', 'git+ssh'): return False
        host, path = parts.hostname or '', parts.path
    elif m := re.fullmatch(r'(?:[^@/]+@)?([^:/]+):(.+)', u): host, path = m.groups()
    else: return False
    path = path.strip('/').removesuffix('.git').lower()
    return host.lower() == 'github.com' and path == repo_full.lower()


def _mtime_ok(f, since):
    try: return not since or datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) >= since
    except OSError: return False


def _fill_models(recs):
    """Inherit models only for activity records without usage. A billable response whose own model
    is absent remains unknown; borrowing another response's model would invent its price."""
    cur = None
    for i, r in enumerate(recs):
        if r[3]: cur = r[3]
        elif cur and r[4] is None: recs[i] = r[:3] + (cur,) + r[4:]
    first = next((r[3] for r in recs if r[3]), None)
    return [r if r[3] or r[4] is not None or not first else r[:3] + (first,) + r[4:] for r in recs]


def scan_claude_logs(root, repo_name, repo_full, since=None, scan_all=False, project_dirs=()):
    """Claude JSONL -> independent file streams, requiring evidence of repository identity.

    A matching directory basename (and scan_all) is never identity evidence. Accept an explicitly
    trusted transcript project directory, a session's exact repository pr-link, or its cwd's git
    remote. Six-field records stay compatible; sidechain/file streams get distinct session keys.
    Final usage snapshots are selected across files by timestamp, with a stable tie-breaker.
    """
    root = Path(root).expanduser()
    sessions = defaultdict(list); links = defaultdict(set)
    trusted_dirs = {Path(d).expanduser().resolve() for d in project_dirs}
    dirs = {d.resolve() for d in root.iterdir() if d.is_dir()} if root.is_dir() else set()
    dirs |= {d for d in trusted_dirs if d.is_dir()}
    cwd_cache = {}; contradictory_cwds = set(); candidates = []; stream_links = {}; winners = {}

    def verified_cwd(cwd):
        if not cwd or not isinstance(cwd, str): return False
        if cwd not in cwd_cache:
            try:
                result = subprocess.run(['git', '-C', cwd, 'config', '--get-regexp', r'^remote\..*\.url$'],
                                        capture_output=True, text=True, check=False)
                remotes = [line.split(None, 1)[1] for line in result.stdout.splitlines()
                           if len(line.split(None, 1)) == 2] if result.returncode == 0 else []
                cwd_cache[cwd] = any(repo_url_matches(remote, repo_full) for remote in remotes)
                if remotes and not cwd_cache[cwd]: contradictory_cwds.add(cwd)
            except OSError: cwd_cache[cwd] = False
        return cwd_cache[cwd]

    for d in sorted(dirs):
        trusted_sessions = set(); project_links = defaultdict(set); per_stream = defaultdict(list)
        session_cwds = defaultdict(set); foreign_sessions = set()
        for f in sorted(d.rglob('*.jsonl')):
            if not _mtime_ok(f, since): continue
            try: fh = f.open(encoding='utf-8', errors='replace')
            except OSError: continue
            with fh:
                for line_no, line in enumerate(fh):
                    try: o = json.loads(line)
                    except ValueError: continue
                    if not isinstance(o, dict): continue
                    sid = o.get('sessionId') or f'file:{f.relative_to(d)}'
                    if isinstance(o.get('cwd'), str): session_cwds[sid].add(o['cwd'])
                    if verified_cwd(o.get('cwd')): trusted_sessions.add(sid)
                    if isinstance(o.get('cwd'), str) and o['cwd'] in contradictory_cwds and d not in trusted_dirs:
                        continue  # An explicit conflicting remote cannot inherit another record's identity.
                    if o.get('type') == 'pr-link':
                        if str(o.get('prRepository', '')).lower() == repo_full.lower():
                            try: project_links[sid].add(int(o['prNumber'])); trusted_sessions.add(sid)
                            except (KeyError, TypeError, ValueError): pass
                        elif o.get('prRepository'): foreign_sessions.add(sid)
                        continue
                    if o.get('type') not in ('assistant', 'user'): continue
                    ts = opt_utc(o.get('timestamp')); branch = o.get('gitBranch')
                    if not ts or not branch or branch == 'HEAD': continue
                    usage = model = None
                    msg = o.get('message') if isinstance(o.get('message'), dict) else {}
                    if o.get('type') == 'assistant' and msg and msg.get('model') != '<synthetic>':
                        model = msg.get('model')
                        usage = msg['usage'] if isinstance(msg.get('usage'), dict) and msg['usage'] else {'usage_missing': True}
                    main = not o.get('isSidechain') and 'subagents' not in f.relative_to(d).parts
                    stream = (str(f), sid, main, str(o.get('agentId') or ''))
                    rec = (ts, branch, 'claude-code', model, usage, main)
                    dedup = (msg['id'], o.get('requestId')) if msg.get('id') else None
                    rank = (not usage.get('usage_missing'), ts, usage_tokens(usage), json.dumps(usage, sort_keys=True),
                            str(f), line_no) if usage else None
                    per_stream[stream].append((rec, dedup, rank))
        # A worktree removed after merge can no longer show its remote. A session whose every cwd is gone
        # inherits identity from a session of the same transcript dir that ran in one of those cwds and
        # was identified (its remote while the dir existed, or a pr-link of this repo), unless it links another repo.
        anchors = {c for t in trusted_sessions for c in session_cwds[t]}
        for sid, cwds in session_cwds.items():
            if (sid not in trusted_sessions and sid not in foreign_sessions and cwds & anchors
                    and not any(os.path.isdir(c) for c in cwds)):
                trusted_sessions.add(sid)
        for stream, entries in sorted(per_stream.items()):
            file_path, sid, main, _ = stream
            if d not in trusted_dirs and sid not in trusted_sessions: continue
            stream_id = sid if main and sid not in stream_links else f'{sid}::stream:{file_path}:{stream[2]}:{stream[3]}'
            stream_links[stream_id] = project_links.get(sid, set())
            for rec, dedup, rank in entries:
                index = len(candidates); candidates.append((stream_id, rec))
                if rank and dedup and (dedup not in winners or rank > winners[dedup][0]):
                    winners[dedup] = (rank, index)
                # A missing message id cannot safely identify snapshots; retain every usage block.
                elif rank and not dedup: winners[('line', index)] = (rank, index)
    keep = {index for _, index in winners.values()}
    for index, (sid, rec) in enumerate(candidates):
        if rec[4] and index not in keep: rec = rec[:4] + (None,) + rec[5:]
        sessions[sid].append(rec)
    for sid, recs in sessions.items():
        sessions[sid] = _fill_models(sorted(recs, key=lambda r: r[0]))
        links[sid].update(stream_links[sid])
    return sessions, links


def scan_codex_logs(root, repo_full, since=None):
    """Codex rollout JSONL -> records per session, for sessions whose git remote is this repo.

    Usage comes from `token_usage_record` and `event_msg/token_count` `last_token_usage` (never the
    cumulative total). Reports with a matching response ID or timestamp and usage count once.
    Equal usage at different times without IDs is ambiguous and keeps billing incomplete. Unmatched
    fallback reports remain counted when explicit records cover only part of a rollout. Codex records the git
    branch once per session, so a Codex session that switches branches stays on its first branch.
    """
    root = Path(root).expanduser(); sessions = defaultdict(list)
    if not root.is_dir(): return sessions
    for f in sorted(root.rglob('*.jsonl')):
        if not _mtime_ok(f, since): continue
        try: fh = f.open(encoding='utf-8', errors='replace')
        except OSError: continue
        with fh:
            try: meta = json.loads(fh.readline())
            except ValueError: continue
            if not isinstance(meta, dict): continue
            p = meta.get('payload') if isinstance(meta.get('payload'), dict) else {}; git = p.get('git') or {}
            if meta.get('type') != 'session_meta' or not repo_url_matches(git.get('repository_url'), repo_full): continue
            branch = git.get('branch'); sid = 'codex:' + str(p.get('id')); model = None
            if not branch or branch == 'HEAD': continue
            if sid in sessions: sid += f'::stream:{f.relative_to(root)}'
            recs = []; last_total = None; unreported = []; pending = None; starts = set()
            for line in fh:
                try: o = json.loads(line)
                except ValueError: continue
                if not isinstance(o, dict): continue
                ts = opt_utc(o.get('timestamp'))
                if not ts: continue
                pl = o.get('payload') if isinstance(o.get('payload'), dict) else {}
                kind, item = o.get('type'), pl.get('type')
                # A response logged but interrupted before its usage report leaves billing incomplete.
                if pending and (kind == 'turn_context' or (kind == 'response_item' and pl.get('role') == 'user')
                                or (kind == 'event_msg' and item in ('user_message', 'turn_aborted', 'task_started'))):
                    unreported.append(pending); pending = None
                if kind == 'turn_context': model = pl.get('model') or model
                if kind == 'response_item' and (item in ('reasoning', 'function_call', 'custom_tool_call', 'local_shell_call')
                                                 or (item == 'message' and pl.get('role') == 'assistant')):
                    pending = pending or (ts, model)
                if kind == 'turn_context' or (kind == 'response_item' and (item in (
                        'reasoning', 'function_call', 'custom_tool_call', 'local_shell_call')
                        or (item == 'message' and pl.get('role') == 'assistant'))):
                    starts.add(len(recs))  # a new turn or model output: no later report echoes an earlier record
                rec_u = pl.get('usage') if o.get('type') == 'token_usage_record' else None
                cnt_u = None
                if o.get('type') == 'event_msg' and pl.get('type') == 'token_count':
                    info = pl.get('info') or {}; cumulative = info.get('total_token_usage')
                    total = json.dumps(cumulative, sort_keys=True) if cumulative is not None else None
                    if total is None or total != last_total: cnt_u = info.get('last_token_usage')
                    last_total = total
                ids = {(kind, str(source[key])) for source in (o, pl) for kind, key in
                       (('response', 'response_id'), ('response', 'responseId'),
                        ('request', 'request_id'), ('request', 'requestId')) if source.get(key)}
                if rec_u or cnt_u: pending = None
                recs.append((ts, model, rec_u, cnt_u, ids))
            if pending: unreported.append(pending)
            # Resolve proven response identity before comparing token values: snapshots may change
            # usage or even contradict the recorded model. Never sum reports of the same response.
            parent = list(range(len(recs))); identity = {}

            def find(index):
                while parent[index] != index:
                    parent[index] = parent[parent[index]]; index = parent[index]
                return index

            for index, (_, _, ru, cu, ids) in enumerate(recs):
                if not (ru or cu): continue
                for response_id in ids:
                    if response_id in identity: parent[find(index)] = find(identity[response_id])
                    else: identity[response_id] = index
            groups = defaultdict(list)
            for index, (_, _, ru, cu, ids) in enumerate(recs):
                if ids and (ru or cu): groups[find(index)].append(index)
            for indices in groups.values():
                if len(indices) < 2: continue
                winner = max(indices, key=lambda i: (bool(recs[i][2]), recs[i][0],
                                                    usage_tokens(recs[i][2] or recs[i][3]), i))
                ts, m, ru, cu, ids = recs[winner]; usage = ru or cu
                models = {recs[i][1] for i in indices if recs[i][1]}
                if len(models) > 1:
                    usage = {'usage_missing': True, 'reported_usage': normalize_usage(usage),
                             'reason': 'conflicting models for one Codex response'}
                elif models: m = next(iter(models))
                for index in indices:
                    stamp, model_id, _, _, report_ids = recs[index]
                    recs[index] = (stamp, model_id, None, None, report_ids)
                recs[winner] = (ts, m, usage if ru else None, usage if not ru else None, ids)
            def signature(usage):
                return json.dumps({k: v for k, v in normalize_usage(usage).items() if k != 'total_tokens'}, sort_keys=True)
            explicit = defaultdict(list)
            for index, (ts, m, ru, _, ids) in enumerate(recs):
                if ru: explicit[(m, signature(ru))].append((ts, ids, index))
            paired = set(); last = None
            for position, (ts, m, ru, cu, ids) in enumerate(recs):
                usage = ru
                if cu:
                    reports = explicit[(m, signature(cu))]
                    match = next((index for stamp, rid, index in reports if index not in paired and
                                  ((ids and rid and ids & rid) or
                                   (stamp == ts and not (ids and rid and ids.isdisjoint(rid))))), None)
                    # Codex echoes each identified usage record as the next usage report: a `token_count`
                    # without ids, the same usage and model, a later timestamp, and no new turn or model output
                    # in between. That echo is the same response.
                    if match is None and not ids and last is not None and last not in paired and recs[last][2] \
                            and recs[last][4] and recs[last][1] == m and signature(recs[last][2]) == signature(cu):
                        match = last
                    if match is not None: paired.add(match)
                    elif not ru:
                        ambiguous = any(not (ids and rid and ids.isdisjoint(rid)) for _, rid, _ in reports)
                        usage = {'usage_missing': True, 'reported_usage': normalize_usage(cu),
                                 'reason': 'ambiguous Codex duplicate'} if ambiguous else cu
                sessions[sid].append((ts, branch, 'codex', m, usage, True))
                if ru or cu: last = position
                elif position in starts: last = None
            sessions[sid] += [(ts, branch, 'codex', m, {'usage_missing': True, 'reason': 'Codex response without usage'}, True)
                              for ts, m in unreported]
            sessions[sid] = _fill_models(sorted(sessions[sid], key=lambda r: r[0]))
    return sessions


def attribute(prs, sessions, links, prices, idle_min=5.0, now=None):
    """Agent activity and cost per PR number.

    A record belongs to a PR when its git branch is the PR's head branch inside the PR's window, or when
    its session links exactly that one PR (Claude Code `pr-link`) and its branch is no other PR's head.
    The executor is the agent/model with the most active hours in main (non-subagent) sessions.
    """
    now = now or datetime.now(timezone.utc)
    branches = {p['headRefName'] for p in prs}
    branch_starts = {}; linked_starts = {}
    for sid, recs in sessions.items():
        own = links.get(sid, set())
        single = next(iter(own)) if len(own) == 1 else None
        for rec in recs:
            ts, branch = rec[:2]
            branch_starts[branch] = min(ts, branch_starts.get(branch, ts))
            if single is not None and branch not in branches:
                linked_starts[single] = min(ts, linked_starts.get(single, ts))
    by_branch = defaultdict(list)
    for p in sorted(prs, key=lambda p: p['createdAt']): by_branch[p['headRefName']].append(p)
    windows = {}
    for plist in by_branch.values():
        prev_end = None
        for p in plist:
            starts = [utc(p['createdAt'])]
            starts += [date for c in p.get('commits', []) if (date := opt_utc(c.get('authoredDate')))]
            starts += [date for date in (opt_utc(p.get('estimateStartAt')), branch_starts.get(p['headRefName']),
                                        linked_starts.get(p['number'])) if date is not None]
            lo = prev_end if prev_end is not None else min(starts)
            hi = utc(p['mergedAt']) if p.get('mergedAt') else now  # post-merge follow-up is not counted
            windows[p['number']] = (lo, hi); prev_end = hi
    num_by_branch = {b: [p['number'] for p in pl] for b, pl in by_branch.items()}
    hit = defaultdict(list)
    for sid, recs in sessions.items():
        own = links.get(sid, set())  # every PR of this repo the session linked, known here or not (open PRs)
        single = next(iter(own)) if len(own) == 1 and next(iter(own)) in windows else None
        for rec in recs:
            ts, branch = rec[0], rec[1]; target = None
            for n in num_by_branch.get(branch, ()):
                lo, hi = windows[n]
                if lo <= ts <= hi: target = n; break
            if target is None and single is not None and branch not in branches:
                lo, hi = windows[single]
                if lo <= ts <= hi: target = single
            if target is not None: hit[target].append((sid, rec))
    out = {}
    for n, items in hit.items():
        usd = 0.0; unpriced = priced = missing_usage = 0; agents = {}; streams = defaultdict(list)
        usage_by_model = defaultdict(list); main_counts = Counter(); agent_intervals = defaultdict(list)
        for sid, (ts, _, agent, model, usage, main) in items:
            key = f"{agent}/{model or 'unknown'}"
            a = agents.setdefault(key, {'msgs': 0, 'tokens': 0, 'usd': None, 'known_usd': 0.0,
                                       'unpriced_msgs': 0, 'missing_usage_msgs': 0,
                                       'cost_complete': False, 'active_h': 0.0})
            streams[sid].append((ts, key, main))
            if main: main_counts[key] += 1
            if not usage: continue
            a['msgs'] += 1; a['tokens'] += usage_tokens(usage)
            normalized = normalize_usage(usage)
            usage_by_model[model or 'unknown'].append(normalized)
            if normalized.get('usage_missing'):
                missing_usage += 1; a['missing_usage_msgs'] += 1
                continue
            fam = model_family(model, prices)
            if fam:
                c = message_cost(usage, prices[fam]); usd += c; a['known_usd'] += c; priced += 1
            else: unpriced += 1; a['unpriced_msgs'] += 1
        intervals = []
        for recs in streams.values():
            recs.sort(key=lambda r: r[0])
            intervals += activity_intervals([r[0] for r in recs], idle_min)
            for (lo, key, main), (hi, _, next_main) in zip(recs, recs[1:]):
                if main and next_main and 0 < (hi - lo).total_seconds() <= idle_min * 60:
                    agent_intervals[key].append((lo, hi))
        for key, a in agents.items():
            a['active_h'] = round(interval_hours(agent_intervals[key]), 4)
            a['cost_complete'] = bool(a['msgs']) and a['unpriced_msgs'] == 0 and a['missing_usage_msgs'] == 0
            a['usd'] = a['known_usd'] if a['cost_complete'] else None
        ranked = sorted(main_counts, key=lambda k: (agents[k]['active_h'], main_counts[k], k), reverse=True)
        complete = bool(priced) and unpriced == 0 and missing_usage == 0
        out[n] = {'first_ts': min(r[0] for _, r in items), 'active_h': interval_hours(intervals),
                  'usd': usd if complete else None, 'known_usd': usd, 'cost_complete': complete,
                  'usage_by_model': dict(usage_by_model), 'unpriced_msgs': unpriced,
                  'missing_usage_msgs': missing_usage,
                  'sessions': len(streams), 'pricing_basis': PRICING_BASIS,
                  'agents': agents, 'executor': ranked[0] if ranked else None}
    return out


# ---------- GitHub ----------

def run_gh(args):
    try:
        r = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        sys.exit('estimate: the GitHub CLI `gh` is required (https://cli.github.com)')
    if r.returncode: sys.exit(f'estimate: gh {" ".join(args[:3])} failed: {r.stderr.strip()[:400]}')
    return json.loads(r.stdout or 'null')


def detect_repo():
    return run_gh(['repo', 'view', '--json', 'nameWithOwner'])['nameWithOwner']


MERGED_QUERY = """query($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, first: $first, after: $after, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title body createdAt mergedAt additions deletions changedFiles headRefName
        files(first: 100) { nodes { path additions deletions } }
        reviews(first: 1, states: [APPROVED, CHANGES_REQUESTED]) { totalCount }
        commits(first: 100) { nodes { commit { authoredDate } } } } } } }"""


def _flat_pr(n):
    return {**{k: n[k] for k in ('number', 'title', 'body', 'createdAt', 'mergedAt', 'additions', 'deletions',
                                 'changedFiles', 'headRefName')},
            'files': (n.get('files') or {}).get('nodes') or [],
            'reviews': [{}] * ((n.get('reviews') or {}).get('totalCount') or 0),
            'commits': [{'authoredDate': c['commit']['authoredDate']} for c in (n.get('commits') or {}).get('nodes') or []]}


def fetch_prs(repo, state, limit):
    """Merged PRs via paginated GraphQL (gh pr list overflows the node limit on files + commits)."""
    if state != 'merged':
        return run_gh(['pr', 'list', '--repo', repo, '--state', state, '--limit', str(limit), '--json', OPEN_FIELDS])
    owner, name = repo.split('/', 1); out = []; after = None
    while len(out) < limit:
        args = ['api', 'graphql', '-f', f'query={MERGED_QUERY}', '-F', f'owner={owner}', '-F', f'name={name}',
                '-F', f'first={min(50, limit - len(out))}']
        if after: args += ['-F', f'after={after}']
        page = run_gh(args)['data']['repository']['pullRequests']
        out += [_flat_pr(n) for n in page['nodes']]
        if not page['pageInfo']['hasNextPage']: break
        after = page['pageInfo']['endCursor']
    return out


def branch_has_pr(repo, branch):
    """Whether `branch` has an open or merged PR (any age; the fetched history page may not reach it)."""
    found = run_gh(['pr', 'list', '--repo', repo, '--head', branch, '--state', 'all', '--limit', '20', '--json', 'state'])
    return any(p.get('state') in ('OPEN', 'MERGED') for p in found or [])


def fetch_pr(repo, number):
    """Fetch a tracked PR even when it falls outside the recent history page."""
    fields = 'number,title,body,createdAt,mergedAt,additions,deletions,changedFiles,headRefName,files,reviews,commits'
    p = run_gh(['pr', 'view', str(number), '--repo', repo, '--json', fields])
    p['reviews'] = [r for r in p.get('reviews', []) if r.get('state') in ('APPROVED', 'CHANGES_REQUESTED')]
    return p


def areas(files, depth=2, limit=8):
    c = Counter('/'.join(f['path'].split('/')[:depth]) if '/' in f['path'] else '(root)' for f in files or [])
    return [a for a, _ in c.most_common(limit)]


def pr_actual(p, agent):
    """Measured cost of one merged PR. Never filled in by hand."""
    created = utc(p['createdAt']); merged = utc(p['mergedAt'])
    bound = opt_utc(p.get('previousMergedAt'))
    starts = [(created, 'pr-created')]
    for c in p.get('commits') or []:
        d = opt_utc(c.get('authoredDate'))
        if d and d <= merged: starts.append((d, 'commit'))
    made = opt_utc(p.get('estimateStartAt'))
    if made and made <= merged:
        starts.append((made, 'estimate'))
    if agent and agent.get('first_ts') and agent['first_ts'] <= merged:
        first = agent['first_ts']
        # The agent that logs the estimate usually reads the task in the same session first: activity a few
        # minutes before the estimate, with no commit or PR before it, is that sizing, not work.
        sizing = (made and made <= merged and timedelta(0) < made - first <= timedelta(minutes=SIZING_GRACE_MIN)
                  and all(ts >= made for ts, _ in starts))
        if not sizing: starts.append((first, 'agent-log'))
    if bound:
        starts = [(ts, source) for ts, source in starts if ts >= bound]
        if not starts: starts = [(bound, 'previous-merge-bound')]
    start, start_source = min(starts)
    wall = max(hours(start, merged), 0.0)
    a = {'pr': p['number'], 'title': p.get('title', ''), 'branch': p.get('headRefName'), 'merged_at': iso(merged),
         'start_at': iso(start), 'start_source': start_source, 'wall_h': round(wall, 4),
         'active_h': None, 'active_basis': 'log-gap heuristic', 'wait_h': None, 'unobserved_h': None,
         'usd': None, 'known_usd': None, 'cost_complete': False, 'unpriced_msgs': 0,
         'missing_usage_msgs': 0, 'usage_by_model': {},
         'measurement_version': __version__, 'pricing_basis': PRICING_BASIS,
         'review_rounds': len(p.get('reviews') or []),
         'fix_commits': sum(1 for c in p.get('commits') or [] if (opt_utc(c.get('authoredDate')) or created) > created),
         'files': p.get('changedFiles'), 'lines': (p.get('additions') or 0) + (p.get('deletions') or 0),
         'areas': areas(p.get('files')), 'sessions': 0, 'executor': None, 'agents': {}, 'provenance': 'auto'}
    if agent:
        a.update(active_h=round(agent['active_h'], 4), unobserved_h=round(max(wall - agent['active_h'], 0), 4),
                 usd=round(agent['usd'], 4) if agent.get('cost_complete') is True and agent.get('usd') is not None else None,
                 known_usd=round(agent.get('known_usd', 0), 4), cost_complete=agent.get('cost_complete') is True,
                 unpriced_msgs=agent.get('unpriced_msgs', 0), usage_by_model=agent.get('usage_by_model', {}),
                 missing_usage_msgs=agent.get('missing_usage_msgs', 0),
                 pricing_basis=agent.get('pricing_basis', PRICING_BASIS),
                 sessions=agent['sessions'], executor=agent['executor'], agents=agent['agents'])
    return a


# ---------- ledger ----------

class Store:
    def __init__(self, path):
        self.dir = Path(path); self.ledger = self.dir / 'ledger.jsonl'

    def ensure(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        gi = self.dir / '.gitignore'
        if not gi.exists(): gi.write_text('# local estimate data; never commit\n*\n')

    def append(self, events):
        self.ensure()
        with self.ledger.open('a', encoding='utf-8') as fh:
            for e in events: fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + '\n')

    def rows(self):
        """Fold the append-only event log into one row per estimate (latest actual wins)."""
        rows = {}
        if not self.ledger.exists(): return []
        for line in self.ledger.read_text(encoding='utf-8').splitlines():
            if not line.strip(): continue
            try: e = json.loads(line); e['kind']; e['id']
            except (ValueError, KeyError, TypeError):
                print(f'estimate: skipping a damaged ledger line: {line[:80]!r}', file=sys.stderr); continue
            if e['kind'] == 'estimate': rows[e['id']] = dict(e)
            elif e['id'] in rows:
                if e['kind'] == 'actual': rows[e['id']]['actual'] = {k: v for k, v in e.items() if k not in ('kind', 'id')}
                elif e['kind'] == 'superseded': rows[e['id']]['superseded'] = True
                elif e['kind'] == 'no-pr': rows[e['id']]['no_pr'] = e.get('at') or True
        return list(rows.values())

    def read_json(self, name, default=None):
        p = self.dir / name
        return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default

    def write_json(self, name, obj):
        self.ensure(); p = self.dir / name; tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8'); tmp.replace(p)

    def prices(self):
        return self.read_json('prices.json') or DEFAULT_PRICES


def detect_agent():
    if os.environ.get('CLAUDECODE'): return 'claude-code'
    if any(k in os.environ for k in ('CODEX_THREAD_ID', 'CODEX_SANDBOX', 'CODEX_SANDBOX_NETWORK_DISABLED')): return 'codex'
    return 'unknown'


def estimator_key(r):
    e = r.get('estimator') or {}
    return f"{e.get('agent') or 'unknown'}/{e.get('model') or 'unknown'}"


def new_estimate(sp, task_ref, source, created_at, agent, model, closest=None, quote=None, calib_id=None):
    return {'kind': 'estimate', 'id': 'e-' + uuid.uuid4().hex[:10], 'created_at': iso(created_at),
            'task_ref': task_ref, 'sp': sp, 'source': source, 'estimator': {'agent': agent, 'model': model},
            'closest_pr': closest, 'quote': quote, 'calib_id': calib_id}


def settled(rows):
    return [r for r in rows if r.get('actual') and r.get('sp') and not r.get('superseded')]


def unsettled(rows):
    """Estimates without an actual, including those marked 'no PR' (a late PR still settles them)."""
    return [r for r in rows if not r.get('actual') and not r.get('superseded')]


def pending_rows(rows):
    """Estimates awaiting settlement: unsettled and not marked 'no PR'."""
    return [r for r in unsettled(rows) if not r.get('no_pr')]


def no_pr_marks(pending, merged, now, days=NO_PR_DAYS):
    """Candidate 'no-pr' events (with the branch as `ref`) for branch estimates older than `days` that no
    fetched merged PR settles; `record` drops those whose branch has an open or merged PR before writing."""
    if days <= 0: return []
    old = now - timedelta(days=days)
    return [{'kind': 'no-pr', 'id': r['id'], 'at': iso(now), 'after_days': days, 'ref': r['task_ref']} for r in pending
            if task_pr(r['task_ref']) is None and utc(r['created_at']) <= old
            and not match_pr(r, merged)]


def one_per_pr(rows):
    """Several estimators may size the same PR; overall stats count each PR once, preferring an estimate
    made before work over a retro one, then the earliest."""
    best = {}
    for r in sorted(rows, key=lambda r: (r.get('source') == 'retro', r['created_at'])):
        best.setdefault(r['actual']['pr'], r)
    return list(best.values())


def task_pr(task_ref):
    m = re.fullmatch(r'#?(\d+)', str(task_ref or '').strip())
    return int(m.group(1)) if m else None


def match_pr(est, merged):
    """The merged PR an estimate refers to: by number, or the earliest PR on that branch merged after
    the estimate was made (an older PR that reused the branch name never matches)."""
    n = task_pr(est['task_ref']); made = utc(est['created_at'])
    if n: return next((p for p in merged if p['number'] == n), None)
    cands = [p for p in merged if p['headRefName'] == est['task_ref'] and utc(p['mergedAt']) >= made
             and utc(p['createdAt']) >= made - timedelta(days=14)]
    return min(cands, key=lambda p: p['mergedAt']) if cands else None


# ---------- calibration ----------

def throughput(rows, start, end, merged_times=None, tz=None):
    """SP merged per local day, from the first sized merge in the window to `end`.

    `samples` pairs each day's SP with the share of that day inside the span (the first and last days
    are partial), so resampling them reproduces the span's SP/day instead of dropping edge-day merges.
    """
    tz = tz or datetime.now().astimezone().tzinfo
    if not rows: return None
    t0 = max(start, min(utc(r['actual'].get('start_at') or r['actual']['merged_at']) for r in rows))
    d0 = t0.astimezone(tz).date(); d1 = end.astimezone(tz).date()
    days = {d0 + timedelta(days=i): 0 for i in range((d1 - d0).days + 1)}
    for r in rows:
        d = utc(r['actual']['merged_at']).astimezone(tz).date()
        if d in days: days[d] += r['sp']
    allv = list(days.values()); span_days = max(hours(t0, end) / 24, 1 / 24)
    midnight = lambda d: datetime(d.year, d.month, d.day, tzinfo=tz)
    samples = []
    for d, v in days.items():
        length = max(hours(max(t0, midnight(d)), min(end, midnight(d + timedelta(days=1)))) / 24, 0)
        # A merge exactly at a midnight `end` (or `t0`) leaves a zero-length day: keep its SP in the neighbour.
        if length <= 0 and samples: samples[-1][0] += v
        elif length <= 0: samples.append([v, 0])
        elif samples and samples[-1][1] <= 0: samples[-1] = [samples[-1][0] + v, length]
        else: samples.append([v, length])
    cov = None
    if merged_times is not None:
        total = sum(1 for t in merged_times if t0 <= t <= end)
        cov = round(sum(1 for r in rows if utc(r['actual']['merged_at']) >= t0) / total, 3) if total else None
    return {'start': iso(t0), 'days': [[d.isoformat(), v] for d, v in days.items()], 'samples': samples,
            'sp': sum(allv), 'merge_days': sum(1 for v, _ in samples if v),
            'sp_per_day': round(sum(allv) / span_days, 2) if span_days >= 1 else None, 'span_days': round(span_days, 2),
            'full_days': max(len(allv) - 2, 0), 'coverage': cov}


def metric_value(actual, metric):
    """A usable measured metric; legacy or partial dollar totals are unverified until refreshed."""
    if metric == 'usd' and actual.get('cost_complete') is not True: return None
    return actual.get(metric)


def _rates(rs, m):
    return [value / (r['sp'] if m in SCALED else 1) for r in rs
            if (value := metric_value(r['actual'], m)) is not None]


def loo_resid(rows, m):
    """p20 and p80 of log(actual / leave-one-out forecast) over `rows`, or None below MIN_INTERVAL_N.

    Each row is forecast from the other rows the way `quote` would: its SP bucket's median when that has
    MIN_INTERVAL_N rows, else the median per-SP rate times its SP. That is the error a new task meets, which
    the in-sample p20-p80 of a few values understates (the median itself is uncertain).
    """
    pts = [(r['sp'], v) for r in rows if (v := metric_value(r['actual'], m)) and v > 0]
    if len(pts) < MIN_INTERVAL_N: return None
    res = []
    for i, (sp, v) in enumerate(pts):
        others = [p for j, p in enumerate(pts) if j != i]
        same = [w for s, w in others if s == sp]
        mid = statistics.median(same) if len(same) >= MIN_INTERVAL_N else statistics.median(w / s for s, w in others) * sp
        res.append(math.log(v / mid))
    return [round(percentile(res, .2), 4), round(percentile(res, .8), 4)]


def _stats(cur, prev):
    """Per-SP rates, Fibonacci buckets, drift against the previous window, and consistency."""
    s = {'n': len(cur), 'prework': sum(map(_prospective_estimate, cur)),
         'sources': dict(Counter(r.get('source') for r in cur)),
         'per_sp': {m: _rates(cur, m) for m in METRICS}, 'buckets': {}, 'drift': {},
         'executors': dict(Counter(r['actual'].get('executor') or 'unknown' for r in cur))}
    for sp in sorted({r['sp'] for r in cur}):
        b = [r for r in cur if r['sp'] == sp]
        s['buckets'][str(sp)] = {'n': len(b), **{m: spread([metric_value(r['actual'], m) for r in b]) for m in METRICS}}
    for m in SCALED:
        a, b = _rates(cur, m), _rates(prev, m)
        if len(a) >= MIN_INTERVAL_N and len(b) >= MIN_INTERVAL_N and statistics.median(b) > 0:
            ratio = statistics.median(a) / statistics.median(b)
            s['drift'][m] = {'ratio': round(ratio, 2), 'flag': ratio > 2 or ratio < .5}
    anch = [r for r in cur if r.get('closest_pr')]; free = [r for r in cur if not r.get('closest_pr')]
    s['loo_resid'] = {m: loo_resid(cur, m) for m in SCALED}
    s['consistency'] = {m: ratio_p80_p20(_rates(cur, m)) for m in SCALED}
    s['anchored'] = {'n': len(anch), 'wall_h': ratio_p80_p20(_rates(anch, 'wall_h')), 'active_h': ratio_p80_p20(_rates(anch, 'active_h'))}
    s['unanchored'] = {'n': len(free), 'wall_h': ratio_p80_p20(_rates(free, 'wall_h')), 'active_h': ratio_p80_p20(_rates(free, 'active_h'))}
    return s


def calibrate(rows, now, window_days=14.0, since=None, merged_times=None, tz=None):
    """Recent-window calibration, overall and per estimator (agent/model)."""
    end = now; start = end - timedelta(days=window_days)
    if since and since > start: start = since
    start = min(start, end)
    done = settled(rows)
    in_win = lambda r, a, b: a <= utc(r['actual']['merged_at']) <= b
    cur_all = [r for r in done if in_win(r, start, end)]
    prev_lo = max(start - (end - start), since) if since else start - (end - start)
    prev_all = [r for r in done if in_win(r, prev_lo, start - timedelta(microseconds=1))] if prev_lo < start else []
    # One independent outcome per PR: prefer the earliest non-retro estimate, rather than giving
    # PRs sized by many models extra weight. Per-estimator comparisons remain in groups below.
    cur, prev = one_per_pr(cur_all), one_per_pr(prev_all)
    cal = {'id': 'c-' + now.strftime('%Y%m%dT%H%M%S'), 'created_at': iso(now), 'version': __version__,
           'window': {'start': iso(start), 'end': iso(end), 'days': round(hours(start, end) / 24, 2),
                      'since': iso(since) if since else None},
           **_stats(cur, prev), 'groups': {}}
    for key in sorted({estimator_key(r) for r in cur_all}):
        cal['groups'][key] = _stats(one_per_pr([r for r in cur_all if estimator_key(r) == key]),
                                    one_per_pr([r for r in prev_all if estimator_key(r) == key]))
    cal['prs'] = len({r['actual']['pr'] for r in cur_all})
    cal['throughput'] = throughput(one_per_pr(cur_all), start, end, merged_times, tz)
    cal['audit'] = sample_audit(rows, cur, start, end, since)
    return cal


def _cost_gaps(a):
    """Why one measured PR has no usable dollar total; a PR can have several reasons."""
    if not a.get('sessions'): return ['no agent logs matched the PR branch']
    if a.get('cost_complete') is None and a.get('usd') is not None:
        return ['cost measured by an older version (run `estimate record --refresh`)']
    agents = a.get('agents') or {}
    out = [f'no price for {k}' for k in sorted(agents) if agents[k].get('unpriced_msgs')]
    if a.get('unpriced_msgs') and not out: out.append('no price for a model')
    who = [f'{k} responses logged without token usage' for k in sorted(agents) if agents[k].get('missing_usage_msgs')]
    out += who or (['agent responses logged without token usage'] if a.get('missing_usage_msgs') else [])
    return out or ['cost incomplete in the logs']


def sample_audit(rows, used, start, end, since=None):
    """Why calibration n (and each metric's n) is below what was sized: PRs per reason.

    Calibration reasons are exclusive; dollar reasons are not (one PR can lack a price and usage).
    """
    pending = pending_rows(rows)
    done_keys = {(estimator_key(r), k) for r in settled(rows) for k in (str(r.get('task_ref')), f"#{r['actual']['pr']}")}
    duplicate = lambda r: (estimator_key(r), str(r.get('task_ref'))) in done_keys or (
        (n := task_pr(r.get('task_ref'))) is not None and (estimator_key(r), f'#{n}') in done_keys)
    by_pr = {}
    for r in settled(rows): by_pr.setdefault(r['actual']['pr'], r)
    in_use = {r['actual']['pr'] for r in used}
    dropped = defaultdict(list)
    for n, r in sorted(by_pr.items()):
        if n in in_use: continue
        merged = utc(r['actual']['merged_at'])
        if merged < start and since and since >= start: why = f'merged before the saved --since {iso(since)[:10]}'
        elif merged < start: why = f'merged before the {round(hours(start, end) / 24, 2):g}-day window (before {iso(start)[:10]}; --window widens it)'
        elif merged > end: why = 'merged after the window end'
        else: why = 'other'
        dropped[why].append(n)
    metrics = {}
    for m, label in (('active_h', 'observed active h'), ('usd', 'API-equivalent $')):
        gaps = defaultdict(list); have = 0
        for r in sorted(used, key=lambda r: r['actual']['pr']):
            a = r['actual']
            if metric_value(a, m) is not None: have += 1; continue
            reasons = _cost_gaps(a) if m == 'usd' else (
                ['no agent logs matched the PR branch'] if not a.get('sessions') else ['no observed activity'])
            for why in reasons: gaps[why].append(a['pr'])
        metrics[m] = {'label': label, 'n': have, 'of': len(used), 'missing': dict(gaps)}
    return {'sized_prs': len(by_pr), 'used': len(used), 'dropped': dict(dropped),
            'extra_estimates': sum(1 for r in settled(rows) if start <= utc(r['actual']['merged_at']) <= end) - len(used),
            'pending': [str(r.get('task_ref')) for r in pending if not duplicate(r)],
            'duplicates': [str(r.get('task_ref')) for r in pending if duplicate(r)],
            'no_pr': [str(r.get('task_ref')) for r in unsettled(rows) if r.get('no_pr')], 'metrics': metrics}


def _pr_list(prs, limit=8):
    shown = ', '.join(f'#{n}' for n in prs[:limit])
    return shown + (f' +{len(prs) - limit} more' if len(prs) > limit else '')


def audit_lines(audit, prices=None):
    """Plain-text explanation of the sample sizes, first line the calibration n.

    `prices` is the prices.json of the data directory in use, named when a model has no price.
    """
    if not audit: return []
    out = [f"calibration n={audit['used']} of {audit['sized_prs']} sized PRs"
           + ('' if audit['used'] < audit['sized_prs'] else ' (all of them are in the window)')]
    for why, prs in audit['dropped'].items(): out.append(f'  {len(prs)} {why}: {_pr_list(prs)}')
    if audit.get('extra_estimates'):
        out.append(f"  {audit['extra_estimates']} more estimates of the same PRs (other estimators) count once per PR")
    if audit.get('pending'):
        out.append(f"  {len(audit['pending'])} logged estimates await settlement (PR not merged, or merged and not yet "
                   f"recorded: run `estimate record`): {', '.join(audit['pending'][:8])}")
    if audit.get('duplicates'):
        out.append(f"  {len(audit['duplicates'])} logged estimates repeat an already settled estimate by the same estimator "
                   f"and will not count: {', '.join(audit['duplicates'][:8])}")
    for m in audit['metrics'].values():
        if m['n'] == m['of']: continue
        out.append(f"{m['label']} n={m['n']} of {m['of']}" + ('; a PR can have several reasons' if len(m['missing']) > 1 else ''))
        for why, prs in m['missing'].items(): out.append(f'  {len(prs)} {why}: {_pr_list(prs)}')
        if any(why.startswith('no price for') for why in m['missing']):
            out.append(f"  add the missing model rates to {prices or 'prices.json in the data directory'}")
    return out


def audit_summary(audit):
    """One line for forecasts: calibration n and how many PRs or estimates the full audit lists, or None
    when nothing is missing. The full list is in `estimate doctor` and `estimate record`."""
    if not audit: return None
    parts = []
    if out := sum(len(v) for v in audit['dropped'].values()): parts.append(f'{out} outside the window')
    no_logs = {n for m in audit['metrics'].values() for why, prs in m['missing'].items() if why.startswith('no agent logs') for n in prs}
    if no_logs: parts.append(f"{len(no_logs)} PR{'s' if len(no_logs) != 1 else ''} without agent logs")
    other = {n for m in audit['metrics'].values() for why, prs in m['missing'].items() if not why.startswith('no agent logs') for n in prs} - no_logs
    if other: parts.append(f"{len(other)} PR{'s' if len(other) != 1 else ''} without a full $ or active-time measure")
    if audit.get('pending'): parts.append(f"{len(audit['pending'])} estimate{'s' if len(audit['pending']) != 1 else ''} awaiting `estimate record`")
    repeats = len(audit.get('duplicates') or []) + (audit.get('extra_estimates') or 0)
    if repeats: parts.append(f"{repeats} repeated estimate{'s' if repeats != 1 else ''} of the same PRs count once")
    if not parts: return None
    return f"calibration n={audit['used']} of {audit['sized_prs']} sized PRs; " + ', '.join(parts) + ': see `estimate doctor`'


def quote(cal, sp, now=None, estimator=None):
    """SP -> per-metric ranges and confidence from independent PR outcomes.

    Bucket percentiles when the SP bucket has MIN_INTERVAL_N rows, else pooled per-SP rates times SP.
    An estimator's own group is used once it has ESTIMATOR_MIN_N settled estimates logged before work;
    otherwise all estimators are pooled and `estimator_prework` says how far it is from its own ranges.
    Overall confidence and n are conservative minima of the quoted time/cost metrics. Review rounds
    carry their own confidence but do not determine the time/cost forecast's aggregate confidence.
    """
    now = now or utc(cal['created_at'])
    g = (cal.get('groups') or {}).get(estimator) if estimator else None
    k = g.get('prework', 0) if g else 0
    src, scope = (g, estimator) if k >= ESTIMATOR_MIN_N else (cal, ALL)
    q = {'sp': sp, 'calib_id': cal['id'], 'scope': scope, 'estimator': estimator, 'estimator_prework': k}
    b = src['buckets'].get(str(sp)) or {}
    age = max(hours(utc(cal['window']['end']), now) / 24, 0)
    for m in METRICS:
        bv = b.get(m)
        if bv and bv['n'] >= MIN_INTERVAL_N:
            qm = {**bv, 'basis': f'{sp} SP bucket'}
        else:
            rs = src['per_sp'].get(m) or []
            s = spread([x * sp for x in rs] if m in SCALED else rs)
            if not s: continue
            qm = {**s, 'basis': 'per-SP rate × SP' if m in SCALED else 'all sizes'}
        res = (src.get('loo_resid') or {}).get(m)
        if res and qm['kind'] == 'p20-p80':
            # Widen to the leave-one-out forecast error, so p20-p80 holds about 60% of new tasks, not of past ones.
            qm.update(lo=min(qm['lo'], qm['mid'] * math.exp(res[0])), hi=max(qm['hi'], qm['mid'] * math.exp(res[1])))
        ratio = qm['hi'] / qm['lo'] if qm['lo'] > 0 else None
        drifted = (src.get('drift', {}).get(m) or {}).get('flag', False)
        qm['confidence'] = confidence(qm['n'], ratio, age, drifted)
        if m in SCALED and qm['basis'] != f'{sp} SP bucket' and qm['confidence'] == 'high':
            qm['confidence'] = 'medium'  # assumes linear SP
        q[m] = qm
    measured = [q[m] for m in SCALED if q.get(m)]
    levels = ('none', 'low', 'medium', 'high')
    q['confidence'] = min((v['confidence'] for v in measured), key=levels.index) if measured else 'none'
    q['n'] = min((v['n'] for v in measured), default=0)
    q['window_days'] = cal['window']['days']
    q['drift'] = any(d['flag'] for d in src['drift'].values())
    return q


def fmt_h(lo, hi):
    if hi < 1: return f'{round(lo * 60):d}–{max(round(hi * 60), 1):d} min'
    f = lambda x: f'{x:.1f}' if x < 10 else f'{x:.0f}'
    return f'{f(lo)}–{f(hi)} h'


def fmt_usd(lo, hi):
    f = lambda x: f'{x:.2f}' if x < 1 else (f'{x:.1f}' if x < 10 else f'{x:.0f}')
    return f'${f(lo)}–{f(hi)}'


def quote_line(q, closest=None):
    head = f"{q['sp']} SP" + (f' (closest #{closest})' if closest else '')
    if q['confidence'] == 'none': return head + ' · no settled history yet: measuring starts with this estimate'
    parts = [head]
    def detail(m):
        v = q[m]
        return f" (n={v['n']}, {v['confidence']}, {v['kind']})"
    if q.get('active_h'): parts.append('observed active ≈' + fmt_h(q['active_h']['lo'], q['active_h']['hi']) + detail('active_h'))
    if q.get('wall_h'): parts.append('wall ' + fmt_h(q['wall_h']['lo'], q['wall_h']['hi']) + detail('wall_h'))
    if q.get('usd'): parts.append(fmt_usd(q['usd']['lo'], q['usd']['hi']) + detail('usd'))
    if q.get('review_rounds') and q['review_rounds']['hi'] > 0:
        parts.append(f"{q['review_rounds']['mid']:.0f} review rounds typical" + detail('review_rounds'))
    tail = f"{q['confidence']}: n≥{q['n']} ({scope_note(q)}), window {q['window_days']:g} d" + (', drift' if q['drift'] else '')
    return ' · '.join(parts + [tail])


def scope_note(q):
    """Which ranges a quote used, and the estimator's own pre-work sample size."""
    k, est = q.get('estimator_prework', 0), q.get('estimator')
    if q['scope'] != ALL: return f'own ranges of {est}, {k} pre-work PRs'
    if not est: return 'pooled: all estimators'
    return f'pooled: all estimators; {est} has {k}/{ESTIMATOR_MIN_N} pre-work PRs'


# ---------- forecasts ----------

_COLUMNS = {'id': ('id', '#'), 'title': ('title', 'item', 'task'), 'sp': ('effort', 'sp', 'e'),
            'value': ('value', 'v'), 'depends': ('depends', 'depends on', 'deps'), 'key': ('key',), 'note': ('note', 'notes'),
            'lane': ('lane',)}
SHORT_ID = 8                                   # a UUID id is shown, and may be referenced, by this many leading chars
_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def _cells(line):
    """Split on unescaped pipes; drop at most one outer delimiter per side; keep empty cells."""
    line = line.strip()
    if line.startswith('|'): line = line[1:]
    if line.endswith('|') and not line.endswith('\\|'): line = line[:-1]
    return [c.replace('\\|', '|').strip() for c in re.split(r'(?<!\\)\|', line)]


def _header(line):
    """Column index per field when the row is a backlog header (title and effort columns), else None."""
    if not line.startswith('|'): return None
    names = [c.lower() for c in _cells(line)]
    cols = {k: next((i for i, n in enumerate(names) if n in aliases), None) for k, aliases in _COLUMNS.items()}
    return cols if cols['sp'] is not None and cols['title'] is not None else None


def _depends(cell):
    """`3, 5` or `3-6` (numeric range); `-`/empty means none."""
    out = []
    for part in re.split(r'[,\s]+', cell.strip()):
        part = part.strip().lstrip('#')
        if not part or part in ('-', '–', '—'): continue
        m = re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)', part)
        if m and int(m.group(1)) <= int(m.group(2)): out += [str(n) for n in range(int(m.group(1)), int(m.group(2)) + 1)]
        else: out.append(part)
    return out


def _parse_table(cols, lines, unsized=None):
    items, row = [], 0
    for line in lines:
        cells = _cells(line)
        if all(re.fullmatch(r':?-+:?', c) for c in cells if c): continue           # |---|---|
        get = lambda k: cells[cols[k]] if cols[k] is not None and cols[k] < len(cells) else ''
        effort, value = get('sp'), get('value'); row += 1  # implicit ids follow file rows, skipped ones included
        if not re.fullmatch(r'\d+', effort) and unsized is not None:
            unsized.append({'id': get('id').lstrip('#') or str(row), 'key': get('key'),
                            'title': get('title') or f'item {row}', 'note': get('note'), 'implicit': not get('id').lstrip('#')}); continue
        if not re.fullmatch(r'\d+', effort): raise ValueError(f'backlog item has no effort, size it first: {line.strip()!r}')
        if value and not re.fullmatch(r'\d+', value): raise ValueError(f'backlog value must be one of {VALUES}: {line.strip()!r}')
        items.append({'id': get('id').lstrip('#') or str(row), 'title': get('title') or f'item {row}',
                      'sp': int(effort), 'value': int(value) if value else None, 'depends': _depends(get('depends')),
                      'key': get('key'), 'note': get('note'), 'lane': get('lane'), 'line': line.strip(), 'implicit': not get('id').lstrip('#')})
    return items


def _parse_lines(lines):
    items = []
    for line in lines:
        m = re.match(r'^(.*?)\s*[|;,]\s*(\d+)\s*(?:sp)?$', line, re.I)       # "title | SP"
        if m: title, sp = m.group(1), int(m.group(2))
        else:
            m = re.match(r'^(\d+)\s*(?:sp\b)?(?:\s*[:,|-]\s*|\s+)(.+)$', line, re.I)  # "SP title", "5sp: title"
            if m: sp, title = int(m.group(1)), m.group(2)
            else:
                m = re.match(r'^(.*\S)\s+(\d+)\s*(?:sp)?$', line, re.I)      # "title 3"
                if not m: raise ValueError(f'backlog line has no story points: {line!r}')
                title, sp = m.group(1), int(m.group(2))
        items.append({'id': str(len(items) + 1), 'sp': sp, 'title': title.strip(' \t-:|,;') or f'item {len(items) + 1}',
                      'value': None, 'depends': [], 'key': '', 'note': '', 'lane': '', 'line': line})
    return items


def parse_backlog(text, unsized=None):
    """A Markdown table `| id | title | effort | value | depends |`, or one `<SP> <title>` per line.

    With a table, only the first contiguous table under a header with title and effort columns is read;
    other lines (headings, prose, other tables) are ignored. Without one, '#' starts a comment line and
    every other line must carry story points; such items have no value, so no Score.
    A table row without effort raises, unless `unsized` is a list: the row then goes there (id, key,
    title, label) instead of into the items, and `depends` naming it resolves to its id.
    """
    lines = [raw.strip() for raw in text.splitlines()]
    start = next((n for n, line in enumerate(lines) if _header(line)), None)
    if start is None: items = _parse_lines([line for line in lines if line and not line.startswith('#')])
    else:
        end = next((n for n in range(start + 1, len(lines)) if not lines[n].startswith('|')), len(lines))
        items = _parse_table(_header(lines[start]), lines[start + 1:end], unsized)
    for i in items:
        if i['sp'] not in FIB: raise ValueError(f'backlog SP must be one of {FIB}: {i["line"]!r}')
        if i['value'] is not None and i['value'] not in VALUES:
            raise ValueError(f'backlog value must be one of {VALUES}: {i["line"]!r}')
        del i['line']
        i['score'] = round(i['value'] / i['sp'], 2) if i['value'] is not None else None
    every = items + (unsized or [])
    # A hand-written id (`1`, `BL-3`) is what the author refers to the row by, so it stays its label even with a key.
    labels, resolve = id_aliases([(i['id'], i['key']) for i in every],
                                 own={i['id'] for i in every if not i.pop('implicit', True) and not re.fullmatch(_UUID, i['id'].lower())})
    for i in every:
        i['label'] = labels[i['id']]
    for i in items:
        i['depends'] = [resolve(r) for r in i['depends']]
    return items


def id_aliases(pairs, own=()):
    """(id -> label, resolve) for (id, key) pairs. The label is the id itself for ids in `own`, else the
    human key (KEY-21) when there is one, else the 8-char prefix of a UUID id when no other id shares it,
    else the id. `resolve(ref)` maps the full id, else the key, else either one in another case, else a
    UUID prefix of at least 8 chars, each only when it names one id. An ambiguous or unknown ref comes back unchanged (an unknown dependency)."""
    ids = list(dict.fromkeys(i for i, _ in pairs))
    uuids = [i for i in ids if re.fullmatch(_UUID, i.lower())]
    prefix = Counter(i[:SHORT_ID].lower() for i in uuids)
    labels, keys, folded = {}, defaultdict(set), defaultdict(set)
    for i, key in reversed(pairs):  # the first of duplicate ids wins
        labels[i] = i if i in own else key or (i[:SHORT_ID] if i in uuids and prefix[i[:SHORT_ID].lower()] == 1 else i)
    for i, key in pairs:
        if key: keys[key].add(i); folded[key.lower()].add(i)
    for i in ids: folded[i.lower()].add(i)

    def resolve(ref):
        if ref in labels: return ref
        for found in (keys.get(ref), folded.get(ref.lower()),
                      {u for u in uuids if u.lower().startswith(ref.lower())} if len(ref) >= SHORT_ID else None):
            if found: return next(iter(found)) if len(found) == 1 else ref
        return ref
    return labels, resolve


def order_backlog(items, unsized=()):
    """Score desc (unscored last, ties by file order), but never before an item it depends on.

    Returns (ordered items, warnings). Each ordered item gets `rank` and, when a dependency pushed it
    below its pure Score rank, `waits_for` (the dependency placed last); an item placed before a
    higher-Score item that depends on it, directly or through other items, gets `unblocks` (those
    items' ids). Items with an unknown dependency or in (or behind) a dependency cycle are not ranked
    by Score: they follow, in file order. A dependency on one of the skipped `unsized` items counts as
    unknown, with its own warning.
    """
    warnings, n = [], len(items)
    tag = lambda it: '#' + (it.get('label') or it['id'])
    key = lambda i: (items[i]['score'] is None, -(items[i]['score'] or 0), i)
    index = {}
    for i, it in enumerate(items):
        if it['id'] in index: warnings.append(f"duplicate id {tag(it)}: dependencies point to the first one")
        else: index[it['id']] = i
    deps, unknown, skipped = [], [], {u['id']: u for u in unsized}
    for i, it in enumerate(items):
        refs = list(dict.fromkeys(it.get('depends') or []))
        missing = [r for r in refs if r not in index]
        if missing:
            unknown.append(i)
            if gone := [r for r in missing if r in skipped]:
                warnings.append(f"{tag(it)} depends on unsized {', '.join(tag(skipped[r]) for r in gone)}: kept in file order")
            if other := [r for r in missing if r not in skipped]:
                warnings.append(f"{tag(it)} depends on unknown {', '.join('#' + r for r in other)}: kept in file order")
        deps.append([index[r] for r in refs if r in index])
    pure = {i: r for r, i in enumerate(sorted(range(n), key=key))}
    waiting = [len(d) for d in deps]; dependents = defaultdict(list)
    for i, d in enumerate(deps):
        for x in d: dependents[x].append(i)

    def downstream(i):
        """Every item that needs `i` first, directly or through other items."""
        seen, stack = set(), list(dependents[i])
        while stack:
            j = stack.pop()
            if j not in seen: seen.add(j); stack += dependents[j]
        return seen
    ready = [key(i) for i in range(n) if not waiting[i] and i not in unknown]; heapq.heapify(ready)
    placed, out = {}, []
    while ready:
        i = heapq.heappop(ready)[-1]
        placed[i] = len(out)
        it = {**items[i], 'rank': len(out) + 1}
        if len(out) > pure[i] and deps[i]: it['waits_for'] = items[max(deps[i], key=placed.get)]['id']
        if up := [items[j]['id'] for j in sorted(downstream(i), key=pure.get) if pure[j] < pure[i] and j not in unknown]:
            it['unblocks'] = list(dict.fromkeys(up))
        out.append(it)
        for j in dependents[i]:
            waiting[j] -= 1
            if not waiting[j] and j not in unknown: heapq.heappush(ready, key(j))
    rest = [i for i in range(n) if i not in placed]
    stuck = [i for i in rest if i not in unknown]
    if stuck:
        warnings.append('dependency cycle (or waiting on an unresolved item): '
                        + ', '.join(tag(items[i]) for i in stuck) + ' kept in file order')
    out += [{**items[i], 'rank': len(out) + k + 1} for k, i in enumerate(rest)]
    return out, warnings


def simulate_backlog(items, cal, sims=10000, seed=1):
    """Two-level bootstrap over per-SP rates, and day-level resampling of merge pace.

    Each simulation first resamples the n historical rates (the rate distribution is itself only
    known from n PRs), then draws every item's rate from that resample. Drawing items independently
    from one fixed history lets per-item scatter cancel out over many items and claims more precision
    than n PRs support; summing per-item p20/p80 assumes every item is slow together. Items stay
    independent given the resample, so a shared slowdown beyond the history is still not covered.
    Below five outcomes use total-SP scaled min/max observed rates; resampling adds no evidence there.
    """
    if not isinstance(sims, int) or isinstance(sims, bool) or sims <= 0:
        raise ValueError('simulations must be a positive integer')
    if any(i.get('sp') not in FIB for i in items): raise ValueError(f'backlog SP must be one of {FIB}')
    rng = random.Random(seed); total_sp = sum(i['sp'] for i in items); sps = [i['sp'] for i in items]
    out = {'items': len(items), 'sp': total_sp, 'oversize': [i for i in items if i['sp'] > MAX_SP]}
    for m in SCALED:
        rs = cal['per_sp'].get(m) or []
        if not rs: continue
        if len(rs) < MIN_INTERVAL_N:
            q = {**spread([total_sp * x for x in rs]), 'basis': f'total SP × min–max per-SP rate of {len(rs)} PRs'}
        else:
            sums = []
            for _ in range(sims):
                hist = rng.choices(rs, k=len(rs))
                sums.append(sum(sp * x for sp, x in zip(sps, rng.choices(hist, k=len(sps)))))
            q = {'lo': percentile(sums, .2), 'mid': percentile(sums, .5), 'hi': percentile(sums, .8),
                 'n': len(rs), 'kind': 'p20-p80', 'basis': f'bootstrap of {len(rs)} PRs’ per-SP rates'}
        # Confidence from the per-item rate spread: a sum over many items is narrow by construction.
        lo, hi = percentile(rs, .2), percentile(rs, .8)
        q['confidence'] = confidence(len(rs), hi / lo if lo > 0 else None, 0,
                                     (cal.get('drift', {}).get(m) or {}).get('flag', False))
        if q['confidence'] == 'high': q['confidence'] = 'medium'  # assumes linear independent item rates
        out[m] = q
    tp = cal.get('throughput') or {}
    samples = [(sp, length) for sp, length in tp.get('samples') or [] if length > 0]
    if samples and sum(sp for sp, _ in samples) > 0 and total_sp > 0:
        need = []
        for _ in range(min(sims, 4000)):
            acc = d = 0
            while d < 3650:
                x, length = rng.choice(samples)
                if x and acc + x >= total_sp: d += length * (total_sp - acc) / x; break
                acc += x; d += length
            need.append(d)
        small = len(samples) < MIN_INTERVAL_N
        out['days'] = {'lo': min(need) if small else percentile(need, .2), 'mid': percentile(need, .5),
                       'hi': max(need) if small else percentile(need, .8), 'kind': 'min-max' if small else 'p20-p80',
                       'n': len(samples), 'full_days': tp.get('full_days', 0), 'sp_per_day': tp.get('sp_per_day'),
                       'prs': cal.get('prs', cal['n']), 'sp_merged': tp.get('sp'), 'span_days': tp.get('span_days'),
                       'merge_days': tp.get('merge_days'),
                       'confidence': 'low' if tp.get('full_days', 0) < 7 or cal['n'] < MIN_INTERVAL_N else 'medium'}
    return out


WAITS_DOMINATE = .1  # observed agent work under 10% of the forecast calendar time: waits, not work, set the days
SHORT_HISTORY_DAYS = 7  # merge history shorter than this: calendar days rest on too few days to trust


def short_history(days):
    """A warning when the merge history behind calendar days spans under SHORT_HISTORY_DAYS, else None."""
    span = (days or {}).get('span_days')
    if span is None or span >= SHORT_HISTORY_DAYS: return None
    n = max(math.ceil(span), 1)
    return (f"only {n} day{'s' if n != 1 else ''} of merge history: calendar days are unreliable "
            f'until about {SHORT_HISTORY_DAYS} days of merges are in')


def calendar_basis(sim, concurrency=1.0, one_agent=False):
    """(what calendar days and agent hours are based on, warning or None) for a backlog simulation.

    `concurrency` > 1 checks the one-agent days the dashboard shows (repo days × PRs in flight);
    `one_agent` says so in the warning, since the dashboard's day figure changes with the agent count.
    """
    d, h = sim.get('days'), sim.get('active_h')
    if not d: return None, None
    days_mid = d['mid'] * max(concurrency or 1.0, 1.0)
    pace = (f"{d['sp_per_day']:g} SP/day" if d.get('sp_per_day') is not None else 'under 1 day of pace')
    basis = (f"calendar days from merge pace: {d['sp_merged']} SP merged by {d['prs']} PRs over "
             f"{d['span_days']:g} d ({pace}; SP merged on {d['merge_days']} of {d['n']} days), "
             'so they include review, CI, owner and idle time')
    if h: basis += f"; agent hours from {h['basis']}, logged agent work only"
    if not h or days_mid <= 0: return basis, None
    busy = h['mid'] / (days_mid * 24)
    if busy >= WAITS_DOMINATE: return basis, None
    return basis, (f"Waits dominate: about {_num(h['mid'])} h of observed agent work spread over {_num(days_mid)} calendar days{' for one agent' if one_agent else ''} "
                   f"({busy:.0%} of the time, ≈{_num(h['mid'] / days_mid)} h/day). The days follow this repo’s merge pace, "
                   'not the work; they shrink only if reviews, merges and idle days do.')


def _prospective_estimate(row):
    """Only timestamps known to precede measured work can validate a prospective estimate."""
    if row.get('source') in ('retro', 'pr-body') or row.get('timing_known') is False: return False
    made = opt_utc(row.get('created_at')); start = opt_utc(row['actual'].get('start_at'))
    merged = opt_utc(row['actual'].get('merged_at'))
    return bool(made and start and merged and made <= start <= merged and made < merged)


def _known_at(row):
    # A body may have been edited after PR creation; created_at is not proof of its first observation.
    return opt_utc(row.get('observed_at') if row.get('source') == 'pr-body' else row.get('created_at'))


def _backtest_population(done, targets, window_days, tz, since, retro=False):
    res = {'n': 0, 'skipped': 0, 'metrics': {}, 'note':
           ('retrospective diagnostic only: final-diff sizing is not a prospective accuracy claim; ' if retro else '')
           + 'a calibrated p20-p80 interval holds about 60% of actuals; '
           'the SP step earns its keep only if mdape beats mdape_baseline_last10'}
    per = defaultdict(lambda: {'hit': 0, 'ape': [], 'ape_base': [], 'pairs': []})
    for t in sorted(one_per_pr(targets), key=lambda r: r['actual']['merged_at']):
        t0 = utc(t['actual']['start_at']) if retro else utc(t['created_at'])
        if since and t0 <= since: res['skipped'] += 1; continue
        # Exclude every estimate on the target PR. Historical body/retro estimates can train a
        # prospective quote only after they were actually observed; retrospective diagnostics
        # alone may pretend blind retro sizes were available at the historical merge.
        hist = [r for r in done if r['actual']['pr'] != t['actual']['pr']
                and utc(r['actual']['merged_at']) < t0
                and ((_known_at(r) is not None and _known_at(r) < t0)
                     or (retro and r.get('source') == 'retro'))]
        cal = calibrate(hist, t0, window_days, since=since, tz=tz)
        if cal['n'] < MIN_INTERVAL_N: res['skipped'] += 1; continue
        q = quote(cal, t['sp'], t0, estimator_key(t)); res['n'] += 1
        lo = max(t0 - timedelta(days=window_days), since) if since else t0 - timedelta(days=window_days)
        recent = sorted(one_per_pr([r for r in hist if utc(r['actual']['merged_at']) >= lo]),
                        key=lambda r: r['actual']['merged_at'])[-10:]
        for m in SCALED:
            actual = metric_value(t['actual'], m); qm = q.get(m)
            if actual is None or not qm or actual <= 0 or qm['n'] < MIN_INTERVAL_N: continue
            p = per[m]; p['hit'] += qm['lo'] <= actual <= qm['hi']; p['ape'].append(abs(qm['mid'] - actual) / actual)
            base = [v for r in recent if (v := metric_value(r['actual'], m)) is not None]
            if base:
                p['ape_base'].append(abs(statistics.median(base) - actual) / actual)
                p['pairs'].append((p['ape'][-1], p['ape_base'][-1]))  # same target: a fair comparison
    for m, p in per.items():
        k = len(p['ape'])
        res['metrics'][m] = {'n': k, 'interval_hit_rate': round(p['hit'] / k, 3) if k else None,
                             'mdape': round(statistics.median(p['ape']), 3) if k else None,
                             'mdape_baseline_last10': round(statistics.median(p['ape_base']), 3) if p['ape_base'] else None,
                             'paired': {'n': len(p['pairs']),
                                        'mdape': round(statistics.median(a for a, _ in p['pairs']), 3) if p['pairs'] else None,
                                        'mdape_baseline_last10': round(statistics.median(b for _, b in p['pairs']), 3) if p['pairs'] else None}}
    return res


def backtest(rows, window_days=14.0, include_retro=False, tz=None, since=None):
    """Leave-future-out prospective accuracy, one target and baseline sample per PR.

    Target estimates must precede actual.start_at; body estimates with unknown timing cannot be
    targets. Retrospectives are separate diagnostics and may train a prospective forecast only
    once logged. `since` clips both target outcomes and each historical training window.
    """
    all_done = settled(rows)
    if since: all_done = [r for r in all_done if utc(r['actual']['merged_at']) >= since]
    done = [r for r in all_done if _prospective_estimate(r) or r.get('source') in ('retro', 'pr-body')]
    targets = [r for r in done if _prospective_estimate(r)]
    res = _backtest_population(done, targets, window_days, tz, since)
    res['excluded'] = len(all_done) - len(targets)
    if include_retro:
        retros = [r for r in done if r.get('source') == 'retro' and opt_utc(r['actual'].get('start_at'))]
        res['retro'] = _backtest_population(done, retros, window_days, tz, since, retro=True)
    return res


def refs(rows, sp=None, paths=(), limit=5):
    """Optional anchors: settled PRs ranked by shared top-level paths, then by SP closeness."""
    want = set(areas([{'path': p} for p in paths], limit=50)) if paths else set()
    scored = []
    for r in settled(rows):
        have = set(r['actual'].get('areas') or [])
        score = len(want & have) / len(want | have) if want and have else 0
        if sp in FIB and r['sp'] in FIB: score += max(0, .5 - .25 * abs(FIB.index(r['sp']) - FIB.index(sp)))
        scored.append((score, r['actual']['merged_at'], r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored[:limit]]


def project_summary(rows, label):
    """Process traits of one project over all settled PRs, each PR counted once."""
    done = one_per_pr(settled(rows))
    med = lambda xs: round(statistics.median(xs), 3) if xs else None
    def metric(xs): return {'median': med(xs), 'n': len(xs)}
    waits = [(w - a) / w for r in done if (w := r['actual'].get('wall_h')) and (a := r['actual'].get('active_h')) is not None]
    reviews = [v for r in done if (v := r['actual'].get('review_rounds')) is not None]
    ex = Counter(r['actual'].get('executor') or 'unknown' for r in done)
    merged = sorted(r['actual']['merged_at'] for r in done)
    return {'project': label, 'prs': len(done), 'first_merge': merged[0] if merged else None,
            'last_merge': merged[-1] if merged else None,
            'active_h_per_sp': metric(_rates(done, 'active_h')), 'wall_h_per_sp': metric(_rates(done, 'wall_h')),
            'wait_share': metric([max(x, 0) for x in waits]), 'usd_per_sp': metric(_rates(done, 'usd')),
            'review_rounds': metric(reviews),
            'executors': {k: round(v / len(done), 3) for k, v in ex.most_common()} if done else {}}


def compare_text(summaries):
    """One plain-text table per project."""
    def val(m, f):
        return '–' if m['median'] is None else f'{f(m["median"])} (n={m["n"]})'
    out = []
    for s in summaries:
        span = f", merged {s['first_merge'][:10]} → {s['last_merge'][:10]}" if s['prs'] else ''
        mix = ', '.join(f'{k} {v:.0%}' for k, v in list(s['executors'].items())[:3]) or '–'
        if len(s['executors']) > 3: mix += f', other {sum(list(s["executors"].values())[3:]):.0%}'
        lines = [('PRs', str(s['prs'])),
                 ('active h / SP (≈)', val(s['active_h_per_sp'], lambda v: f'{v:.2f}')),
                 ('wall h / SP', val(s['wall_h_per_sp'], lambda v: f'{v:.2f}')),
                 ('wait share (wall − active) / wall', val(s['wait_share'], lambda v: f'{v:.0%}')),
                 ('$ / SP', val(s['usd_per_sp'], lambda v: f'${v:.2f}')),
                 ('review rounds', val(s['review_rounds'], lambda v: f'{v:g}')),
                 ('executor mix', mix)]
        w = max(len(k) for k, _ in lines)
        out += [f"{s['project']}{span}", *(f'  {k.ljust(w)}  {v}' for k, v in lines), '']
    out.append('Medians over all settled PRs, each PR once. Wait share is unobserved time (waits and work without '
               'nearby logs). $ and active h are PR-branch work only.')
    return '\n'.join(out)


def project_label(store):
    """The repo a data directory belongs to: retro packet or config, else the directory name."""
    repo = (store.read_json('retro-packet.json') or {}).get('repo') or (store.read_json('config.json') or {}).get('repo')
    if repo: return repo
    d = store.dir.resolve()
    return d.parent.name if d.name == '.estimate' else d.name


# ---------- backlog sources ----------

BACKLOG_SOURCES = ('markdown', 'tiden')
CONFIGURED = object()                          # `forecast --backlog` without FILE: use config.json
PULLED_BACKLOG = 'backlog.md'                  # Tiden cards land here, inside the data directory
TIDEN_OPEN = ('Backlog', 'Review')            # default open statuses. Override: backlog.statuses
TIDEN_DELIVERED = ('Active', 'Done')
TIDEN_PAGE = 100


def git_root():
    try: return subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError): return None


def backlog_config(store):
    """`backlog` in config.json: {"source": "markdown", "path": ...} or
    {"source": "tiden", "product_id": ..., "statuses": ["Backlog", "Review"]}.

    Missing file or key: Markdown `BACKLOG.md` at the git root; a relative path is resolved from the git root.
    A Tiden backlog is read from the table `estimate backlog pull` writes into the data directory; `statuses`
    (default ["Backlog", "Review"]) picks the cards it pulls; delivered statuses (Active, Done) are not allowed.
    """
    shape = ('expected {"backlog": {"source": "markdown", "path": "BACKLOG.md"}} or '
             '{"backlog": {"source": "tiden", "product_id": "<id>"}}')
    try: top = store.read_json('config.json')
    except ValueError: raise ValueError(f'{store.dir / "config.json"} is not valid JSON; {shape}') from None
    cfg = top.get('backlog') if isinstance(top, dict) else top
    if cfg is None: cfg = {}
    bad = [k for k in ('source', 'path', 'product_id') if isinstance(cfg, dict) and cfg.get(k) is not None and not isinstance(cfg[k], str)]
    if (top is not None and not isinstance(top, dict)) or not isinstance(cfg, dict) or bad:
        raise ValueError(f'{store.dir / "config.json"}: {shape}' + (f'; {", ".join(bad)} must be a string' if bad else ''))
    source = cfg.get('source') or 'markdown'
    if source not in BACKLOG_SOURCES: raise ValueError(f'config.json: backlog source must be one of {BACKLOG_SOURCES}, not {source!r}')
    if source == 'tiden':
        statuses = cfg.get('statuses', list(TIDEN_OPEN))
        if (not isinstance(statuses, list) or not statuses or not all(isinstance(x, str) and x for x in statuses)
                or any(x in TIDEN_DELIVERED for x in statuses)):
            raise ValueError('config.json: backlog.statuses must be a non-empty list of Tiden statuses such as '
                             f'["Backlog", "Review"], without {" or ".join(TIDEN_DELIVERED)}; got {statuses!r}')
        return {'source': source, 'product_id': cfg.get('product_id') or None, 'path': store.dir / PULLED_BACKLOG,
                'statuses': tuple(dict.fromkeys(statuses))}
    path = Path(cfg.get('path') or 'BACKLOG.md').expanduser()
    return {'source': source, 'path': path if path.is_absolute() else Path(git_root() or '.') / path}


def _table_rows(text):
    """(column names, [(id, fields)]) of an existing backlog table with an id column, every row in file order,
    duplicates included. `fields`: effort/value/depends/key cells, unvalidated (empty cells included), plus
    `row`: every cell by its column (title, status, extra columns...), to write a hand-written row back as it is."""
    lines = [raw.strip() for raw in text.splitlines()]
    start = next((n for n, line in enumerate(lines) if _header(line)), None)
    cols = _header(lines[start]) if start is not None else None
    if not cols or cols['id'] is None: return [], []
    names, out = [n.lower() for n in _cells(lines[start])], []
    for line in lines[start + 1:]:
        if not line.startswith('|'): break
        cells = _cells(line)
        if all(re.fullmatch(r':?-+:?', c) for c in cells if c): continue
        get = lambda k: cells[cols[k]] if cols[k] is not None and cols[k] < len(cells) else ''
        row = {**dict(zip(names, cells)), **{k: get(k) for k in ('title', 'key', 'note')}}
        out.append((get('id').lstrip('#'), {'effort': get('sp'), 'value': get('value'), 'depends': get('depends'),
                                             'key': get('key'), 'row': row}))
    return names, out


def _local_fields(text):
    """id -> fields (see `_table_rows`) of an existing backlog table; the first of duplicate ids wins, as in the parser."""
    out = {}
    for i, fields in _table_rows(text)[1]: out.setdefault(i, fields)
    return out


def tiden_key(spec):
    """A Tiden card's human key: a `key`/`humanKey`/`displayKey` field, else a `KEY-21`-style title prefix."""
    for k in ('key', 'humanKey', 'displayKey'):
        if isinstance(spec.get(k), str) and spec[k].strip(): return spec[k].strip()
    m = re.match(r'\s*([A-Z][A-Z0-9]*-\d+)\b', spec.get('title') or '')
    return m.group(1) if m else ''


def is_container(spec, parents=()):
    """A Tiden card with child cards (`childrenCount` > 0, or the parent of a listed card): its children are the work."""
    n = spec.get('childrenCount')
    return spec.get('id') in parents or (isinstance(n, int) and not isinstance(n, bool) and n > 0)


def delivery_note(spec):
    """`maybe-done: 1 PR, 4 code anchors` when Tiden links a pull request or repository code to an open card
    (sources from `--include-sources`), else ''. Tiden statuses lag merges, so such a card may be delivered."""
    kinds = Counter(x.get('sourceType') for x in spec.get('sources') or [] if isinstance(x, dict))
    parts = [f"{n} {word}{'s' if n != 1 else ''}" for n, word in ((kinds['pull_request'], 'PR'), (kinds['repo_file'], 'code anchor')) if n]
    return 'maybe-done: ' + ', '.join(parts) if parts else ''


def maybe_done(item):
    return (item.get('note') or '').lower().startswith('maybe-done')


def depends_refs(text, code=None):
    """Card refs on a card body's `Depends on` lines: Tiden UUIDs anywhere on the line, and keys such as
    KEY-44 or product sequence refs such as ACME-225 (product code, dash, seqNum) up to the end of the
    first sentence, so `Depends on: none for code. Live evidence needs ACME-237.` names nothing."""
    out = []
    for line in re.findall(r'(?im)^.*\bdepends on\b.*$', text or ''):
        out += re.findall(_UUID, line.lower())
        clause = re.split(r'(?i)\bdepends on\b', line, maxsplit=1)[1]
        out += re.findall(r'\b[A-Z][A-Z0-9]*-\d+\b', re.split(r'\.(?:\s|$)', clause, maxsplit=1)[0])
    return list(dict.fromkeys(out))


def tiden_cards(specs, statuses=TIDEN_OPEN, code=None):
    """Open work cards from `tiden spec list --format json` (the `requirements` list).

    Open: status in `statuses` (default Backlog), and not a container (a card with child cards: its
    children are the work, so its size would be counted twice). Effort and Value come from the first
    `Effort: N` / `Value: N` in the content when N is on the scale (`Value: 5. Effort: 3. Score: 1.67.`);
    Depends from `Depends on` lines naming other open cards (`depends_refs`: a Tiden id, a key, or the
    product `code` with a seqNum), written as their label (key or 8-char id prefix). Priority is shown, never turned into Value. `note` flags a card Tiden links to
    merged code while its status is still open (`delivery_note`).
    """
    opened = [s for s in specs if isinstance(s, dict) and s.get('status') in statuses and s.get('id')]
    parents = {s.get('parentId') for s in opened}
    cards = [s for s in opened if not is_container(s, parents)]
    ids = {s['id'] for s in cards}
    labels, _ = id_aliases([(s['id'], tiden_key(s)) for s in cards])
    named = {tiden_key(s): s['id'] for s in cards if tiden_key(s)}
    if code: named.update({f"{code}-{s['seqNum']}": s['id'] for s in cards if isinstance(s.get('seqNum'), int)})

    def number(text, label, scale):
        m = re.search(rf'(?i)\b{label}\s*:\s*(\d+)\b', text)
        return m.group(1) if m and int(m.group(1)) in scale else ''
    out = []
    for s in cards:
        text = s.get('content') or ''
        refs = [named.get(r, r) for r in depends_refs(text, code)]
        out.append({'id': s['id'], 'key': tiden_key(s), 'title': s.get('title') or '', 'effort': number(text, 'effort', FIB),
                    'value': number(text, 'value', VALUES),
                    'depends': ', '.join(labels[r] for r in dict.fromkeys(refs) if r in ids and r != s['id']),
                    'depends_ids': [r for r in dict.fromkeys(refs) if r in ids and r != s['id']],
                    'status': s['status'], 'priority': s.get('priority') or '', 'type': s.get('type') or '',
                    'note': delivery_note(s)})
    return out


def run_tiden(product_id=None, cwd=None, page_size=TIDEN_PAGE):
    """Every spec via read-only `tiden spec list --format json --include-sources`, page by page
    (`pagination.nextPageToken`). Sources (linked PRs, code anchors) flag open cards that may be delivered;
    a tiden without `--include-sources` is asked again without it.

    Without a product id, tiden uses the repo it is linked to. Any failed or short page fails the whole
    listing, so a partial list can never make unseen cards look closed.
    """
    base = ['tiden', 'spec', 'list', '--format', 'json', '--include-sources', '--page-size', str(page_size)] + \
        (['--product-id', product_id] if product_id else [])
    specs, token, seen, total = [], None, set(), None
    while True:
        cmd = base + (['--page-token', token] if token else [])
        try: r = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd, timeout=300)
        except FileNotFoundError: raise RuntimeError('the Tiden CLI `tiden` is not installed or not on PATH') from None
        except subprocess.TimeoutExpired: raise RuntimeError('`tiden spec list` timed out') from None
        if r.returncode and not specs and '--include-sources' in base and 'include-sources' in (r.stderr or r.stdout or ''):
            base.remove('--include-sources'); continue
        if r.returncode:
            msg = (r.stderr or r.stdout or '').strip()[:400] or f'exit code {r.returncode}'
            raise RuntimeError(f'`tiden spec list` failed: {msg}\n  run `tiden setup` to authorize and link this repo, '
                               'or set backlog.product_id in .estimate/config.json')
        try: data = json.loads(r.stdout)
        except ValueError: raise RuntimeError('`tiden spec list` did not return JSON') from None
        page = data.get('requirements') if isinstance(data, dict) else None
        if not isinstance(page, list): raise RuntimeError('`tiden spec list` JSON has no "requirements" list')
        specs += page
        pagination = data.get('pagination') if isinstance(data.get('pagination'), dict) else {}
        if isinstance(pagination.get('totalCount'), int): total = pagination['totalCount']
        token = pagination.get('nextPageToken') or None
        if not token: break
        if token in seen: raise RuntimeError(f'`tiden spec list` repeated page token {token!r}')
        seen.add(token)
    if total is not None and len(specs) != total:
        raise RuntimeError(f'`tiden spec list` returned {len(specs)} of {total} specs; nothing written, try again')
    return specs


def tiden_product_name(product_id, specs, cwd=None):
    """The product's name from read-only `tiden product list`, for the dashboard's source line; None on any failure."""
    return tiden_product(product_id, specs, cwd)['name']


def tiden_product(product_id, specs, cwd=None):
    """{'name', 'code'} of the product from read-only `tiden product list`; each None on any failure. The code
    (e.g. ACME) prefixes the product's sequence refs in card text, such as ACME-225."""
    none = {'name': None, 'code': None}
    pid = product_id or next((s.get('productId') for s in specs if isinstance(s, dict) and s.get('productId')), None)
    if not pid: return none
    try:
        r = subprocess.run(['tiden', 'product', 'list', '--format', 'json'], capture_output=True, text=True, check=True, cwd=cwd, timeout=60)
        data = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError): return none
    products = data.get('products') if isinstance(data, dict) else data
    p = next((p for p in products if isinstance(p, dict) and p.get('id') == pid), {}) if isinstance(products, list) else {}
    name, code = p.get('name'), p.get('code')
    return {'name': ' '.join(name.split()) if isinstance(name, str) and name.strip() else None,
            'code': code.strip() if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9]*', code.strip()) else None}


def _short_list(names, limit=8):
    return ', '.join(names[:limit]) + (f' +{len(names) - limit} more' if len(names) > limit else '')


def pulled_source(text):
    """(product name or None, pulled-at datetime or None) from the header `estimate backlog pull` writes."""
    name = re.search(r'(?m)^# Backlog from Tiden product (.+) \([^()]+\)[ \t]*$', text)
    when = re.search(r'(?m)^Pulled (\S+) by `estimate backlog pull`', text)
    return (name.group(1).strip() if name else None), (opt_utc(when.group(1)) if when else None)


def pull_tiden(specs, existing, product_id, now, statuses=TIDEN_OPEN, product_name=None, code=None):
    """(Markdown backlog text, notes). A row already in the local table keeps its effort/value/depends
    cells as they are, empty ones included; card values fill only new rows. Cards no longer open (status
    not in `statuses`) are dropped; refs (id, key or 8-char prefix) to delivered (Active/Done) specs leave
    the depends cells, unknown refs stay for the warning. A local ref is resolved against the old table
    first (its ids and keys), so a renamed key or a newly shared prefix cannot orphan the edge; refs to
    open cards are then rewritten as their current label. Only rows imported from Tiden (a UUID id) are
    ever dropped: a hand-written row (any other id, e.g. `1`) is kept as it is, after the cards."""
    names_old, table = _table_rows(existing) if existing else ([], [])
    local = {}
    for i, fields in table: local.setdefault(i, fields)
    rows = {i: f['row'] for i, f in local.items()}
    _, old_resolve = id_aliases([(i, f['key']) for i, f in local.items()])
    cards = tiden_cards(specs, statuses, code)
    every = [s for s in specs if isinstance(s, dict) and s.get('id')]
    names, resolve = id_aliases([(s['id'], tiden_key(s)) for s in every])
    labels, _ = id_aliases([(c['id'], c['key']) for c in cards])
    status = {s['id']: s.get('status') for s in every}
    delivered = {i for i, st in status.items() if st in TIDEN_DELIVERED}
    taken = {c['id'] for c in cards}
    # Every hand-written row (an id that is not a Tiden UUID or spec id), duplicates included, as written.
    manual = [{**f['row'], **{k: v for k, v in f.items() if k != 'row'}, 'id': i} for i, f in table
              if i not in taken and i not in status and not re.fullmatch(_UUID, i.lower())]
    own = {m['id'] for m in manual}
    for c in cards + manual:
        mine = c['id'] in local
        if mine and c['id'] in taken:
            # An empty depends cell takes the card's `Depends on` line; a hand-filled one (`-` for none) stays.
            c.update({k: v for k, v in local[c['id']].items() if k not in ('row', 'key') and (k != 'depends' or v.strip())})
        # Refs from the card's text are current Tiden ids; only refs kept from the old table resolve against its aliases.
        fresh = c.pop('depends_ids', None) if c['id'] in taken and not (mine and local[c['id']]['depends'].strip()) else None
        c.pop('depends_ids', None)
        out = []
        for d in fresh if fresh is not None else _depends(c['depends']):
            full = d if fresh is not None else old_resolve(d) if mine and old_resolve(d) in local else resolve(d)
            if full in delivered: continue
            label = labels.get(full, full if full in status else d)
            # A label that is also a hand-written row's id would point at that row: write the full id instead.
            out.append(full if label in own and label != full and full in status else label)
        c['depends'] = ', '.join(out) or ('-' if c['depends'].strip() in ('-', '–', '—') else '')  # `-`: none, by hand
    ids, notes = {c['id'] for c in cards}, []
    opened = {s['id'] for s in every if s.get('status') in statuses}
    parents = {s.get('parentId') for s in every if s['id'] in opened}
    containers = [s['id'] for s in every if s['id'] in opened and is_container(s, parents)]
    kept = [m['id'] for m in manual]
    gone = [i for i in local if i not in ids and i not in own]
    done = [i for i in gone if i in delivered]
    other = [i for i in gone if i not in done and i in status and i not in containers]
    missing = [i for i in gone if i not in status]
    if done: notes.append(f"dropped {len(done)} delivered card(s) (Active/Done), also removed from depends: {', '.join(names.get(i, i) for i in done)}")
    if other: notes.append(f"dropped {len(other)} card(s) not in status {' or '.join(statuses)}: {', '.join(names.get(i, i) for i in other)}")
    if missing: notes.append(f"dropped {len(missing)} card(s) no longer in Tiden: {', '.join(missing)}")
    if containers:
        notes.append(f"left out {len(containers)} container card(s) with child cards, never sized or counted "
                     f"(their children are the work): {_short_list([names.get(i, i) for i in containers])}")
    if flagged := [c for c in cards if c['note']]:
        notes.append(f"{len(flagged)} open card(s) marked maybe-done: Tiden links merged code, but the status is still open; "
                     f"check and close them in Tiden: {_short_list([labels[c['id']] for c in flagged])}")
    if kept: notes.append(f"kept {len(kept)} hand-written row(s) without a Tiden id: {_short_list(kept)}")
    added = sum(c['id'] not in local for c in cards)
    if local and added: notes.append(f'added {added} new card(s)')
    cols = ('id', 'key', 'title', 'effort', 'value', 'depends') + (('lane',) if 'lane' in names_old else ()) + \
        ('status', 'priority', 'type', 'note')
    known = set(cols) | {a for aliases in _COLUMNS.values() for a in aliases}
    cols += tuple(n for n in dict.fromkeys(names_old) if n and n not in known)  # columns added by hand keep their cells
    for c in cards: c.update({k: rows.get(c['id'], {}).get(k, '') for k in cols if k not in c})
    cell = lambda s: re.sub(r'\s*[\r\n]+\s*', ' ', str(s).strip()).replace('|', '\\|')  # one line; inner spaces kept
    pid = product_id or next((s.get('productId') for s in every if s.get('productId')), None)
    title = f' product {product_name} ({pid})' if product_name and pid else (f' product {product_id}' if product_id else '')
    text = (f"# Backlog from Tiden{title}\n\n"
            f"Pulled {iso(now)} by `estimate backlog pull` (open cards: status {' or '.join(statuses)}). Fill effort,\n"
            'value and depends; a re-pull keeps them for the same id and keeps rows you add with your own id (e.g. 1).\n'
            'Nothing here is written back to Tiden.\n'
            f'Depends may name an id, its key, or its first {SHORT_ID} characters. Note `maybe-done`: Tiden links merged\n'
            'code to the card while its status is still open; it is counted until it is closed in Tiden.\n'
            + (f'Left out {len(containers)} container card(s) (their child cards are the work).\n' if containers else '') + '\n'
            f"| {' | '.join(cols)} |\n|{'|'.join('-' * (len(k) + 2) for k in cols)}|\n"
            + ''.join(f"| {' | '.join(cell(c.get(k, '')) for k in cols)} |\n" for c in cards + manual)
            + (f'\n{after}' if (after := after_table(existing)) else ''))
    return text, notes


def after_table(text):
    """Everything after the backlog table (`## Lanes`, notes), kept by a re-pull; '' when there is none."""
    lines = (text or '').splitlines(keepends=True)
    start = next((n for n, line in enumerate(lines) if _header(line.strip())), None)
    if start is None: return ''
    end = next((n for n in range(start + 1, len(lines)) if not lines[n].strip().startswith('|')), len(lines))
    return ''.join(lines[end:]).lstrip('\n')


# ---------- dashboard ----------

PARALLEL = (1, 2, 3, 5)  # agent counts offered by the planner
TOP_ROWS = 12            # backlog rows shown before "Show all"
TIERS = (('sure', 'hi'), ('likely', 'mid'), ('maybe', 'lo'))  # waterline -> the h/SP or days quantile behind it
BURN_DAYS = 7            # the burn row's longer period


def _num(v):
    return f'{v:.0f}' if abs(v) >= 10 or v == int(v) else (f'{v:.1f}' if abs(v) >= 1 else f'{v:.2g}')


def critical_path(items):
    """The heaviest chain of written dependencies, in SP: {'sp', 'chain', 'cycle', 'unknown'}.

    `chain` lists ids first to last. Unknown ids are ignored (listed in `unknown` as (id, missing));
    items in or behind a dependency cycle are left out of the chain and `cycle` names one cycle
    (ids, first repeated at the end). Iterative topological order; duplicate ids resolve to the first item.
    """
    index = {}
    for i, it in enumerate(items): index.setdefault(it['id'], i)
    deps, unknown = [], []
    for it in items:
        refs = list(dict.fromkeys(it.get('depends') or []))
        missing = [r for r in refs if r not in index]
        if missing: unknown.append((it['id'], missing))
        deps.append([index[r] for r in refs if r in index])
    waiting = [len(d) for d in deps]; dependents = defaultdict(list)
    for i, d in enumerate(deps):
        for j in d: dependents[j].append(i)
    best = {}; queue = [i for i in range(len(items)) if not waiting[i]]
    while queue:
        i = queue.pop()
        prev = max((best[j] for j in deps[i]), key=lambda t: t[0], default=(0, []))
        best[i] = (prev[0] + items[i]['sp'], prev[1] + [items[i]['id']])
        for k in dependents[i]:
            waiting[k] -= 1
            if not waiting[k]: queue.append(k)
    cycle = []
    rest = [i for i in range(len(items)) if i not in best]
    if rest:  # every unresolved item depends on another unresolved one, so walking dependencies must repeat
        seen, i = {}, rest[0]
        while i not in seen:
            seen[i] = len(seen); i = next(j for j in deps[i] if j not in best)
        loop = [k for k, _ in sorted(seen.items(), key=lambda kv: kv[1])][seen[i]:]
        cycle = [items[k]['id'] for k in reversed(loop)] + [items[loop[-1]]['id']]
    sp, chain = max(best.values(), key=lambda t: t[0], default=(0, []))
    return {'sp': sp, 'chain': chain, 'cycle': cycle, 'unknown': unknown}


def union_hours(spans):
    """Hours covered by at least one of the sorted (start, end) spans."""
    union = 0.0; cur = None
    for s, e in spans:
        if cur and s <= cur[1]: cur = (cur[0], max(cur[1], e))
        else:
            if cur: union += hours(*cur)
            cur = (s, e)
    return union + (hours(*cur) if cur else 0.0)


def observed_concurrency(rows, cal):
    """Average PRs in flight in the window: summed PR wall time / union of the PRs' start→merge spans.

    The repo's SP/day already includes this parallelism, so one agent's pace is SP/day ÷ concurrency.
    Wall spans include review and CI waits; the figure describes overlap, not proven simultaneous work.
    """
    if not cal: return None
    a, b = utc(cal['window']['start']), utc(cal['window']['end'])
    spans = sorted((utc(r['actual']['start_at']), utc(r['actual']['merged_at'])) for r in one_per_pr(settled(rows))
                   if r['actual'].get('start_at') and a <= utc(r['actual']['merged_at']) <= b)
    total = sum(hours(s, e) for s, e in spans); union = union_hours(spans)
    return {'value': max(total / union, 1.0), 'prs': len(spans)} if union > 0 else None


def parallel_days(days, agents, total_sp, path_sp, concurrency=1.0):
    """Days for `agents` at once, from repo calendar days whose pace already had `concurrency` PRs in flight.

    One agent: repo days × concurrency (work ÷ per-agent pace). More agents: one-agent days ÷ agents,
    never below the critical path's share. That ideal split assumes perfect hand-offs and no waits, so
    for more than one agent it is an optimistic lower bound, not a completion forecast.
    """
    if not days or total_sp <= 0: return None
    share = min(path_sp / total_sp, 1); one = {k: days[k] * max(concurrency or 1.0, 1.0) for k in ('lo', 'mid', 'hi')}
    out = {k: max(one[k] / agents, one[k] * share) for k in one}
    out['limited'] = agents > 1 and one['mid'] / agents < one['mid'] * share
    return out


ACTIVE_LABEL = 'agent active time, summed (one agent working serially; not wall clock)'


def wall_clock_lines(items, days, concurrency=None):
    """Calendar days with 1, 2, 3 and 5 agents in parallel (`parallel_days`): never below the critical path."""
    cp = critical_path(items); total = sum(i['sp'] for i in items)
    if not days or total <= 0: return []
    if cp['cycle']: return ['wall clock: dependencies form a loop (' + ' → '.join('#' + x for x in cp['cycle']) + '); remove one link to plan']
    c = (concurrency or {}).get('value') or 1.0; out = []
    for a in PARALLEL:
        pd = parallel_days(days, a, total, cp['sp'], c)
        out.append(f"wall clock, {_agents(a) + ':':<10}{_num(pd['lo'])}–{_num(pd['hi'])} d (median {_num(pd['mid'])} d; {days['kind']}"
                   + (f"; the {cp['sp']} SP dependency chain sets the floor" if pd['limited'] else '') + ')')
    return out + ['  2+ agents assume perfect hand-offs and no extra waits: a lower bound, not a promise']


def _when(d):
    return f'{d.day} {d:%b %H:%M} UTC'


def _tok(v):
    if v >= 1e9: return f'{v / 1e9:.1f} B'
    if v >= 1e6: return f'{v / 1e6:.1f} M' if v < 1e7 else f'{v / 1e6:.0f} M'
    return f'{v / 1e3:.0f} k' if v >= 1e3 else f'{v:.0f}'


def _usd(v):
    return f'${v:,.2f}' if v < 1 else f'${v:.1f}' if v < 10 else f'${v:,.0f}'


def _hrs(h):
    return f'{max(round(h * 60), 1)} min' if h < 1 else f'{h:.1f} h' if h < 10 else f'{h:.0f} h'


# ---------- burn: every local agent response, any repo ----------

_SCAN_CACHE = None   # a dict while `run_all` runs: one log scan feeds every project


def scan_burn(claude_root, codex_root, since, prices, limits=None, meta=None, labels=True):
    """API-equivalent burn since `since` from local Claude Code transcripts and Codex sessions, every repo.

    One record per response: (ts, agent, model, tokens, usd or None when the model has no price, streams, key).
    Claude: deduplicated by message id and request id across files (streamed snapshots and resumed sessions
    repeat them; the largest usage wins). Codex: a `token_usage_record` counts once per response id. The
    `token_count` next to it (before or after) that reports the same response is its echo: same usage, or same
    cumulative total (`thread_token_usage`); every copy of that echo in other files is dropped too. A distinct
    response with equal usage right next to a record without its own report is indistinguishable and counts
    once. Any other `token_count` counts its `last_token_usage` once its cumulative total changes, deduplicated
    across files by timestamp and total. `streams` lists every session a response appears in (a resumed or
    forked session copies history) and `key` identifies the response, so agent hours count a copied stretch
    once. Lines are filtered by a byte search before parsing; only timestamps, model names and token counts are
    kept, never message text. With a `limits` list, the newest Codex `rate_limits` numbers (`codex_limits`)
    replace its contents. With a `meta` dict, each session (`session_key` of a stream) gets its cwd, Codex git
    remote (owner/name), pr-link repos and, with `labels`, a short first-line label (`prompt_label`) and nothing
    more of it; without `labels` no user line is decoded. A record also carries its normalized usage and its home
    stream: of the sessions it appears in (a fork or resume copies history), the one that began first. Inside
    `run_all` the scan is shared between projects (same logs, `since`, `labels`); only the prices are applied again.
    """
    # One decision per run: labels are read when any project shows them and stay in memory; a project that hides
    # labels still hides them in its report (`usage_report(labels=False)`), so mixed settings do not cost a second scan.
    if _SCAN_CACHE is not None: labels = _SCAN_CACHE.get('labels', labels)
    key = (str(Path(claude_root).expanduser()), str(Path(codex_root).expanduser()), since, labels, meta is not None, limits is not None)
    if _SCAN_CACHE is not None and key in _SCAN_CACHE:
        cands, seen, opened, cached_meta, cached_limits = _SCAN_CACHE[key]
        if meta is not None: meta.update(cached_meta)
        if limits is not None: limits[:] = cached_limits
    else:
        cands, seen, opened = _scan_burn_logs(claude_root, codex_root, since, limits, meta, labels)
        if _SCAN_CACHE is not None: _SCAN_CACHE[key] = (cands, seen, opened, dict(meta or {}), list(limits or []))
    return _price_burn(cands, seen, opened, prices)


def _scan_burn_logs(claude_root, codex_root, since, limits, meta, labels):
    """The price-free part of `scan_burn`: (cands, seen, opened) from the logs."""
    cands = {}; seen = defaultdict(set); echoes = {}   # echoes: report key -> the record key it repeats

    opened = {}   # stream -> when its session began: a copied response belongs to the session that began first

    def add(key, rec):
        seen[key].add(rec[5])
        if rec[5] not in opened or rec[0] < opened[rec[5]]: opened[rec[5]] = rec[0]
        if key not in cands or rec[3] > cands[key][3]: cands[key] = rec
    root = Path(claude_root).expanduser()
    for f in sorted(root.rglob('*.jsonl')) if root.is_dir() else ():
        if not _mtime_ok(f, since): continue
        try: fh = f.open('rb')
        except OSError: continue
        with fh:
            # A subagent's prompt is not its session's label; user lines are parsed only until their session has one.
            need_label = meta is not None and labels and 'subagents' not in f.relative_to(root).parts
            for n, line in enumerate(fh):
                # A user line (a prompt or a tool result) is decoded only to label its session, never otherwise.
                if (b'"type":"user"' in line or b'"type": "user"' in line) and b'"type":"assistant"' not in line \
                        and b'"type": "assistant"' not in line:
                    if not need_label or _labelled(meta, line, f): continue
                elif b'"usage"' not in line and not (meta is not None and b'"pr-link"' in line): continue
                try: o = json.loads(line)
                except ValueError: continue
                if meta is not None and isinstance(o, dict):
                    s = _session_meta(meta, f"claude:{o.get('sessionId') or f}", 'claude-code')
                    if not s['cwd'] and isinstance(o.get('cwd'), str): s['cwd'] = o['cwd']
                    s['dir'] = s['dir'] or str(root / f.relative_to(root).parts[0])
                    if o.get('type') == 'pr-link' and o.get('prRepository'): s['repos'].add(str(o['prRepository']))
                    if need_label and o.get('type') == 'user' and not s['label'] and not o.get('isMeta') and not o.get('isSidechain'):
                        s['label'] = claude_label(o.get('message'))
                if not isinstance(o, dict) or o.get('type') != 'assistant' or not isinstance(o.get('message'), dict): continue
                msg = o['message']; u = msg.get('usage'); model = msg.get('model')
                if not isinstance(u, dict) or not u or model == '<synthetic>': continue
                ts = opt_utc(o.get('timestamp'))
                if not ts or ts < since: continue
                key = ('claude', str(msg['id']), str(o.get('requestId'))) if msg.get('id') else ('line', str(f), n)
                add(key, (ts, 'claude-code', model, usage_tokens(u), u, f"claude:{o.get('sessionId') or f}:{o.get('agentId') or ''}"))
    root = Path(codex_root).expanduser()
    same = lambda a, b: a[1] == b[1] or (a[0] is not None and a[0] == b[0])  # (total, usage): a thread total may differ from the session's
    for f in sorted(root.rglob('*.jsonl')) if root.is_dir() else ():
        if not _mtime_ok(f, since): continue
        try: fh = f.open('rb')
        except OSError: continue
        with fh:
            model = last = record = report = None; stream = f'codex:{f}'
            s = _session_meta(meta, stream, 'codex') if meta is not None else None; need_label = s is not None and labels
            for line in fh:
                if not any(k in line for k in (b'"turn_context"', b'"token_usage_record"', b'"token_count"', b'"session_meta"')) and not (
                        need_label and (b'"user"' in line or b'"user_message"' in line)):
                    continue
                try: o = json.loads(line)
                except ValueError: continue
                if not isinstance(o, dict): continue
                pl = o.get('payload') if isinstance(o.get('payload'), dict) else {}
                if o.get('type') == 'session_meta':
                    if (began := opt_utc(pl.get('timestamp') or o.get('timestamp'))) and stream not in opened: opened[stream] = began
                    if s is not None:
                        if isinstance(pl.get('cwd'), str): s['cwd'] = s['cwd'] or pl['cwd']
                        git = pl.get('git') if isinstance(pl.get('git'), dict) else {}
                        s['remote'] = s['remote'] or github_repo(git.get('repository_url'))   # owner/name only, never userinfo
                    continue
                if need_label and (label := codex_label(o.get('type'), pl)):
                    s['label'] = label; need_label = False; continue
                if o.get('type') == 'turn_context': model = pl.get('model') or model; continue
                ts = opt_utc(o.get('timestamp')); fresh = bool(ts and ts >= since)
                if o.get('type') == 'token_usage_record' and isinstance(pl.get('usage'), dict):
                    u = pl['usage']; total = pl.get('thread_token_usage')
                    pair = (json.dumps(total, sort_keys=True) if total is not None else None, json.dumps(normalize_usage(u), sort_keys=True))
                    key = ('codex-response', str(pl['response_id'])) if pl.get('response_id') else ('codex', o.get('timestamp'), pair[1])
                    if report and same(report[1], pair): echoes[report[0]] = key; record = None   # its report came first
                    else: record = (key, pair)
                    report = None
                    if fresh: add(key, (ts, 'codex', model, usage_tokens(u), u, stream))
                    continue
                if limits is not None and pl.get('type') == 'token_count' and isinstance(pl.get('rate_limits'), dict) and ts \
                        and (not limits or ts > limits[0]['ts']):
                    limits[:] = [codex_limits(ts, pl['rate_limits'])]
                info = pl.get('info') if pl.get('type') == 'token_count' else None
                if not isinstance(info, dict) or not isinstance(info.get('last_token_usage'), dict): continue
                u = info['last_token_usage']; total = info.get('total_token_usage')
                total = json.dumps(total, sort_keys=True) if total is not None else None
                if total is not None and total == last: continue
                last = total; pair = (total, json.dumps(normalize_usage(u), sort_keys=True))
                key = ('codex', o.get('timestamp'), total or pair[1])
                if record and same(record[1], pair): echoes[key] = record[0]; record = None   # the report of the record just counted
                else: record = None; report = (key, pair)
                if fresh: add(key, (ts, 'codex', model, usage_tokens(u), u, stream))
    for key, target in echoes.items():               # an echo, here or in a copied file, belongs to its record
        if key in cands and key != target: seen[target] |= seen.pop(key); del cands[key]
    return cands, seen, opened


def _price_burn(cands, seen, opened, prices):
    recs = []
    fams = {}
    last = datetime.max.replace(tzinfo=timezone.utc)
    for key, (ts, agent, model, tokens, u, _) in cands.items():
        fam = fams[model] if model in fams else fams.setdefault(model, model_family(model, prices))
        home = min(seen[key], key=lambda st: (opened.get(st, last), st))
        recs.append((ts, agent, model, tokens, message_cost(u, prices[fam]) if fam else None, tuple(sorted(seen[key])), key,
                     normalize_usage(u), home))
    return sorted(recs, key=lambda r: r[0])


def burn_report(recs, now, tz=None, idle_min=5.0):
    """{'today': period, 'week': period} from `scan_burn` records. Today starts at local midnight; the week is
    the last BURN_DAYS days. A period has tokens, priced $, unpriced tokens, per agent and per model splits,
    and agent hours: each session's log-gap activity, summed over sessions (parallel agents add up); a gap
    between the same two responses in several sessions (a resumed or forked copy) counts once."""
    loc = now.astimezone(tz) if tz else now.astimezone()
    starts = {'today': loc.replace(hour=0, minute=0, second=0, microsecond=0), 'week': now - timedelta(days=BURN_DAYS)}
    out = {}
    for name, start in starts.items():
        p = {'responses': 0, 'tokens': 0, 'usd': 0.0, 'unpriced_tokens': 0, 'agents': {}, 'models': {}, 'agent_h': 0.0}
        streams = defaultdict(list)
        for ts, agent, model, tokens, usd, where, *rest in recs:
            if not start <= ts <= now: continue
            p['responses'] += 1; p['tokens'] += tokens
            for s in ((where,) if isinstance(where, str) else where): streams[s].append((ts, rest[0] if rest else (s, ts)))
            a = p['agents'].setdefault(agent, {'tokens': 0, 'usd': 0.0, 'unpriced_tokens': 0})
            m = p['models'].setdefault(model or 'unknown', {'tokens': 0, 'usd': None})
            a['tokens'] += tokens; m['tokens'] += tokens
            if usd is None: p['unpriced_tokens'] += tokens; a['unpriced_tokens'] += tokens
            else: p['usd'] += usd; a['usd'] += usd; m['usd'] = (m['usd'] or 0) + usd
        gaps = {}
        for s in streams.values():
            s.sort(key=lambda r: r[0])
            for (lo, a_id), (hi, b_id) in zip(s, s[1:]):
                if 0 < (hi - lo).total_seconds() <= idle_min * 60: gaps[(a_id, b_id)] = hours(lo, hi)
        p['agent_h'] = sum(gaps.values())
        out[name] = p
    return out


# ---------- usage: spend by repo, cache, trend, parallelism, models (every local log) ----------

USAGE_DAYS = (7, 30, 90)     # `estimate usage --days`; the dashboard offers the last two
LABEL_CHARS = 60             # a session label: the first line of its first prompt, cut to this many characters
SLOT_MIN = 15                # parallelism counts sessions active in slots this long
TOP_SESSIONS = 10
TOP_REPOS = 10               # repos shown by name; the rest are summed as "other"
GIT_TIMEOUT_S = 5            # a git remote lookup for a session folder gives up after this long
_KEY_WORD = re.compile(r'(?i)^(?:sk|pk|rk)[-_]|^gh[pousr]_|^github_pat[_]|^xox[abprs]-|^(?:AKIA|ASIA)[0-9A-Z]{12}|^eyJ[\w-]{8,}\.|^glpat-|^AIza')
_KEY_NAME = re.compile(r'(?i)(?:key|token|secret|passw|pwd|auth|bearer|credential|cookie|session)')


def _session_meta(meta, session, agent):
    return meta.setdefault(session, {'agent': agent, 'cwd': None, 'dir': None, 'remote': None, 'label': None, 'repos': set()})


def _labelled(meta, line, f):
    """Whether the Claude session of a raw transcript line already has a label (read without parsing the line)."""
    m = re.search(rb'"sessionId":\s*"([^"\\]+)"', line)
    s = meta.get(f"claude:{m.group(1).decode('utf-8', 'replace') if m else f}")
    return bool(s and s['label'])


def session_key(stream):
    """The session a burn stream belongs to: a Claude subagent stream (`claude:<session>:<agent id>`) pools into its session."""
    return stream.rsplit(':', 1)[0] if stream.startswith('claude:') else stream


def _secretish(word, after_key):
    """Whether one word of a prompt looks like a credential: a known key prefix, the word after `Bearer` or
    `token:`, or a long run that is mostly letters and digits with both. Linear in the word."""
    core = word.strip('\'"`()[]{}<>,;')
    if not core: return False
    if after_key or _KEY_WORD.search(core): return True
    letters = sum(c.isalpha() for c in core); digits = sum(c.isdigit() for c in core)
    return len(core) >= 20 and letters > 0 and digits > 0 and letters + digits >= len(core) * .7


def prompt_label(text):
    """A session label: the first non-empty line, whitespace collapsed, URL userinfo dropped, credential-like words
    and `name=value` secrets masked, at most LABEL_CHARS. Linear: the line is cut long before masking."""
    line = next((x for x in str(text).splitlines() if x.strip()), '')[:LABEL_CHARS * 8]
    words = []; after = False
    for w in line.split():
        w = re.sub(r'(?<=://)[^/@\s]*@', '', w)                         # https://user:pass@host -> https://host
        if after and w.lower() in ('bearer', 'basic'): words.append(w); continue   # `Authorization: Bearer <token>`
        name, sep, value = w.partition('=') if '=' in w else w.partition(':') if re.match(r'[\w-]+:\S', w) else (w, '', '')
        if sep and value and _KEY_NAME.search(name) and '//' not in value: w = f'{name}{sep}…'
        elif _secretish(w, after): w = '…'
        after = w.lower() in ('bearer', 'basic') or (w.endswith((':', '=')) and bool(_KEY_NAME.search(w)))
        words.append(w)
    line = ' '.join(words)
    return (line if len(line) <= LABEL_CHARS else line[:LABEL_CHARS - 1].rstrip() + '…') or None


def claude_label(msg):
    """Label of a Claude user message: a slash command's name, or the first line of its text; None for a tool
    result or harness text (`<...>`), so the first real prompt labels the session."""
    content = msg.get('content') if isinstance(msg, dict) else None
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content): return None
        content = next((c.get('text') for c in content if isinstance(c, dict) and c.get('type') == 'text' and isinstance(c.get('text'), str)), None)
    if not isinstance(content, str) or not content.strip(): return None
    text = content.lstrip()
    if m := re.search(r'<command-name>\s*(/?[\w:.-]+)\s*</command-name>', text): return prompt_label(m.group(1))
    return None if text.startswith('<') else prompt_label(text)


def codex_label(kind, pl):
    """Label of a Codex user turn (`event_msg/user_message`, or a user `response_item` message); None for the
    injected AGENTS.md and `<environment_context>` items."""
    if kind == 'event_msg' and pl.get('type') == 'user_message' and isinstance(pl.get('message'), str): texts = [pl['message']]
    elif kind == 'response_item' and pl.get('type') == 'message' and pl.get('role') == 'user' and isinstance(pl.get('content'), list):
        texts = [c.get('text') for c in pl['content'] if isinstance(c, dict) and isinstance(c.get('text'), str)]
    else: return None
    for t in texts:
        t = (t or '').lstrip()
        if t and not t.startswith('<') and not t.startswith('# AGENTS.md'): return prompt_label(t)
    return None


def github_repo(url):
    """owner/name of a GitHub remote URL (https, ssh or scp-like), else None."""
    m = re.fullmatch(r'(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::\d+)?/|[^@/\s]+@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?',
                     (url or '').strip())
    return f'{m.group(1)}/{m.group(2)}' if m else None


def _folder_name(p):
    """A cwd with no git repo behind it, named as a folder: its top folder under $HOME (`~/notes (folder)`, so
    every project dir below it pools), else the path itself."""
    home = str(Path.home())
    if p == home: return '~ (folder)'
    if p.startswith(home + os.sep): return f"~/{p[len(home) + 1:].split(os.sep, 1)[0]} (folder)"
    return f'{p} (folder)'


def session_repos(meta, registry=None, remote=None):
    """{session: repo} for `scan_burn` meta. In order: a Codex session's recorded git remote; the git remote of the
    cwd (so every worktree of a repo pools); a registry entry whose checkout or transcript dir holds the session;
    the one repo its pr-links name. A cwd that is gone (a removed worktree) then takes the remote of its nearest
    existing parent, else the one repo every resolved session under that parent maps to. Anything else is a folder:
    its top folder under $HOME (`~/notes (folder)`) or its path, or `(no cwd)`. `remote`: cwd -> owner/name (default `remote_repo`)."""
    remote = remote or (lambda cwd: remote_repo(cwd, timeout=GIT_TIMEOUT_S)); cache = {}

    def by_cwd(cwd):
        if cwd not in cache:
            try: cache[cwd] = remote(cwd) if os.path.isdir(cwd) else None
            except (OSError, subprocess.SubprocessError): cache[cwd] = None
        return cache[cwd]
    under = lambda p, root: p == root or p.startswith(root.rstrip(os.sep) + os.sep)
    trusted = [(os.path.abspath(os.path.expanduser(p)), repo) for repo, e in (registry or {}).items() for p in e.get('claude_project_dir') or ()]
    out = {}; gone = {}
    for sid, s in meta.items():
        cwd = s.get('cwd'); repos = {r for r in s.get('repos') or () if isinstance(r, str)}
        name = s.get('remote') or (by_cwd(cwd) if cwd else None)
        if not name and cwd: name = next((r for p, r in trusted if under(cwd, p)), None)
        if not name and s.get('dir'): name = next((r for p, r in trusted if p == os.path.abspath(s['dir'])), None)
        if not name and len(repos) == 1: name = next(iter(repos))
        if name: out[sid] = name
        elif cwd and not os.path.isdir(cwd): gone[sid] = cwd
        else: out[sid] = _folder_name(cwd) if cwd else '(no cwd)'
    stops = {str(Path.home()), os.sep}
    for sid, cwd in gone.items():
        parent = os.path.dirname(cwd.rstrip(os.sep))
        while parent and parent not in stops and not os.path.isdir(parent): parent = os.path.dirname(parent)
        name = None
        if parent and parent not in stops:
            name = by_cwd(parent)
            if not name:
                found = {out[o] for o, s in meta.items() if o in out and s.get('cwd') and under(s['cwd'], parent) and '/' in out[o]
                         and not out[o].startswith(('~', os.sep, '('))}
                name = next(iter(found)) if len(found) == 1 else None
        out[sid] = name or _folder_name(cwd)
    return out


def merged_by_repo(registry, extra=()):
    """{owner/name: [merge times]} from each registry project's history.json (and `extra` (repo, data dir) pairs).
    A missing or damaged history is skipped: merged PRs per session are then unknown for that repo."""
    out = {}
    for repo, d in [*((r, e.get('dir')) for r, e in (registry or {}).items()), *extra]:
        if not repo or not d or repo in out: continue
        try: hist = Store(os.path.expanduser(d)).read_json('history.json', None)
        except (OSError, ValueError): continue
        if isinstance(hist, list):
            out[repo] = [t for p in hist if isinstance(p, dict) and (t := opt_utc(p.get('mergedAt')))]
    return out


def cache_saving(u, p):
    """$ that cached reads saved: what they would have cost at the input price minus what they cost (same tier rule as message_cost)."""
    prompt = u['input_tokens'] + u['cache_creation_input_tokens'] + u['cache_read_input_tokens']
    if p.get('long_context') and prompt > p.get('long_context_threshold', float('inf')): p = p['long_context']
    return u['cache_read_input_tokens'] * (p['input'] - p['cache_read']) / 1e6


def usage_report(recs, meta, repos, now, days, prices, tz=None, idle_min=5.0, labels=True, merged=None):
    """Spend analytics of the last `days` local days (today and the days before it) from `scan_burn` records.

    `meta`/`repos`: `scan_burn` meta and `session_repos`. `merged`: `merged_by_repo`. Returns totals; cache tiles
    (processed tokens, cached input share, uncached input = input + cache writes, output, savings); $ and tokens per
    repo with sessions and merged PRs per session; the top sessions by $; the daily $ per agent and per model;
    parallelism (median and p80 of sessions active per SLOT_MIN-minute slot, over slots with any activity, overall
    and per day); and per model $, share, tokens and an approximate flag for tokens without a price."""
    loc = now.astimezone(tz) if tz else now.astimezone()
    first = (loc - timedelta(days=days - 1)).date()
    # The first day's own midnight, with that day's UTC offset (DST): a given tz, else the system zone.
    start = datetime(first.year, first.month, first.day, tzinfo=tz) if tz else datetime(first.year, first.month, first.day).astimezone()
    local = lambda t: t.astimezone(tz) if tz else t.astimezone()
    dates = [(first + timedelta(days=k)).isoformat() for k in range(days)]
    fams = {}; total = {'usd': 0.0, 'tokens': 0, 'unpriced_tokens': 0, 'responses': 0, 'unpriced_responses': 0}
    cache = {'input': 0, 'cache_write': 0, 'cache_read': 0, 'output': 0, 'savings_usd': 0.0}
    by_repo = defaultdict(lambda: {'usd': 0.0, 'tokens': 0, 'unpriced_tokens': 0, 'sessions': set()})
    by_model = defaultdict(lambda: {'usd': 0.0, 'tokens': 0, 'unpriced_tokens': 0, 'priced': 0, 'unpriced': 0})
    sess = defaultdict(lambda: {'usd': 0.0, 'tokens': 0, 'start': None, 'models': Counter()})
    daily = {d: {'agents': defaultdict(float), 'models': defaultdict(float), 'tokens': 0} for d in dates}
    stamps = defaultdict(list)
    for r in recs:
        ts, agent, model, tokens, usd = r[:5]
        if not start <= ts <= now or local(ts).date().isoformat() not in daily: continue
        u = r[7] if len(r) > 7 else None; home = r[8] if len(r) > 8 else (r[5] if isinstance(r[5], str) else r[5][0])
        sid = session_key(home); repo = repos.get(sid, '(no cwd)'); day = local(ts).date().isoformat(); m = model or 'unknown'
        total['responses'] += 1; total['tokens'] += tokens
        x = by_repo[repo]; x['tokens'] += tokens; x['sessions'].add(sid)
        y = by_model[m]; y['tokens'] += tokens
        s = sess[sid]; s['tokens'] += tokens; s['agent'] = agent; s['repo'] = repo
        s['start'] = ts if s['start'] is None or ts < s['start'] else s['start']
        daily[day]['tokens'] += tokens; stamps[sid].append(ts)
        if usd is None:
            total['unpriced_tokens'] += tokens; x['unpriced_tokens'] += tokens; y['unpriced_tokens'] += tokens; s['models'][m] += 0
            total['unpriced_responses'] += 1; y['unpriced'] += 1
        else:
            y['priced'] += 1; total['usd'] += usd; x['usd'] += usd; y['usd'] += usd; s['usd'] += usd; s['models'][m] += usd
            daily[day]['agents'][agent] += usd; daily[day]['models'][m] += usd
        if u and not u.get('usage_missing'):
            cache['input'] += u['input_tokens']; cache['cache_write'] += u['cache_creation_input_tokens']
            cache['cache_read'] += u['cache_read_input_tokens']; cache['output'] += u['output_tokens']
            fam = fams[model] if model in fams else fams.setdefault(model, model_family(model, prices))
            if fam and u['cache_read_input_tokens']: cache['savings_usd'] += cache_saving(u, prices[fam])
    prompt = cache['input'] + cache['cache_write'] + cache['cache_read']
    cache.update(processed=prompt + cache['output'], uncached_input=cache['input'] + cache['cache_write'],
                 cached_share=cache['cache_read'] / prompt if prompt else None)
    merged = merged or {}
    repo_rows = []
    for repo, x in sorted(by_repo.items(), key=lambda kv: (-kv[1]['usd'], -kv[1]['tokens'], kv[0])):
        n = len(x['sessions']); prs = None
        if repo in merged: prs = sum(1 for t in merged[repo] if start <= t <= now)
        repo_rows.append({'repo': repo, 'usd': x['usd'], 'tokens': x['tokens'], 'unpriced_tokens': x['unpriced_tokens'], 'sessions': n,
                          'prs': prs, 'prs_per_session': prs / n if prs is not None and n else None})
    top = sorted(sess.items(), key=lambda kv: (-kv[1]['usd'], -kv[1]['tokens'], kv[0]))[:TOP_SESSIONS]
    sessions = [{'start': iso(s['start']), 'agent': s['agent'], 'model': s['models'].most_common(1)[0][0] if s['models'] else None,
                 'repo': s['repo'], 'usd': s['usd'], 'tokens': s['tokens'],
                 'label': ((meta or {}).get(sid) or {}).get('label') if labels else None} for sid, s in top]
    slots = defaultdict(set); gap = idle_min * 60; width = SLOT_MIN * 60
    for sid, ts_list in stamps.items():
        ts_list.sort()
        for t in ts_list: slots[int(t.timestamp() // width)].add(sid)
        for a, b in zip(ts_list, ts_list[1:]):
            if 0 < (b - a).total_seconds() <= gap:
                for k in range(int(a.timestamp() // width), int(b.timestamp() // width) + 1): slots[k].add(sid)
    per_day = defaultdict(list)
    for k, ss in slots.items(): per_day[local(datetime.fromtimestamp(k * width, timezone.utc)).date().isoformat()].append(len(ss))
    counts = [len(ss) for ss in slots.values()]
    known = [r for r in repo_rows if r['prs'] is not None]
    prs, n_known = sum(r['prs'] for r in known), sum(r['sessions'] for r in known)
    parallel = {'median': percentile(counts, .5), 'p80': percentile(counts, .8), 'slots': len(counts),
                'usually': max(round(percentile(counts, .5)), 1) if counts else None,
                'days': [{'date': d, 'median': percentile(per_day[d], .5), 'p80': percentile(per_day[d], .8), 'peak': max(per_day[d])}
                         for d in dates if per_day.get(d)],
                'prs': prs if known else None, 'sessions': n_known if known else None,
                'prs_per_session': prs / n_known if known and n_known else None}
    models = [{'model': m, 'usd': y['usd'] if y['priced'] else None,
               'share': y['usd'] / total['usd'] if total['usd'] else None, 'tokens': y['tokens'],
               'approximate': y['unpriced'] > 0, 'unpriced_tokens': y['unpriced_tokens']}
              for m, y in sorted(by_model.items(), key=lambda kv: (-kv[1]['usd'], -kv[1]['tokens'], kv[0]))]
    return {'days': days, 'start': iso(start), 'end': iso(now), 'basis': PRICING_BASIS, 'labels': labels,
            'total': {**total, 'sessions': len(sess), 'approximate': total['unpriced_responses'] > 0},
            'cache': cache, 'repos': repo_rows, 'sessions': sessions,
            'daily': [{'date': d, 'agents': dict(daily[d]['agents']), 'models': dict(daily[d]['models']), 'tokens': daily[d]['tokens']}
                      for d in dates],
            'parallel': parallel, 'models': models}


AGENT_NAMES = {'claude-code': 'Claude Code', 'codex': 'Codex'}


def _short_model(m):
    return (m or 'unknown').removeprefix('claude-')


def _day(d):
    d = datetime.fromisoformat(d) if isinstance(d, str) else d
    return f'{d.day} {d:%b}'


def usage_lines(rep):
    """`estimate usage` text from `usage_report`."""
    t, c, p = rep['total'], rep['cache'], rep['parallel']; days = rep['days']
    first, last = rep['daily'][0]['date'], rep['daily'][-1]['date']
    approx = f" · approximate: {_tok(t['unpriced_tokens'])} tokens have no price" if t['approximate'] else ''
    out = [f"Usage, last {days} days ({_day(first)} – {_day(last)}): every local Claude Code and Codex log",
           f"Spend {_usd(t['usd'])} · {_tok(t['tokens'])} tokens · {t['sessions']} session{'s' if t['sessions'] != 1 else ''}{approx}",
           f"  ({rep['basis']})", '']
    if not t['responses']:
        return out + ['No agent responses in this period.']
    share = f"{c['cached_share'] * 100:.0f}%" if c['cached_share'] is not None else '–'
    out += ['Cache',
            f"  processed tokens  {_tok(c['processed'])}",
            f"  cached input      {share} of input ({_tok(c['cache_read'])} read from cache)",
            f"  uncached input    {_tok(c['uncached_input'])} (input {_tok(c['input'])} + cache writes {_tok(c['cache_write'])})",
            f"  output            {_tok(c['output'])}",
            f"  cache savings     {_usd(c['savings_usd'])} (cached reads at the input price minus what they cost)", '']
    rows = rep['repos']; shown = rows[:TOP_REPOS]; rest = rows[TOP_REPOS:]
    w = max([len(r['repo']) for r in shown] + [10])
    w = min(w, 44)
    out.append(f"{'By repo':<{w + 2}}{'$':>9}  {'tokens':>8}  {'sessions':>8}  PRs/session")
    for r in shown:
        name = r['repo'] if len(r['repo']) <= w else '…' + r['repo'][-(w - 1):]
        pps = f"{r['prs_per_session']:.2f} ({r['prs']} PRs)" if r['prs_per_session'] is not None else '–'
        out.append(f"  {name:<{w}}{_usd(r['usd']):>9}  {_tok(r['tokens']):>8}  {r['sessions']:>8}  {pps}")
    if rest:
        out.append(f"  {f'{len(rest)} more':<{w}}{_usd(sum(r['usd'] for r in rest)):>9}  {_tok(sum(r['tokens'] for r in rest)):>8}  "
                   f"{sum(r['sessions'] for r in rest):>8}")
    out += ['', f"Top {len(rep['sessions'])} sessions by $"]
    for s in rep['sessions']:
        when = _day(utc(s['start']).astimezone().date())
        label = f"  “{s['label']}”" if s['label'] else ''
        out.append(f"  {when:>6}  {AGENT_NAMES.get(s['agent'], s['agent']):<11}  {_short_model(s['model']):<16}  {_usd(s['usd']):>8}  "
                   f"{s['repo']}{label}")
    out += ['', f'Daily $ ({"per week" if days > 30 else "per day"}): Claude Code █  Codex ░']
    daily = rep['daily']
    buckets = ([(f"{_day(daily[k]['date'])}", daily[k:k + 7]) for k in range(0, len(daily), 7)] if days > 30
               else [(_day(d['date']), [d]) for d in daily])
    sums = [(label, sum(d['agents'].get('claude-code', 0) for d in ds), sum(v for d in ds for a, v in d['agents'].items() if a != 'claude-code'))
            for label, ds in buckets]
    top_usd = max((a + b for _, a, b in sums), default=0) or 1
    for label, a, b in sums:
        bar = '█' * round(a / top_usd * 40) + '░' * round(b / top_usd * 40)
        out.append(f"  {label:>6}  {_usd(a + b):>8}  {bar}")
    if p['slots']:
        prs = (f" · merged PRs per session {p['prs_per_session']:.2f} ({p['prs']} PRs / {p['sessions']} sessions in repos with history)"
               if p['prs_per_session'] is not None else '')
        out += ['', f"Parallel sessions: median {_num(p['median'])}, p80 {_num(p['p80'])} over {p['slots']} active "
                    f"{SLOT_MIN}-minute slots · you usually run ~{p['usually']}{prs}"]
    out += ['', f"{'Models':<30}{'$':>9}  {'share':>6}  {'tokens':>8}"]
    for m in rep['models']:
        usd = _usd(m['usd']) if m['usd'] is not None else '–'
        share = f"{m['share'] * 100:.0f}%" if m['share'] and m['usd'] is not None else '–'
        flag = '  approximate: no price' if m['usd'] is None else (f"  approximate: {_tok(m['unpriced_tokens'])} unpriced" if m['approximate'] else '')
        out.append(f"  {_short_model(m['model']):<28}{usd:>9}  {share:>6}  {_tok(m['tokens']):>8}{flag}")
    return out


def labels_on(args, cfg):
    """Session labels are shown unless --no-labels or `"session_labels": false` in the data dir's config.json."""
    return not getattr(args, 'no_labels', False) and not (isinstance(cfg, dict) and cfg.get('session_labels') is False)


def usage_inputs(args, now, days, prices, limits=None, labels=True):
    """(records, meta, session repos, merged times) for the last `days` days: one transcript scan, the registry and histories."""
    meta = {}
    recs = scan_burn(args.claude_dir, args.codex_dir, now - timedelta(days=days), prices, limits, meta, labels)
    reg = read_registry(); r = resolve(args)
    repos = session_repos(meta, reg)
    merged = merged_by_repo(reg, [(r['repo'], r['dir'])] if r.get('repo') and r.get('dir') else ())
    return recs, meta, repos, merged


def cmd_usage(args):
    now = utc(args.now) if args.now else datetime.now(timezone.utc)
    d = resolve(args)['dir']; cfg = _quiet_config(d); prices = Store(d).prices() if d else DEFAULT_PRICES
    labels = labels_on(args, cfg)
    recs, meta, repos, merged = usage_inputs(args, now, args.days, prices, labels=labels)
    rep = usage_report(recs, meta, repos, now, args.days, prices, idle_min=args.idle_min, labels=labels, merged=merged)
    print(json.dumps(rep, indent=1, ensure_ascii=False) if args.json else '\n'.join(usage_lines(rep)))
    return rep


# ---------- plan windows: status-line snapshots ----------

WINDOWS_FILE = '~/.local/state/agent-estimate/windows.jsonl'   # `estimate statusline-log` appends here ($ESTIMATE_WINDOWS_FILE)
RATE_HISTORY = '~/.claude/data/rate_history.jsonl'              # a status line's own % log: no reset times, no account
SNAPSHOT_EVERY_S = 300        # an unchanged snapshot is logged again only after this long
WINDOW_PRICE_MIN_HOURS = 10   # clean hours needed before a window price is shown
CLEAN_SPAN = timedelta(minutes=15)   # shortest stretch of one hour a window price uses
STALE_PCT = 5                        # a smaller drop in % is a session reporting an older value


def windows_path():
    return Path(os.environ.get('ESTIMATE_WINDOWS_FILE') or WINDOWS_FILE).expanduser()


def claude_config_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser()


def claude_account_tag():
    """A short one-way tag of the signed-in Claude account (sha256 of `oauthAccount.accountUuid`), never the
    e-mail or name; None when unknown. Read from `$CLAUDE_CONFIG_DIR/.claude.json`, else `~/.claude.json`."""
    cfg = os.environ.get('CLAUDE_CONFIG_DIR')
    path = Path(cfg).expanduser() / '.claude.json' if cfg else Path.home() / '.claude.json'
    try: account = json.loads(path.read_text(encoding='utf-8'))['oauthAccount']['accountUuid']
    except (OSError, ValueError, KeyError, TypeError): return None
    return hashlib.sha256(str(account).encode()).hexdigest()[:8] if account else None


def _pct(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1000 else None


def _epoch(v):
    return datetime.fromtimestamp(v, timezone.utc) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def window_snapshot(data, now, account=None):
    """A plan-window snapshot from Claude Code's status-line JSON: % used and reset time of the 5-hour and weekly
    windows, or None without `rate_limits` (API-key sessions have none). Nothing else from the input is kept."""
    rl = data.get('rate_limits') if isinstance(data, dict) else None
    if not isinstance(rl, dict): return None
    out = {'ts': iso(now), 'agent': 'claude-code', 'account': account}
    for k in ('five_hour', 'seven_day'):
        w = rl.get(k) if isinstance(rl.get(k), dict) else {}
        if (pct := _pct(w.get('used_percentage'))) is not None:
            out[k] = {'pct': pct, 'resets_at': iso(r) if (r := _epoch(w.get('resets_at'))) else None}
    return out if 'five_hour' in out or 'seven_day' in out else None


def append_snapshot(path, snap, every_s=SNAPSHOT_EVERY_S):
    """Append `snap` unless the last line has the same windows and account and is under `every_s` seconds old.
    One short line per append, so concurrent status lines do not interleave."""
    path = Path(path); last = None
    try:
        with path.open('rb') as fh:
            fh.seek(0, os.SEEK_END); fh.seek(max(fh.tell() - 4096, 0))
            tail = fh.read().splitlines()
        last = json.loads(tail[-1]) if tail else None
    except (OSError, ValueError): pass
    keep = lambda s: {k: s.get(k) for k in ('account', 'five_hour', 'seven_day')}
    if isinstance(last, dict) and keep(last) == keep(snap) and (then := opt_utc(last.get('ts'))) \
            and 0 <= (utc(snap['ts']) - then).total_seconds() < every_s:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh: fh.write(json.dumps(snap, sort_keys=True) + '\n')
    return True


def read_snapshots(now, days=8, path=None, legacy=None):
    """Claude window snapshots of the last `days`: this tool's own (with reset times and account) and, when present,
    a status line's `rate_history.jsonl` (% only). Each: {'ts', 'account', 'h5', 'w7', 'legacy'}, where a window is
    (pct, resets_at or None) or None. Unreadable lines are skipped."""
    since = now - timedelta(days=days); out = []
    sources = ((Path(path) if path else windows_path(), False),
               (Path(legacy).expanduser() if legacy else Path(os.environ.get('ESTIMATE_RATE_HISTORY') or RATE_HISTORY).expanduser(), True))
    for f, old in sources:
        try: fh = f.open(encoding='utf-8', errors='replace')
        except OSError: continue
        with fh:
            for line in fh:
                try: o = json.loads(line)
                except ValueError: continue
                ts = opt_utc(o.get('ts')) if isinstance(o, dict) else None
                if not ts or not since <= ts <= now: continue
                ts = ts.astimezone(timezone.utc)
                if old:
                    h5, w7 = _pct(o.get('5h_pct')), _pct(o.get('7d_pct'))
                    snap = {'ts': ts, 'account': None, 'h5': (h5, None) if h5 is not None else None,
                            'w7': (w7, None) if w7 is not None else None, 'legacy': True}
                else:
                    win = lambda k: ((p, opt_utc((o.get(k) or {}).get('resets_at'))) if isinstance(o.get(k), dict)
                                     and (p := _pct(o[k].get('pct'))) is not None else None)
                    snap = {'ts': ts, 'account': o.get('account') if isinstance(o.get('account'), str) else None,
                            'h5': win('five_hour'), 'w7': win('seven_day'), 'legacy': False}
                if snap['h5'] or snap['w7']: out.append(snap)
    return sorted(out, key=lambda s: s['ts'])


def window_price(snaps, recs):
    """API-equivalent $ per 1% of the Claude 5-hour and weekly windows from clean clock hours, each window on its
    own. Within one hour: one account, snapshots at least CLEAN_SPAN apart, one reset time, no drop of more than
    STALE_PCT points (a reset or another account; smaller drops are sessions reporting an older %, so the rise
    is the running maximum), and every Claude response between the first and last snapshot priced. The price is
    summed $ over summed % rise; the spend is every local Claude Code response, so API-key sessions in the same
    hour make it rough. Returns {'h5': $ or None, 'w7': ..., 'hours': {'h5': n, 'w7': n}}."""
    by_hour = defaultdict(list)
    for s in snaps: by_hour[s['ts'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)].append(s)
    claude = sorted(((r[0], r[4]) for r in recs if r[1] == 'claude-code'), key=lambda r: r[0]); stamps = [t for t, _ in claude]
    clean = {'h5': [], 'w7': []}
    for ss in by_hour.values():
        own = [s for s in ss if not s['legacy']]; ss = own or ss
        if len({s['account'] for s in ss}) > 1: continue
        for key, found in clean.items():
            pts = [(s['ts'], *s[key]) for s in ss if s[key]]
            if len(pts) < 2 or pts[-1][0] - pts[0][0] < CLEAN_SPAN or len({r for _, _, r in pts}) > 1: continue
            if any(b < a - STALE_PCT for (_, a, _), (_, b, _) in zip(pts, pts[1:])): continue
            lo, hi = bisect.bisect_left(stamps, pts[0][0]), bisect.bisect_right(stamps, pts[-1][0])
            spent = [usd for _, usd in claude[lo:hi]]
            if spent and all(u is not None for u in spent):
                found.append((sum(spent), max(p for _, p, _ in pts) - pts[0][1]))
    out: dict = {'hours': {k: len(v) for k, v in clean.items()}}
    for key, found in clean.items():
        pct = sum(d for _, d in found)
        out[key] = sum(u for u, _ in found) / pct if len(found) >= WINDOW_PRICE_MIN_HOURS and pct >= 1 else None
    return out


def window_report(snaps, recs, codex, now):
    """Plan windows for the dashboard: per Claude window the current reading of the last 7 days ({'pct', 'resets_at',
    'ts'} or None), how many accounts were seen, the latest Codex limits, and the window prices. This tool's own
    snapshot wins over a newer status-line history row, unless its window has reset since and the row is newer
    than the reset (the row has no reset time of its own)."""
    recent = [s for s in snaps if timedelta(0) <= now - s['ts'] <= timedelta(days=BURN_DAYS)]

    def current(key):
        own = next((s for s in reversed(recent) if not s['legacy'] and s[key]), None)
        old = next((s for s in reversed(recent) if s['legacy'] and s[key]), None)
        if own and old:
            resets = own[key][1]
            if (resets and resets <= now and old['ts'] > resets) or (not resets and old['ts'] > own['ts']):
                own = None
        s = own or old
        return {'pct': s[key][0], 'resets_at': s[key][1], 'ts': s['ts']} if s else None
    claude = {k: current(k) for k in ('h5', 'w7')}
    return {'claude': claude if any(claude.values()) else None, 'accounts': len({s['account'] for s in recent if s['account']}),
            'codex': codex, 'price': window_price(recent, recs)}


def codex_limits(ts, rl):
    """The numbers of one Codex `rate_limits` block: plan type and each window's minutes, % used and reset time."""
    wins = [{'minutes': w.get('window_minutes'), 'pct': _pct(w.get('used_percent')), 'resets_at': _epoch(w.get('resets_at'))}
            for w in (rl.get('primary'), rl.get('secondary')) if isinstance(w, dict)]
    plan = rl.get('plan_type') if isinstance(rl.get('plan_type'), str) else None
    return {'ts': ts, 'plan': plan, 'windows': [w for w in wins if w['pct'] is not None and isinstance(w['minutes'], int)]}


def cmd_statusline_log(args):
    """For a Claude Code status line: log the plan-window % from stdin, then run the user's own status line
    command (`--then`) on the same input and print its output, or print a short summary. Never fails."""
    raw = sys.stdin.buffer.read(); snap = None
    try:
        snap = window_snapshot(json.loads(raw or b'null'), datetime.now(timezone.utc), claude_account_tag())
        if snap: append_snapshot(windows_path(), snap)
    except Exception:  # noqa: BLE001 - a status line must keep working whatever the input or disk
        pass
    if args.then:
        try: sys.stdout.buffer.write(subprocess.run(args.then, shell=True, input=raw, capture_output=True, timeout=10, check=False).stdout)
        except (OSError, subprocess.SubprocessError): pass
    elif snap:
        print(' · '.join(f"{label} {snap[k]['pct']:.0f}%" for k, label in (('five_hour', '5h'), ('seven_day', 'week')) if k in snap))


def _settings_file():
    return claude_config_dir() / 'settings.json'


def _ours(command):
    """True for a status line command `statusline_install` wrote."""
    try: parts = shlex.split(command) if isinstance(command, str) else []
    except ValueError: return False
    return len(parts) > 1 and parts[1] == 'statusline-log' and Path(parts[0]).name == 'estimate'


def statusline_install(bin_path, dry):
    """Opt-in: wrap Claude Code's status line so each refresh logs plan-window % (`estimate statusline-log`),
    keeping the user's own status line command as `--then`. Idempotent; `estimate uninstall` restores it."""
    path = _settings_file()
    try: cfg = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError, ValueError): cfg = None
    if not isinstance(cfg, dict):
        print(f'  warning: {path} is not a JSON object; status line not changed', file=sys.stderr); return
    line = cfg.get('statusLine') if isinstance(cfg.get('statusLine'), dict) else {}
    old = line.get('command') if isinstance(line.get('command'), str) else ''
    if _ours(old): print(f'  ok       {path} status line already logs plan windows'); return
    new = f'{shlex.quote(str(bin_path))} statusline-log' + (f' --then {shlex.quote(old)}' if old else '')
    if not dry:
        backup = path.with_name(path.name + '.estimate-backup')
        if path.exists() and not backup.exists(): backup.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps({**cfg, 'statusLine': {**line, 'type': 'command', 'command': new}}, indent=2, ensure_ascii=False) + '\n')
    print(f'  {"would wrap" if dry else "wrapped"} {path} status line: plan-window % go to {windows_path()}'
          + (' (your status line still runs)' if old else ''))


def statusline_uninstall(dry):
    """Undo `statusline_install`: restore the wrapped command, or drop a status line it added. False when the
    status line still calls `estimate` (settings could not be written), else True."""
    path = _settings_file()
    if not path.exists(): return True
    try: text = path.read_text(encoding='utf-8')
    except OSError as err:
        print(f'  warning: could not read {path} ({err.strerror or err}); kept the `estimate` link its status line may call', file=sys.stderr)
        return False
    try: cfg = json.loads(text)
    except ValueError:
        if 'statusline-log' not in text: return True
        print(f'  warning: {path} is not valid JSON and mentions statusline-log; kept the `estimate` link', file=sys.stderr)
        return False
    line = cfg.get('statusLine') if isinstance(cfg, dict) and isinstance(cfg.get('statusLine'), dict) else {}
    cmd = line.get('command')
    if not _ours(cmd): return True
    parts = shlex.split(cmd); old = parts[parts.index('--then') + 1] if '--then' in parts[:-1] else None
    if old: cfg['statusLine'] = {**line, 'command': old}
    else: cfg.pop('statusLine')
    if not dry:
        try: write_atomic(path, json.dumps(cfg, indent=2, ensure_ascii=False) + '\n')
        except OSError as err:
            print(f'  warning: could not restore the status line in {path} ({err.strerror or err}); kept the `estimate` link it calls',
                  file=sys.stderr)
            return False
    print(f'  {"would restore" if dry else "restored"} {path} status line' + ('' if old else ' (removed: estimate added it)'))
    return True


# ---------- planner ----------

def time_boxes(now, tz=None):
    """Planner time boxes (key, label, note, kind, n): the hours left today (whole hours to local midnight,
    at least 1), then 1, 2, 3 and 7 days."""
    loc = now.astimezone(tz) if tz else now.astimezone()
    left = max(int(24 - loc.hour - loc.minute / 60 - loc.second / 3600), 1)
    until = lambda n: loc + timedelta(days=n)
    return [('t0', 'Today', f'{left} h left', 'h', left)] + \
        [(f't{n}', f"{n} day{'s' if n > 1 else ''}", f'until {until(n):%a}', 'd', n) for n in (1, 2, 3)] + \
        [('t7', 'Week', f'until {until(7).day} {until(7):%b}', 'd', 7)]


def tokens_per_sp(rows, cal):
    """Logged tokens per SP of each PR merged in the calibration window (each PR once)."""
    if not cal: return []
    a, b = utc(cal['window']['start']), utc(cal['window']['end'])
    out = []
    for r in one_per_pr([r for r in settled(rows) if a <= utc(r['actual']['merged_at']) <= b]):
        t = sum(usage_tokens(u) for us in (r['actual'].get('usage_by_model') or {}).values() for u in us)
        if t: out.append(t / r['sp'])
    return out


def box_capacity(box, tier, rates):
    """SP of work one agent finishes in a time box at one waterline. Hour boxes: hours ÷ wall hours per SP of
    merged PRs (sure p80, likely median, maybe p20). Day boxes: days × one agent's SP per day (total SP ÷
    one-agent days at p80, median, p20)."""
    q = dict(TIERS)[tier]
    if box[3] == 'h': return box[4] / rates['wall'][q] if rates['wall'][q] > 0 else math.inf
    return box[4] * rates['sp_day'][q]


def schedule(ordered, agents):
    """Greedy list schedule in the given order (Score, dependencies first): each card goes to the agent that can
    start it first, once that agent is free and the cards it depends on are done. Cards are never split. A card
    that depends on a card not placed before it (a cycle, or one waiting on an unknown card) never ends.
    Times are in SP of one agent's work. Returns (agent, start, end) per card."""
    free = [0.0] * agents; end = {}; out = []; ids = {it['id'] for it in ordered}
    for it in ordered:
        deps = [d for d in it.get('depends') or [] if d in ids]
        if any(d not in end for d in deps):
            end.setdefault(it['id'], math.inf); out.append((0, math.inf, math.inf)); continue
        ready = max((end[d] for d in deps), default=0.0)
        i = min(range(agents), key=lambda k: (max(free[k], ready), -free[k], k))
        start = max(free[i], ready); free[i] = start + it['sp']
        end.setdefault(it['id'], free[i]); out.append((i, start, free[i]))
    return out


def done_cards(plan, cap):
    """Cards finished within `cap` walking the order: counting stops at the first card that ends later."""
    return next((n for n, (_, _, end) in enumerate(plan) if end > cap + 1e-9), len(plan))


def plan_state(ordered, box, agents, rates, plans):
    """What one time box × agents gets done: cards and SP per waterline from the schedule, then for the likely SP
    the agent hours, $ and tokens at median per-SP rates, and about how many agents finish every card in this
    box at the likely pace with the same greedy schedule (None when a card or a chain of dependent cards is
    longer than the box). `plans` caches `schedule(ordered, k)` by agent count."""
    def plan(k):
        if k not in plans: plans[k] = schedule(ordered, k)
        return plans[k]
    s = {}
    for t, _ in TIERS:
        n = done_cards(plan(agents), box_capacity(box, t, rates))
        s[t] = {'cards': n, 'sp': sum(it['sp'] for it in ordered[:n])}
    sp = s['likely']['sp']
    for k, rate in (('hours', 'active'), ('usd', 'usd'), ('tok', 'tok')):
        s[k] = sp * rates[rate]['mid'] if rates.get(rate) else None
    cap = box_capacity(box, 'likely', rates); n = len(ordered); fits = lambda k: done_cards(plan(k), cap) == n
    s['need'] = None
    if n and fits(n):                       # as many agents as cards: only card length and dependency chains limit
        lo, hi = max(min(math.ceil(sum(it['sp'] for it in ordered) / cap - 1e-9), n), 1), n
        while lo < hi:
            mid = (lo + hi) // 2
            if fits(mid): hi = mid
            else: lo = mid + 1
        s['need'] = lo
    s['all'] = s['likely']['cards'] == n
    return s


def tier_of(index, s):
    return next((t for t, _ in TIERS if index < s[t]['cards']), None)


# ---------- critical-chain plan ----------

PLAN_RUNS = 500         # Monte Carlo runs of the list scheduler
PLAN_MAX_AGENTS = 8     # agent counts tried for the "max useful agents" line
USEFUL_GAIN = .05       # one more agent that saves less than this share of the p50 finish is not useful
AHEAD_CARDS = 20        # cards on the dashboard's Ahead timeline
_LANE_LINE = re.compile(r'^\s*[-*]\s*`?([^\s`(:]+)`?\s*[(:]\s*(?:(open|merging)\s+)?cap\s*(\d+)', re.I)


def _cap(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else None


def lane_caps(text, config=None):
    """({lane: {'open': n or None, 'merging': n or None}}, problems) from a `## Lanes` section of the backlog
    file (`- migration (cap 1): …`, `- bot.py (merging cap 1)`) and `lanes` in config.json
    (`{"bot.py": {"open": 1}, "migration": {"merging": 1}}`, or `{"ci": 1}` for open); config wins per lane.

    `open`: cards of the lane started and not yet merged at once. `merging`: cards in their merge wait at once;
    their work may overlap. A lane without a cap, and a card without a lane, run in parallel without limit."""
    caps, problems, inside = {}, [], False
    for line in (text or '').splitlines():
        if m := re.match(r'^\s*#{1,6}\s+(.*?)\s*$', line):
            inside = m.group(1).lower().startswith('lanes'); continue
        if inside and (m := _LANE_LINE.match(line)) and _cap(int(m.group(3))):
            caps[m.group(1)] = {'open': None, 'merging': None, (m.group(2) or 'open').lower(): int(m.group(3))}
    lanes = config.get('lanes') if isinstance(config, dict) else None
    if lanes is not None and not isinstance(lanes, dict):
        problems.append('config.json: lanes must be {"lane": {"open": 1}} or {"lane": 1}; ignored'); lanes = {}
    for name, v in (lanes or {}).items():
        if _cap(v): caps[name] = {'open': v, 'merging': None}
        elif isinstance(v, dict) and v and set(v) <= {'open', 'merging'} and all(_cap(x) or x is None for x in v.values()):
            caps[name] = {'open': v.get('open'), 'merging': v.get('merging')}
        else: problems.append(f'config.json: lane {name!r} needs a whole number ≥ 1 for "open" and/or "merging"; ignored')
    return caps, problems


def commit_times(store):
    """PR number -> commit times from history.json, for merge waits."""
    return {p['number']: [t for c in p.get('commits') or [] if (t := opt_utc(c.get('authoredDate')))]
            for p in store.read_json('history.json', []) if p.get('number') is not None}


def plan_samples(rows, cal, commits=None):
    """Per merged PR of the calibration window: (work hours per SP, merge wait hours), and the gap factor.

    Merge wait: the PR's last commit to its merge (review and CI tail; 0 without commit times). Work: the rest
    of start→merge. A PR whose measured start is its last commit (one commit, no earlier agent log) has no
    measurable split: its whole start→merge time counts as work, so card time still grows with SP. Gap factor: the window's calendar hours since the first merge ÷ hours with at least one PR
    in flight. It turns one PR's elapsed hours into an agent's calendar hours (pauses, planning, the owner's
    day), so that as many agents as the repo had PRs in flight reproduce its measured pace."""
    if not cal: return {'pairs': [], 'gap': 1.0}
    a, b = utc(cal['window']['start']), utc(cal['window']['end'])
    pairs, spans = [], []
    for r in one_per_pr([r for r in settled(rows) if a <= utc(r['actual']['merged_at']) <= b]):
        x = r['actual']; merged = utc(x['merged_at']); wall = max(x.get('wall_h') or 0.0, 0.0)
        last = max((t for t in (commits or {}).get(x['pr'], []) if t <= merged), default=None)
        wait = min(max(hours(last, merged), 0.0), wall) if last else 0.0
        if wall - wait <= .01 * wall: wait = 0.0
        pairs.append(((wall - wait) / r['sp'], wait))
        if x.get('start_at'): spans.append((utc(x['start_at']), merged))
    t0 = utc(((cal.get('throughput') or {}).get('start')) or cal['window']['start'])
    busy = union_hours(sorted(spans))
    return {'pairs': pairs, 'gap': max(hours(t0, b) / busy, 1.0) if busy > 0 else 1.0}


def plan_scope(items, unsized=()):
    """(cards to plan in Score order with dependencies first, excluded [{'id', 'label', 'reason'}], warnings).

    Excluded, never planned: a card with an unknown or unsized dependency, in a dependency cycle, or waiting
    on an excluded card. A repeated id is planned once."""
    ordered, warnings = order_backlog(items, unsized)
    known, skipped = {i['id'] for i in items}, {u['id']: u for u in unsized}
    label = {i['id']: i.get('label') or i['id'] for i in list(reversed(ordered)) + list(unsized)}
    loop = set(critical_path(items)['cycle'])
    ok, out, excluded = set(), [], []
    for it in ordered:
        if it['id'] in ok: continue
        bad = [d for d in dict.fromkeys(it.get('depends') or []) if d not in ok]
        if not bad:
            ok.add(it['id']); out.append(it); continue
        if gone := [d for d in bad if d in skipped]: why = 'depends on unsized ' + ', '.join('#' + label[d] for d in gone)
        elif gone := [d for d in bad if d not in known]: why = 'depends on unknown ' + ', '.join('#' + d for d in gone)
        elif it['id'] in loop: why = 'in a dependency cycle'
        else: why = 'waits for excluded ' + ', '.join('#' + label.get(d, d) for d in bad)
        excluded.append({'id': it['id'], 'label': label[it['id']], 'reason': why})
    return out, excluded, warnings


def _cpm(cards, deps, dur):
    """Resource-free critical path at fixed durations: (late start per card, chain indices, length).
    `cards` are in dependency order; ties prefer the earlier card."""
    n = len(cards); es, ef = [0.0] * n, [0.0] * n; dependents = defaultdict(list)
    for i in range(n):
        es[i] = max((ef[j] for j in deps[i]), default=0.0); ef[i] = es[i] + dur[i]
        for j in deps[i]: dependents[j].append(i)
    end = max(ef, default=0.0); ls = [0.0] * n
    for i in reversed(range(n)):
        ls[i] = min((ls[j] for j in dependents[i]), default=end) - dur[i]
    chain, i = [], max(range(n), key=lambda k: (ef[k], -k)) if n else None
    while i is not None:
        chain.append(i); i = max(deps[i], key=lambda k: (ef[k], -k)) if deps[i] else None
    return ls, chain[::-1], end


def _plan_run(deps, dependents, work, wait, agents, lanes, caps, late, rank):
    """One run of the list scheduler: free agents take ready cards (every dependency merged, lane under its
    `open` cap) by priority: a card past its late start first (it would slip the end), then Score order.
    Work ends, the agent moves on, the card waits to merge (a `merging` cap queues it). Returns per card
    (start, work end, merged, agent), hours from now."""
    n = len(work); t = 0.0; left = [len(d) for d in deps]; ready = [i for i in range(n) if not left[i]]
    opened, merging, queue = Counter(), Counter(), defaultdict(list)
    idle = list(range(agents)); ev, seq = [], 0
    start, worked, done, who = [0.0] * n, [0.0] * n, [0.0] * n, [0] * n

    def push(at, kind, i):
        nonlocal seq
        heapq.heappush(ev, (at, kind, seq, i)); seq += 1

    def merge(i, at):
        if lanes[i]: merging[lanes[i]] += 1
        push(at + wait[i], 1, i)
    while True:
        if idle and ready:
            for i in sorted(ready, key=lambda i: (0, late[i], rank[i]) if t >= late[i] - 1e-9 else (1, 0, rank[i])):
                if not idle: break
                cap = (caps.get(lanes[i]) or {}).get('open') if lanes[i] else None
                if cap and opened[lanes[i]] >= cap: continue
                ready.remove(i)
                if lanes[i]: opened[lanes[i]] += 1
                who[i] = heapq.heappop(idle); start[i] = t; push(t + work[i], 0, i)
        if not ev: break
        t = ev[0][0]
        while ev and ev[0][0] <= t:  # every event at this time first, a zero merge wait included, then dispatch
            _, kind, _, i = heapq.heappop(ev); lane = lanes[i]
            if kind == 0:
                worked[i] = t; heapq.heappush(idle, who[i])
                cap = (caps.get(lane) or {}).get('merging') if lane else None
                if cap and merging[lane] >= cap: queue[lane].append(i)
                else: merge(i, t)
            else:
                done[i] = t
                if lane:
                    opened[lane] -= 1; merging[lane] -= 1
                    if queue[lane] and (caps.get(lane) or {}).get('merging'): merge(queue[lane].pop(0), t)
                for j in dependents[i]:
                    left[j] -= 1
                    if not left[j]: ready.append(j)
    return start, worked, done, who


def _a(n):
    """`a 4th`, `an 8th`, `an 11th`."""
    return ('an ' if str(n).startswith('8') or n in (11, 18) or 80 <= n < 90 else 'a ') + _ordinal(n)


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def plan_forecast(items, unsized, rows, cal, commits=None, caps=None, agents=3, top=None, runs=PLAN_RUNS, seed=1):
    """Critical-chain forecast of the backlog: Monte Carlo of a list scheduler with K agents and lane caps.

    Card time = SP × a work rate per SP drawn from the window's merged PRs (two-level bootstrap, as in
    `simulate_backlog`) × the gap factor (`plan_samples`), then that PR's merge wait. Per K: p20/p50/p80
    finish of each card and of the scope, the project buffer (p80 − p50 at the end; no per-card padding), and
    the lanes and waves of the run that ends nearest the p50. Also the critical chain (the longest dependency chain at
    median card times), waves (dependency levels), blocked cards and the max useful agent count. Times are
    hours from now. Without measured PRs only the order, waves and chain in SP come back."""
    if not isinstance(runs, int) or isinstance(runs, bool) or runs <= 0: raise ValueError('runs must be a positive integer')
    unsized = list(unsized or []); caps = caps or {}
    scope, excluded, warnings = plan_scope(items, unsized)
    total = len(scope)
    if top: scope = scope[:top]
    index = {c['id']: k for k, c in enumerate(scope)}
    deps = [[index[d] for d in dict.fromkeys(c.get('depends') or []) if d in index] for c in scope]
    dependents = defaultdict(list)
    for i, d in enumerate(deps):
        for j in d: dependents[j].append(i)
    label = lambda i: scope[i].get('label') or scope[i]['id']
    lanes = [c.get('lane') or '' for c in scope]
    samples = plan_samples(rows, cal, commits); pairs, gap = samples['pairs'], samples['gap']
    rank_key = lambda i: (scope[i]['score'] is None, -(scope[i]['score'] or 0), i)
    out = {'cards': [], 'excluded': excluded, 'unsized': [u.get('label') or u['id'] for u in unsized], 'warnings': warnings,
           'of': total, 'top': top if top and top < total else None, 'sp': sum(c['sp'] for c in scope),
           'n': len(pairs), 'rough': len(pairs) < MIN_INTERVAL_N, 'gap': round(gap, 2), 'runs': runs, 'seed': seed,
           'no_depends': not any(deps), 'maybe_done': [label(i) for i in range(len(scope)) if maybe_done(scope[i])],
           'lanes': {k: v for k, v in caps.items() if k in set(lanes)}, 'agents': {}, 'chosen': agents}
    blockers = Counter(j for d in deps for j in d)
    if not pairs: dur = [float(c['sp']) for c in scope]  # no measured pace: SP stand in for time
    else:
        med = {sp: statistics.median(sp * w * gap + x for w, x in pairs) for sp in {c['sp'] for c in scope}}
        dur = [med[c['sp']] for c in scope]
    late, chain, length = _cpm(scope, deps, dur)
    if not any(deps): late, chain, length = [math.inf] * len(scope), [], 0.0  # no dependencies: Score order only
    on_chain = set(chain)
    order = sorted(range(len(scope)), key=lambda i: (rank_key(i)[0], rank_key(i)[1], i not in on_chain, i))
    rank = [0] * len(scope)
    for r, i in enumerate(order): rank[i] = r
    out['chain'] = {'ids': [scope[i]['id'] for i in chain], 'labels': [label(i) for i in chain],
                    'sp': sum(scope[i]['sp'] for i in chain), ('hours' if pairs else 'sp_length'): length}
    for i, c in enumerate(scope):
        out['cards'].append({'id': c['id'], 'label': label(i), 'title': c['title'], 'sp': c['sp'], 'score': c['score'],
                             'lane': lanes[i], 'chain': i in on_chain, 'depends': [scope[j]['id'] for j in deps[i]],
                             'unblocks': blockers[i], 'maybe_done': maybe_done(c)})
    out['blocked'] = [{'id': scope[i]['id'], 'label': label(i), 'waits_for': [label(j) for j in deps[i]]} for i in range(len(scope)) if deps[i]]
    if not scope: return out
    ks = sorted(set(range(1, PLAN_MAX_AGENTS + 1)) | set(PARALLEL) | {agents})
    def lanes_of(k, work, wait):
        """One run's lanes and waves: a new wave starts, in start order, at a card that needs one of the current wave."""
        start, worked, done, who = _plan_run(deps, dependents, work, wait, k, lanes, caps, late, rank)
        sched, waves, cur = [], [], []
        for i in range(len(scope)):
            last = max(deps[i], key=lambda j: done[j]) if deps[i] else None
            sched.append({'id': scope[i]['id'], 'agent': who[i] + 1, 'start': start[i], 'worked': worked[i], 'merged': done[i],
                          'waits_for': label(last) if last is not None else None})
        for i in sorted(range(len(scope)), key=lambda i: (start[i], who[i])):
            if cur and any(j in cur for j in deps[i]): waves.append(cur); cur = []
            cur.append(i)
        return {'schedule': sched, 'waves': [{'ids': [scope[i]['id'] for i in w]} for w in waves + [cur]]}
    if not pairs:  # no measured pace: one run with SP as time and no merge waits
        out['agents'] = {k: lanes_of(k, dur, [0.0] * len(scope)) for k in ks}
        return out
    rng = random.Random(seed); ends = {k: [] for k in ks}; finish = {k: [[] for _ in scope] for k in ks}; draws = []
    for _ in range(runs):
        hist = rng.choices(pairs, k=len(pairs)); draw = [rng.choice(hist) for _ in scope]
        work = [c['sp'] * w * gap for c, (w, _) in zip(scope, draw)]; wait = [x for _, x in draw]
        draws.append((work, wait))
        for k in ks:
            done = _plan_run(deps, dependents, work, wait, k, lanes, caps, late, rank)[2]
            ends[k].append(max(done))
            for i, t in enumerate(done): finish[k][i].append(t)
    q = lambda xs: {'p20': percentile(xs, .2), 'p50': percentile(xs, .5), 'p80': percentile(xs, .8)}
    for k in ks:
        end = q(ends[k])
        # The lanes show the run that ends closest to the p50 finish: a real run, not a sum of medians.
        r50 = min(range(runs), key=lambda r: (abs(ends[k][r] - end['p50']), r))
        a = out['agents'][k] = {**lanes_of(k, *draws[r50]), 'end': end, 'buffer': end['p80'] - end['p50'],
                                'cards': {scope[i]['id']: q(finish[k][i]) for i in range(len(scope))}}
        upto = [0.0] * runs  # a wave's date: it and every earlier wave merged, so dates never go back
        for w in a['waves']:
            ix = [index[x] for x in w['ids']]
            upto = [max(upto[r], *(finish[k][i][r] for i in ix)) for r in range(runs)]
            w.update(q(upto))
    p50 = {k: out['agents'][k]['end']['p50'] for k in ks}
    # None: even the largest count tried still gains USEFUL_GAIN or more from one more agent
    out['useful'] = next((k for k in ks if k + 1 in p50 and p50[k] - p50[k + 1] < USEFUL_GAIN * p50[k]), None)
    out['searched'] = max(k for k in ks if k - 1 in p50)
    out['saves'] = {k: p50[k] - p50[k + 1] for k in ks if k + 1 in p50}
    return out


def _date(now, h, tz=None):
    d = (now + timedelta(hours=h)).astimezone(tz) if tz else (now + timedelta(hours=h)).astimezone()
    return f'{d:%b} {d.day}'


def _saving(h, p50):
    """Hours one more agent saves, as days; under 0.1 d or 1% of the finish it is noise: `nothing`."""
    return 'nothing' if h < max(2.4, .01 * p50) else f'{_num(h / 24)} d'


def agents_line(plan, k, now, tz=None):
    """`3 agents: done ~Nov 6 (p80 Nov 12); a 4th saves 0.5 d; a 5th saves nothing`."""
    a = plan['agents'][k]; e = a['end']; out = f"{_agents(k)}: done ~{_date(now, e['p50'], tz)} (p80 {_date(now, e['p80'], tz)})"
    for m in (k + 1, k + 2):
        if m - 1 in plan['saves']: out += f"; {_a(m)} saves {_saving(plan['saves'][m - 1], plan['agents'][m - 1]['end']['p50'])}"
    return out


def plan_lines(plan, now, tz=None, cal=None):
    """The short text of `estimate plan`."""
    tag = lambda x: '#' + x
    cards = plan['cards']; k = plan['chosen']; label = {c['id']: c['label'] for c in cards}
    scope = f"{len(cards)} cards, {plan['sp']} SP" + (f" (top {plan['top']} of {plan['of']} in Score order, dependencies first)" if plan['top'] else '')
    out = [f'plan: {scope}' + (f"; {plan['n']} measured PRs" + (', rough' if plan['rough'] else '') if plan['n'] else '')]
    warn = []
    if plan['unsized']: warn.append(f"skipped {len(plan['unsized'])} unsized card(s), not planned: {_short_list([tag(x) for x in plan['unsized']])}")
    for x in plan['excluded']: warn.append(f"#{x['label']} not planned: {x['reason']}")
    warn += [w for w in plan['warnings'] if w.startswith('duplicate id')]
    if plan['maybe_done']: warn.append(f"{len(plan['maybe_done'])} of the planned cards may already be done (Tiden links merged code): "
                                       f"{_short_list([tag(x) for x in plan['maybe_done']])}")
    if plan['no_depends'] and cards: warn.append('no dependencies given, order is Score only')
    if not cards:
        return out + [f'  warning: {w}' for w in warn] + ['  nothing to plan']
    if plan['agents'] and 'end' in plan['agents'][k]:
        for a in sorted(set(PARALLEL) | {k}): out.append('  ' + agents_line(plan, a, now, tz))
        u = plan['useful']
        out.append(f"  max useful agents: {u} ({_a(u + 1)} saves under {USEFUL_GAIN:.0%})" if u else
                   f"  max useful agents: over {plan['searched'] - 1} ({_a(plan['searched'])} still saves {USEFUL_GAIN:.0%} or more; not searched further)")
        out.append(f"  project buffer, {_agents(k)}: {_num(plan['agents'][k]['buffer'] / 24)} d (p80 − p50 at the end; no per-card padding)")
    else: out.append('  no measured PRs yet: order, waves and chain in SP only; dates come after `estimate record` settles merged PRs')
    ch = plan['chain']
    length = f"≈{_num(ch['hours'] / 24)} d at median card times" if 'hours' in ch else f"{ch['sp']} SP"
    if ch['labels']:
        out.append(f"critical chain: {len(ch['labels'])} card{'s' if len(ch['labels']) != 1 else ''}, {ch['sp']} SP, {length}: "
                   + ' → '.join(tag(x) for x in ch['labels']))
    else: out.append('critical chain: none (no dependencies)')
    a = plan['agents'].get(k)
    if a:
        out.append(f"waves, {_agents(k)} (start order; a new wave starts at a card that needs the last one"
                   + ('; date = it and all earlier waves merged, p50 / p80' if 'end' in a else '') + '):')
        groups = []  # waves next to each other with the same dates print as one line (`6–17.`)
        for n, w in enumerate(a['waves'], 1):
            when = f"{_date(now, w['p50'], tz)} / {_date(now, w['p80'], tz)}  " if 'p50' in w else ''
            if groups and when and groups[-1][2] == when: groups[-1][1] = n; groups[-1][3] += w['ids']
            else: groups.append([n, n, when, list(w['ids'])])
        for first, last, when, ids in groups:
            out.append(f"  {first}{f'–{last}' if last != first else ''}. {when}{len(ids)} card{'s' if len(ids) != 1 else ''}: "
                       + _short_list([tag(label[x]) for x in ids], 10))
    if plan['blocked']:
        top = sorted((c for c in cards if c['unblocks']), key=lambda c: -c['unblocks'])[:3]
        out.append(f"blocked now: {len(plan['blocked'])} card{'s wait' if len(plan['blocked']) != 1 else ' waits'} for others; most waited on: "
                   + ', '.join(f"#{c['label']} ({c['unblocks']})" for c in top))
    if plan['lanes']:
        out.append('lanes: ' + ', '.join(f"{n} " + ' '.join(f'{kind} ≤{v}' for kind, v in cap.items() if v) for n, cap in plan['lanes'].items()))
    out += [f'  warning: {w}' for w in warn]
    if plan['n']:
        out.append(f"based on: card time = SP × a work rate per SP drawn from {plan['n']} merged PRs × {_num(plan['gap'])} (calendar hours per "
                   f"hour with a PR in flight) + that PR's merge wait; {plan['runs']} runs, seed {plan['seed']}; "
                   'chain cards first when they would slip the end, otherwise Score order')
    return out


# ---------- dashboard ----------

CSS = """
:root{color-scheme:dark;--bg:#0a0b12;--well:#161929;--line:#232741;--ink:#e9ecf6;--muted:#9aa1c0;--faint:#7c83a3;
--violet:#8d7dff;--coral:#ff6f91;--lime:#cdf463;--thumb:#262b47;--sel:#8d7dff;--sel-ink:#0a0b12;--focus:#cdf463;--warn:#ffb86b;
--sure:var(--violet);--likely:color-mix(in oklab,var(--violet) 60%,var(--bg));--maybe:color-mix(in oklab,var(--violet) 28%,var(--bg))}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#f4f4fb;--well:#e8e9f5;--line:#d3d6ea;--ink:#13152a;--muted:#555b80;
--faint:#6b7091;--violet:#5b48e6;--coral:#d6336c;--lime:#4d7c0f;--thumb:#fff;--sel:#5b48e6;--sel-ink:#fff;--focus:#5b48e6;--warn:#9a4a00;
--likely:color-mix(in oklab,var(--violet) 55%,var(--bg));--maybe:color-mix(in oklab,var(--violet) 25%,var(--bg))}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%;scrollbar-color:var(--line) transparent}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 Figtree,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;overflow-x:hidden;-webkit-tap-highlight-color:transparent}
::selection{background:var(--sel);color:var(--sel-ink)}
p{margin:0}code{font-size:13px}
.vh,.radio,#all{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.v{display:none}
main{max-width:1180px;margin:0 auto;padding:26px 32px 56px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;font-size:13px;color:var(--muted)}
.top b{color:var(--ink);font-weight:600;font-size:15px}
.disp{font-family:"Bricolage Grotesque",Figtree,system-ui,-apple-system,sans-serif;font-weight:600;letter-spacing:-.03em}
.hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px 48px;align-items:end;margin-top:28px}
.huge{font-size:clamp(72px,12vw,150px);line-height:.9;margin:0;letter-spacing:-.05em}
.huge small{font-size:.24em;letter-spacing:-.01em;color:var(--muted);font-weight:500;margin-left:.2em;font-family:Figtree,system-ui,sans-serif}
.huge.short{font-size:clamp(40px,6vw,64px);line-height:1.05}
.line{font-size:18px;margin-top:12px;max-width:54ch}.line b{font-weight:650}
.fine{color:var(--muted);margin-top:6px;max-width:60ch}.fine em{font-style:normal;color:var(--lime)}
.controls{display:flex;flex-direction:column;gap:12px;align-items:flex-start}
.controls .row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.controls .row>span{color:var(--muted);font-size:13px;width:64px}
.seg{display:inline-flex;gap:2px;padding:3px;border:1px solid var(--line);border-radius:12px;background:var(--well)}
.seg label{--on:0;cursor:pointer;user-select:none;padding:8px 14px;border-radius:9px;line-height:1.1;display:flex;flex-direction:column;gap:3px;
color:var(--muted);touch-action:manipulation;background:color-mix(in oklab,var(--thumb) calc(var(--on)*100%),transparent);
transition:background-color 160ms cubic-bezier(.23,1,.32,1),color 160ms cubic-bezier(.23,1,.32,1)}
.seg label:hover{color:var(--ink)}.seg label:active{transform:scale(.97)}
.seg label small{font-size:.72em;color:var(--faint)}
.tl{margin-top:34px;border-top:1px solid var(--line);padding-top:14px}
.axis{display:flex;justify-content:space-between;color:var(--faint);font-size:12px}
.lanes{display:flex;flex-direction:column;gap:6px;position:relative;padding:24px 0 22px}
.lane{display:grid;grid-template-columns:70px 1fr;gap:12px;align-items:center}
.lane .who{color:var(--muted);font-size:13px}
.track{position:relative;height:34px;background:var(--well);border-radius:7px;overflow:hidden}
.card{--tier:var(--likely);position:absolute;top:3px;bottom:3px;left:calc(3px + (100% - 6px) * var(--l));width:calc((100% - 6px) * var(--w) - 2px);border-radius:4px;background:var(--tier);color:var(--ink);font-size:12px;line-height:28px;
padding:0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.card.sure{--tier:var(--sure);color:var(--sel-ink)}.card.maybe{color:var(--muted);background:transparent;border:1px dashed var(--line)}
.mark{position:absolute;top:6px;bottom:4px;width:0;border-left:2px solid var(--lime);pointer-events:none;left:calc(82px + (100% - 82px) * var(--x))}
.mark b{position:absolute;top:-16px;left:6px;font-weight:500;font-size:12px;color:var(--lime);white-space:nowrap}
.mark.end b{left:auto;right:6px}
.mark.lo{border-left:2px dashed var(--coral)}.mark.lo b{top:auto;bottom:-18px;color:var(--coral)}
.empty{color:var(--muted);padding:18px 0 8px}
.legend{display:flex;gap:18px;margin-top:12px;color:var(--muted);font-size:13px;flex-wrap:wrap}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:6px}
.strip{margin-top:40px;display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.w{border-top:1px solid var(--line);padding-top:10px}
.w .lab{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:13px}
.w .lab span+span{color:var(--faint)}
.w .big{font-size:56px;line-height:1;margin:4px 0 8px}.w .big small{font-size:20px;color:var(--muted);letter-spacing:0;margin-left:2px;font-family:Figtree,system-ui,sans-serif;font-weight:500}
.bar{position:relative;height:8px;border-radius:4px;background:var(--well);margin-top:12px;overflow:hidden}.w .big+.bar{margin-top:0}.bar i{position:absolute;inset:0 auto 0 0;background:var(--coral);border-radius:4px}.bar i.x{background:var(--muted)}
.w .sub{margin-top:6px;color:var(--faint);font-size:13px}
.grid{margin-top:40px;display:grid;grid-template-columns:1.2fr 1fr;gap:24px 48px;align-items:start}
h2{font:600 12px/1 Figtree,system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.list ol{list-style:none;margin:0;padding:0}
.list li{--tier:transparent;display:grid;grid-template-columns:28px minmax(0,1fr) 52px 40px;gap:10px;align-items:baseline;padding:8px 0 8px 12px;
border-bottom:1px solid var(--line);position:relative}
.list li::before{content:"";position:absolute;left:0;top:9px;bottom:9px;width:3px;border-radius:2px;background:var(--tier)}
.list .n{color:var(--faint);font-size:13px}.list .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.list .k{font-size:.8em;color:var(--faint);margin-right:.5em}
.list .s,.list .p{text-align:right;color:var(--muted)}.list .s{color:var(--ink);font-weight:600}
.list li.none{color:var(--faint)}.list li.none .s{color:var(--faint);font-weight:400}
.wait{margin-right:.5em;font-size:11.5px;color:var(--warn);border:1px solid currentColor;border-radius:999px;padding:0 .45em;white-space:nowrap}
.rest{display:none}#all:checked~.rest{display:block}
.more{display:inline-block;margin-top:12px;color:var(--violet);cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.more .on,#all:checked~.more .off{display:none}#all:checked~.more .on{display:inline}
.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:0 24px}
.kv>div{padding:8px 0;border-bottom:1px solid var(--line)}
.kv>div:nth-child(odd){color:var(--muted);white-space:nowrap}
.kv>div:nth-child(even){text-align:right}
.kv b{font-family:"Bricolage Grotesque",Figtree,system-ui,sans-serif;font-size:22px;font-weight:600;letter-spacing:-.02em;white-space:nowrap}
.kv small{color:var(--faint);font-size:12px;margin-left:6px}.kv small.ln{display:block;margin:2px 0 0}
.kv .no{color:var(--faint)}
.foot{margin-top:40px;display:grid;gap:6px;color:var(--faint);font-size:13px;max-width:80ch}.foot .warn{color:var(--warn)}
:focus-visible{outline:2px solid var(--focus);outline-offset:3px;border-radius:6px}
#all:focus-visible~.more{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
@media (prefers-reduced-motion:reduce){.seg label{transition:none}.seg label:active{transform:none}}
@media (max-width:720px){
main{padding:20px 18px 48px}.hero,.grid,.strip{grid-template-columns:1fr}.huge{font-size:72px}.huge.short{font-size:44px}
.lane{grid-template-columns:48px 1fr}.card{font-size:11px;padding:0 5px}.w .big{font-size:44px}.seg label{padding:8px 10px}
.kv{grid-template-columns:1fr}.kv>div:nth-child(odd){padding-bottom:0;border:0;font-size:13px}.kv>div:nth-child(even){text-align:left}
.kv b{white-space:normal}.mark b{font-size:11px}.mark{left:calc(60px + (100% - 60px) * var(--x))}}
"""


def state_css(boxes):
    """The planner without a script: the checked time box and agent radios (siblings before <main>) show one
    precomputed state, light their two labels and colour each backlog row's waterline."""
    out = []
    for b in boxes:
        out.append(f'#tb-{b[0]}:checked~main label[for=tb-{b[0]}]{{--on:1;color:var(--ink)}}'
                   f'#tb-{b[0]}:focus-visible~main label[for=tb-{b[0]}]{{outline:2px solid var(--focus);outline-offset:2px}}')
        for a in PARALLEL:
            sel = f'#tb-{b[0]}:checked~#ag-{a}:checked~main'
            out.append(f'{sel} .v.{b[0]}a{a}{{display:revert}}'
                       + ''.join(f'{sel} .k{b[0]}a{a}-{t}{{--tier:var(--{t})}}' for t, _ in TIERS))
    out += [f'#ag-{a}:checked~main label[for=ag-{a}]{{--on:1;color:var(--ink)}}'
            f'#ag-{a}:focus-visible~main label[for=ag-{a}]{{outline:2px solid var(--focus);outline-offset:2px}}' for a in PARALLEL]
    return '\n'.join(out)


def _agents(a):
    return f"{a} agent{'s' if a != 1 else ''}"


def _title(i):
    """(key or '', the title without a leading `KEY — `)."""
    key = i.get('key') or ''
    return key, (re.sub(rf'^{re.escape(key)}\s*[—–-]\s*', '', i['title']) if key else i['title'])


def _kind(s):
    return 'p20–p80' if s['kind'] == 'p20-p80' else 'min–max'


def _prs(n):
    return f"{n} PR{'s' if n != 1 else ''}"


def burn_rows(burn):
    """Burn and rates rows for today and the last BURN_DAYS days: (label, value html, note html)."""
    e = html.escape
    if not burn: return [('Today', '<span class="no">no local agent logs found</span>', '')]
    if burn.get('error'): return [('Today', '<span class="no">agent logs could not be read</span>', '')]
    names = {'claude-code': 'Claude', 'codex': 'Codex'}
    out = []
    for key, label in (('today', 'Today'), ('week', f'{BURN_DAYS} days')):
        p = burn[key]
        if not p['responses']: out.append((label, '<span class="no">no agent responses</span>', '')); continue
        split = [f"{_tok(a['tokens'])} {names.get(k, k)}" + (' unpriced' if a['unpriced_tokens'] >= a['tokens']
                                                             else ' partly unpriced' if a['unpriced_tokens'] else '')
                 for k, a in sorted(p['agents'].items(), key=lambda kv: -kv[1]['tokens'])]
        note = ' · '.join(split)
        value = (f"<b>{e(_usd(p['usd']))}</b>" if p['usd'] or not p['unpriced_tokens'] else f"<b>{e(_tok(p['tokens']))}</b>") \
            + f"<small>{e(_hrs(p['agent_h']))} agent work</small>"
        if key == 'week':
            top = sorted(p['models'].items(), key=lambda kv: (-(kv[1]['usd'] or 0), -kv[1]['tokens']))[:3]
            models = ' · '.join(f"{m.removeprefix('claude-')} {_usd(v['usd']) if v['usd'] is not None else _tok(v['tokens']) + ' unpriced'}"
                                for m, v in top)
            out.append((label, value, f'<small class="ln">{e(note)}</small><small class="ln">{e(models)}</small>'))
        else: out.append((label, value, f'<small class="ln">{e(note)}</small>'))
    return out


def _left(delta):
    m = max(int(delta.total_seconds() // 60), 0); d, rest = divmod(m, 1440); h, mm = divmod(rest, 60)
    return f'{d} d {h} h' if d else f'{h} h {mm} min' if h else f'{mm} min'


def _ago(delta):
    m = max(int(delta.total_seconds() // 60), 0)
    return 'just now' if m < 1 else f'{m} min ago' if m < 60 else f'{m // 60} h ago' if m < 48 * 60 else f'{m // 1440} d ago'


def subscriptions(cfg):
    """Plans typed once into config.json: {"subscriptions": [{"name": "Claude Max", "usd_per_month": 200}, ...]}.
    (plans, warning or None); a malformed list hides the row with a warning and never stops the dashboard."""
    subs = cfg.get('subscriptions') if isinstance(cfg, dict) else None
    if subs is None: return [], None
    good = lambda s: (isinstance(s, dict) and isinstance(s.get('name'), str) and s['name'].strip()
                      and isinstance(s.get('usd_per_month'), (int, float)) and not isinstance(s['usd_per_month'], bool) and s['usd_per_month'] > 0)
    if not isinstance(subs, list) or not subs or not all(good(s) for s in subs):
        return [], 'config.json: subscriptions must be a list like [{"name": "Claude Max", "usd_per_month": 200}]; the row is hidden'
    return [{'name': ' '.join(s['name'].split()), 'usd': float(s['usd_per_month'])} for s in subs], None


def window_cards(win, now, tz=None):
    """The plan-window cards: Claude 5-hour and week from the latest snapshot, Codex week from the latest Codex
    limits; each (label, right text, % or None, note). A window whose reset time has passed, or a % without a reset
    time older than the window itself, is not shown as current: it is never guessed."""
    win = win or {}; claude = win.get('claude') or {}; price = win.get('price') or {}
    loc = lambda d: d.astimezone(tz) if tz else d.astimezone()
    day = lambda d: f'{loc(d):%a} {loc(d).day} {loc(d):%b}, {loc(d):%H:%M}'
    out = []
    for key, label, span, when, unit in (('h5', 'Claude · 5-hour window', timedelta(hours=5), lambda d: f'{loc(d):%H:%M}', 'window'),
                                          ('w7', 'Claude · week', timedelta(days=7), day, 'week')):
        w = claude.get(key)
        if not w: out.append((label, 'no snapshot yet', None, '')); continue
        pct, resets, age = w['pct'], w['resets_at'], now - w['ts']
        if resets and resets <= now: out.append((label, f'reset {when(resets)}', None, 'no snapshot since the reset')); continue
        if not resets and age > span: out.append((label, 'no recent snapshot', None, f'last {pct:.0f}% {_ago(age)}')); continue
        note = ([f'≈ {_usd(price[key] * 100)} API-equivalent per {unit}, rough'] if price.get(key) else []) \
            + ([f'{_left(resets - now)} left'] if resets else []) + ([f'snapshot {_ago(age)}'] if age > timedelta(minutes=10) else []) \
            + ([f"latest of {win['accounts']} accounts"] if win.get('accounts', 0) > 1 else [])
        out.append((label, f'resets {when(resets)}' if resets else f'as of {loc(w["ts"]):%H:%M}', pct, ' · '.join(note)))
    lim = win.get('codex'); wins = (lim or {}).get('windows') or []
    w = next((x for x in wins if x['minutes'] == 7 * 24 * 60), wins[0] if wins else None)
    if not w: out.append(('Codex · week', 'no snapshot yet', None, ''))
    elif w['resets_at'] and w['resets_at'] <= now: out.append(('Codex · week', f"reset {day(w['resets_at'])}", None, 'no snapshot since the reset'))
    else:
        age = now - lim['ts']
        note = ([f"{lim['plan'].title()} plan" + ('' if any(x['minutes'] == 300 for x in wins) else ', no 5-hour window')] if lim.get('plan') else []) \
            + ([f"{_left(w['resets_at'] - now)} left"] if w['resets_at'] else []) + ([f'snapshot {_ago(age)}'] if age > timedelta(minutes=10) else [])
        out.append(('Codex · week' if w['minutes'] == 7 * 24 * 60 else f"Codex · {w['minutes'] // 60} h window",
                    f"resets {day(w['resets_at'])}" if w['resets_at'] else f'as of {loc(lim["ts"]):%H:%M}', w['pct'], ' · '.join(note)))
    return out


AHEAD_CSS = """
.ahead{margin-top:40px;border-top:1px solid var(--line);padding-top:14px}
.ahead .head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.ahead .head h2{margin:0}
.ahead .line{margin-top:10px;max-width:none}
.card.cc{--tier:var(--violet);color:var(--sel-ink);font-weight:600}
.card.wt{opacity:.55}.card.cc.wt{opacity:.8}
.card.mw{top:auto;bottom:2px;height:5px;border-radius:2px;padding:0;background:repeating-linear-gradient(135deg,var(--muted) 0 2px,transparent 2px 5px)}
.track.buf{height:14px;background:transparent}
.track.buf i{position:absolute;top:3px;bottom:3px;left:calc((100% - 6px) * var(--l) + 3px);width:calc((100% - 6px) * var(--w));
border-radius:4px;background:repeating-linear-gradient(135deg,color-mix(in oklab,var(--coral) 70%,transparent) 0 4px,transparent 4px 8px);
border:1px solid var(--coral)}
.ahead .waits{margin-top:10px;color:var(--muted);font-size:13px;max-width:90ch}
.ahead .waits b{font-weight:500;color:var(--ink)}
"""


def ahead_css(own_radios):
    """The Ahead section follows the planner's agent radios; with no planner on the page it has its own."""
    out = [f'#ag-{a}:checked~main .v.ah{a}{{display:revert}}' for a in PARALLEL]
    if own_radios:
        out += [f'#ag-{a}:checked~main label[for=ag-{a}]{{--on:1;color:var(--ink)}}'
                f'#ag-{a}:focus-visible~main label[for=ag-{a}]{{outline:2px solid var(--focus);outline-offset:2px}}' for a in PARALLEL]
    return AHEAD_CSS + '\n'.join(out)


def ahead_html(plan, now, tz=None, switch=''):
    """The Ahead section: the plan's cards in agent lanes for each agent count in PARALLEL (the run
    ending nearest the p50), the critical chain in the accent colour, cards that wait for another one dimmed, the merge wait
    hatched, the p50 marker and the p80 buffer bar, and the diminishing-returns line. '' without a schedule."""
    e = html.escape
    if not plan or not plan.get('cards') or not plan['agents']: return ''
    cards = {c['id']: c for c in plan['cards']}; dated = 'end' in plan['agents'][PARALLEL[0]]
    notes = []
    if plan['no_depends']: notes.append('No dependencies given, order is Score only.')
    if plan['rough'] and dated: notes.append(f"Rough: {_prs(plan['n'])} measured; dates firm up as PRs merge.")
    if not dated: notes.append('No measured PRs yet: lanes in SP, no dates.')
    if plan['excluded']: notes.append(f"{len(plan['excluded'])} card{'s' if len(plan['excluded']) != 1 else ''} not planned "
                                      '(unknown, unsized or looping dependencies): see `estimate plan`.')
    if plan['maybe_done']: notes.append(f"{len(plan['maybe_done'])} of these may already be done (Tiden links merged code).")

    def one(a):
        p = plan['agents'][a]; sched = p['schedule']
        span = max([s['merged'] for s in sched] + ([p['end']['p80']] if dated else [])) or 1.0
        tracks = [[] for _ in range(a)]
        for s in sched:
            c = cards[s['id']]
            cls = ('cc ' if c['chain'] else '') + ('wt' if c['depends'] else '')
            wait = f" · waits for #{s['waits_for']}" if c['depends'] and s['waits_for'] else ''
            tracks[s['agent'] - 1].append(
                f'<div class="card {cls}" style="--l:{s["start"] / span:.4f};--w:{max(s["worked"] - s["start"], 0) / span:.4f}" '
                f'title="{e(c["title"])} · {c["sp"]} SP{" · critical chain" if c["chain"] else ""}{e(wait)}">{e(c["label"])}</div>')
            if s['merged'] - s['worked'] > 1e-9:
                tracks[s['agent'] - 1].append(f'<div class="card mw" aria-hidden="true" style="--l:{s["worked"] / span:.4f};'
                                              f'--w:{(s["merged"] - s["worked"]) / span:.4f}"></div>')
        lanes = ''.join(f'<div class="lane"><span class="who">agent {k}</span><div class="track">{"".join(t)}</div></div>'
                        for k, t in enumerate(tracks, 1))
        if dated:
            x, b = p['end']['p50'] / span, p['end']['p80'] / span
            lanes += (f'<div class="lane"><span class="who">buffer</span><div class="track buf" role="img" aria-label="project buffer '
                      f'{_num(p["buffer"] / 24)} days, p50 to p80"><i style="--l:{x:.4f};--w:{max(b - x, 0):.4f}"></i></div></div>'
                      f'<div class="mark{" end" if x > .8 else ""}" style="--x:{x:.3f}"><b>p50 · {_date(now, p["end"]["p50"], tz)}</b></div>')
            line = e(agents_line(plan, a, now, tz)) + '.'
            right = f"p80 · {_date(now, p['end']['p80'], tz)}"
        else:
            line = f'{_agents(a)}: {plan["sp"]} SP in this order.'; right = f'{_num(span)} SP of work'
        waiting = [(cards[s['id']], s['waits_for']) for s in sorted(sched, key=lambda s: s['start']) if cards[s['id']]['depends'] and s['waits_for']]
        waits = ('<p class="waits">Dimmed cards wait for another card: '
                 + ' · '.join(f"<b>{e(_title(c)[0] or c['label'])}</b> waits for #{e(w)}" for c, w in waiting[:6])
                 + (f' · +{len(waiting) - 6} more' if len(waiting) > 6 else '') + '</p>') if waiting else ''
        return (f'<div class="v ah{a}"><p class="line">{line}</p>'
                f'<div class="axis"><span>now · {_date(now, 0, tz)}</span><span>{e(right)}</span></div>'
                f'<div class="lanes">{lanes}</div>{waits}</div>')
    chain = plan['chain']
    legend = ('<p class="legend"><span><i style="background:var(--violet)"></i>critical chain</span>'
              '<span><i style="background:var(--likely)"></i>card</span><span><i style="background:var(--likely);opacity:.55"></i>waits for another card</span>'
              '<span><i style="height:5px;background:repeating-linear-gradient(135deg,var(--muted) 0 2px,transparent 2px 5px)"></i>merge wait</span>'
              + ('<span><i style="border:1px solid var(--coral)"></i>buffer to p80</span>' if dated else '')
              + (f"<span>Chain: {e(' → '.join('#' + x for x in chain['labels']))} ({chain['sp']} SP). " if chain['labels'] else '<span>')
              + f"Lanes show the simulated run that ends nearest the p50; "
              'dates are p50/p80 of all runs.</span></p>')
    return (f'<section class="ahead" aria-label="Ahead"><div class="head"><h2>Ahead · next {len(cards)} card{"s" if len(cards) != 1 else ""}</h2>{switch}</div>'
            + ''.join(one(a) for a in PARALLEL) + legend
            + ''.join(f'<p class="fine">{e(n)}</p>' for n in notes) + '</section>')


USAGE_PERIODS = (30, 90)   # the dashboard's Usage switch; 30 is checked
USAGE_CSS = """
.usage{margin-top:40px;border-top:1px solid var(--line);padding-top:14px}
.usage .head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.usage .head h2{margin:0}
.u{display:none}.usage .line{margin-top:10px;max-width:none}.usage .line small{color:var(--muted);font-size:13px;margin-left:6px}
.tiles{margin-top:22px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:20px}
.tiles .w .big{font-size:40px}
.charts{margin-top:32px;display:grid;grid-template-columns:1fr 1fr;gap:24px 48px}
.charts figure{margin:0}.charts svg{display:block;width:100%;height:120px;border-bottom:1px solid var(--line)}
.charts .axis{margin-top:6px}
.chart .s0{fill:var(--violet)}.chart .s1{fill:var(--coral)}.chart .s2{fill:var(--lime)}.chart .s3{fill:var(--warn)}.chart .s4{fill:var(--muted)}
.chart g:hover rect{opacity:.75}
.legend i.s0{background:var(--violet)}.legend i.s1{background:var(--coral)}.legend i.s2{background:var(--lime)}.legend i.s3{background:var(--warn)}.legend i.s4{background:var(--muted)}
.urow{display:grid;grid-template-columns:minmax(0,1fr) 64px 56px;gap:4px 12px;align-items:baseline;padding:8px 0;border-bottom:1px solid var(--line)}
.urow .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}.urow .s{text-align:right;font-weight:600}
.urow .p{text-align:right;color:var(--muted)}
.urow .sub{grid-column:1/-1;color:var(--faint);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.urow .bar{grid-column:1/-1;height:4px;margin-top:2px}.urow .bar i{background:var(--violet)}
.urow .flag{margin-left:.5em;font-size:11.5px;color:var(--warn);border:1px solid currentColor;border-radius:999px;padding:0 .45em;white-space:nowrap}
.controls .row>span.hint{width:auto;color:var(--faint)}
@media (max-width:720px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.tiles .w .big{font-size:32px}.charts{grid-template-columns:1fr}}
"""


def usage_css(periods):
    out = [f'#us-{d}:checked~main .u.d{d}{{display:revert}}#us-{d}:checked~main label[for=us-{d}]{{--on:1;color:var(--ink)}}'
           f'#us-{d}:focus-visible~main label[for=us-{d}]{{outline:2px solid var(--focus);outline-offset:2px}}' for d in periods]
    return USAGE_CSS + '\n'.join(out)


def usage_series(rep, by):
    """(series names, label of each) for the daily chart: both agents, or the top four models by $ and 'other'."""
    if by == 'agents': return ['claude-code', 'codex'], ['Claude Code', 'Codex']
    priced = [m['model'] for m in rep['models'] if m['usd']]
    top = priced[:4]
    return top + (['other'] if len(priced) > 4 else []), [_short_model(m) for m in top] + (['other'] if len(priced) > 4 else [])


def usage_svg(rep, by):
    """A static stacked bar chart of daily $ (no script): one bar per day, a <title> tooltip per bar."""
    e = html.escape; names, labels = usage_series(rep, by); days = rep['daily']
    top = set(names) - {'other'}
    stacks = []
    for d in days:
        vals = d[by]
        stack = [vals.get(n, 0.0) for n in names if n != 'other'] + ([sum(v for k, v in vals.items() if k not in top)] if 'other' in names else [])
        stacks.append(stack)
    peak = max((sum(s) for s in stacks), default=0)
    bw = 10; out = []
    for i, (d, stack) in enumerate(zip(days, stacks)):
        y = 100.0; rects = []
        for k, v in enumerate(stack):
            h = v / peak * 96 if peak else 0
            if h > 0: y -= h; rects.append(f'<rect class="s{k}" x="{i * bw + 1.5}" y="{y:.2f}" width="{bw - 3}" height="{h:.2f}"/>')
        tip = f"{_day(d['date'])} · {_usd(sum(stack))}" + ''.join(f' · {l} {_usd(v)}' for l, v in zip(labels, stack) if v)
        out.append(f'<g><title>{e(tip)}</title><rect x="{i * bw}" y="0" width="{bw}" height="100" fill="transparent"/>{"".join(rects)}</g>')
    busiest = max(range(len(days)), key=lambda i: sum(stacks[i])) if days else None
    aria = (f"Daily $ by {'agent' if by == 'agents' else 'model'}, last {rep['days']} days"
            + (f"; the most was {_usd(peak)} on {_day(days[busiest]['date'])}" if peak else '; no priced spend'))
    legend = ''.join(f'<span><i class="s{k}"></i>{e(l)}</span>' for k, l in enumerate(labels))
    return (f'<figure><h2>Daily $ by {"agent" if by == "agents" else "model"}</h2>'
            f'<svg class="chart" viewBox="0 0 {len(days) * bw} 100" preserveAspectRatio="none" role="img" aria-label="{e(aria)}">{"".join(out)}</svg>'
            f'<div class="axis"><span>{e(_day(days[0]["date"]))}</span><span>max {e(_usd(peak))}/day</span><span>{e(_day(days[-1]["date"]))}</span></div>'
            f'<p class="legend">{legend}</p></figure>')


def share_pct(m):
    return f"{m['share'] * 100:.0f}%"


def usage_block(rep, tz=None):
    """One period of the Usage section: the spend line, cache tiles, daily charts, repos, top sessions, models, parallelism."""
    e = html.escape; t, c, p = rep['total'], rep['cache'], rep['parallel']
    if not t['responses']:
        return f'<p class="empty">No agent responses in the last {rep["days"]} days.</p>'
    approx = f'<small>approximate: {e(_tok(t["unpriced_tokens"]))} tokens have no price</small>' if t['approximate'] else ''
    line = (f'<p class="line"><b>{e(_usd(t["usd"]))}</b> API-equivalent over {rep["days"]} days · <b>{e(_tok(t["tokens"]))}</b> tokens · '
            f'{t["sessions"]} session{"s" if t["sessions"] != 1 else ""}{approx}</p>')
    share = f"{c['cached_share'] * 100:.0f}" if c['cached_share'] is not None else '–'
    tiles = [('Processed', _tok(c['processed']), 'tokens', 'input, cache and output'),
             ('Cached input', share, '%', f"{_tok(c['cache_read'])} read from cache"),
             ('Uncached input', _tok(c['uncached_input']), 'tokens', f"incl. {_tok(c['cache_write'])} cache writes"),
             ('Output', _tok(c['output']), 'tokens', ''),
             ('Cache savings', _usd(c['savings_usd']), '', 'cached reads at the input price, minus their cost')]
    tiles_html = '<div class="tiles">' + ''.join(
        f'<div class="w"><div class="lab"><span>{e(a)}</span></div><div class="big disp">{e(v)}'
        + (f'<small>{e(unit)}</small>' if unit else '') + '</div>' + (f'<div class="sub">{e(sub)}</div>' if sub else '') + '</div>'
        for a, v, unit, sub in tiles) + '</div>'
    charts = f'<div class="charts">{usage_svg(rep, "agents")}{usage_svg(rep, "models")}</div>'
    rows = rep['repos']; shown = rows[:TOP_REPOS]; rest = rows[TOP_REPOS:]
    most = max((r['usd'] for r in rows), default=0) or 1

    def repo_row(name, usd, tokens, sessions, pps=None, prs=None):
        sub = f"{_tok(tokens)} tokens · {sessions} session{'s' if sessions != 1 else ''}" + (
            f' · {prs} PRs, {pps:.2f}/session' if pps is not None else '')
        return (f'<div class="urow"><span class="t" title="{e(name)}">{e(name)}</span><span class="s">{e(_usd(usd))}</span>'
                f'<span class="p">{usd / t["usd"] * 100 if t["usd"] else 0:.0f}%</span>'
                f'<div class="bar" aria-hidden="true"><i style="width:{usd / most * 100:.1f}%"></i></div><span class="sub">{e(sub)}</span></div>')
    repos = ''.join(repo_row(r['repo'], r['usd'], r['tokens'], r['sessions'], r['prs_per_session'], r['prs']) for r in shown)
    if rest: repos += repo_row(f'{len(rest)} more', sum(r['usd'] for r in rest), sum(r['tokens'] for r in rest), sum(r['sessions'] for r in rest))
    loc = lambda s: utc(s).astimezone(tz) if tz else utc(s).astimezone()
    sessions = ''.join(
        f'<div class="urow"><span class="t" title="{e(s["label"] or "")}">{e(s["label"] or s["repo"])}</span><span class="s">{e(_usd(s["usd"]))}</span>'
        f'<span class="p">{e(_day(loc(s["start"]).date()))}</span><span class="sub">{e(AGENT_NAMES.get(s["agent"], s["agent"]))} · '
        f'{e(_short_model(s["model"]))}' + (f' · {e(s["repo"])}' if s['label'] else '') + f' · {e(_tok(s["tokens"]))} tokens</span></div>'
        for s in rep['sessions'])
    models = ''.join(
        f'<div class="urow"><span class="t">{e(_short_model(m["model"]))}'
        + (f'<span class="flag">{"no price" if m["usd"] is None else "approximate"}</span>' if m['approximate'] else '')
        + f'</span><span class="s">{e(_usd(m["usd"])) if m["usd"] is not None else "–"}</span>'
        f'<span class="p">{share_pct(m) if m["usd"] is not None and m["share"] else "–"}</span>'
        f'<span class="sub">{e(_tok(m["tokens"]))} tokens' + (f' · {e(_tok(m["unpriced_tokens"]))} without a price' if m['approximate'] and m['usd'] is not None else '')
        + '</span></div>' for m in rep['models'])
    kv = []
    if p['slots']:
        kv.append(('Sessions at once', f"<b>{e(_num(p['median']))}</b><small>median · p80 {e(_num(p['p80']))}</small>",
                   f"<small class=\"ln\">over {p['slots']} active {SLOT_MIN}-minute slots · you usually run ~{p['usually']}</small>"))
        busiest = max(p['days'], key=lambda d: (d['peak'], d['date']))
        kv.append(('Busiest day', f"<b>{busiest['peak']}</b><small>at once</small>", f"<small class=\"ln\">{e(_day(busiest['date']))}</small>"))
    if p['prs_per_session'] is not None:
        kv.append(('Merged PRs per session', f"<b>{p['prs_per_session']:.2f}</b>",
                   f"<small class=\"ln\">{p['prs']} PRs / {p['sessions']} sessions, repos with merge history</small>"))
    par = ('<div><h2>Parallelism</h2><div class="kv">' + ''.join(f'<div>{e(k)}</div><div>{v}{n}</div>' for k, v, n in kv) + '</div></div>') if kv else '<div></div>'
    return (line + tiles_html + charts
            + f'<div class="grid"><div><h2>Spend by repo</h2>{repos}</div><div><h2>Top sessions by $</h2>{sessions}</div></div>'
            + f'<div class="grid"><div><h2>Models</h2>{models}</div>{par}</div>')


def usage_section(usage, tz=None):
    """(radios, section html, css) for the dashboard; `usage`: {days: usage_report}, {'error': True} or None (no section)."""
    if not usage: return '', '', ''
    if usage.get('error'):
        return '', ('<section class="usage" aria-label="Usage"><div class="head"><h2>Usage</h2></div>'
                    '<p class="empty">Agent logs could not be read for usage.</p></section>'), USAGE_CSS
    periods = [d for d in USAGE_PERIODS if d in usage]
    radios = ''.join(f'<input type="radio" class="radio" name="us" id="us-{d}" aria-label="Usage: last {d} days"'
                     f'{" checked" if d == periods[0] else ""}>' for d in periods)
    seg = ('<div class="seg" role="group" aria-label="Usage period" aria-hidden="true">'
           + ''.join(f'<label for="us-{d}">{d} days</label>' for d in periods) + '</div>')
    body = ''.join(f'<div class="u d{d}">{usage_block(usage[d], tz)}</div>' for d in periods)
    return radios, f'<section class="usage" aria-label="Usage"><div class="head"><h2>Usage</h2>{seg}</div>{body}</section>', usage_css(periods)


def dashboard_html(repo, rows, cal, backlog, now, source=None, unsized=(), burn=None, tz=None, windows=None, subs=(), plan=None,
                   usage=None):
    """The Teammate dashboard: how many cards a time box × agents gets done (sure, likely, maybe waterlines) with
    its agent hours, $ and tokens, the agent lanes, plan windows, the backlog in Score order, and burn and rates.
    No script: the planner is 20 precomputed states switched by CSS, "show all" is a checkbox.

    `source`: {'label': 'Tiden · Shop', 'updated': datetime or None}. `unsized`: rows without effort, listed
    last and left out of every total. `burn`: `burn_report` output, {'error': ...}, or None. `tz`: local time zone.
    `windows`: `window_report` output or None. `subs`: `subscriptions` plans; their row is hidden without them.
    `plan`: `plan_forecast` of the next AHEAD_CARDS cards for the Ahead section, or None.
    `usage`: {days: `usage_report`} for the Usage section (and the planner's "you usually run ~N"), {'error': True}, or None.
    """
    e = html.escape
    loc = now.astimezone(tz) if tz else now.astimezone()
    has = bool(cal and cal['n'])
    unsized = list(unsized or [])
    src = source or {}
    cards = len(backlog or []) + len(unsized)
    line = (f"{src.get('label') or 'Backlog'} · {cards} card{'s' if cards != 1 else ''}"
            + (f" · updated {_when(src['updated'])}" if src.get('updated') else '')) if cards else f"{src.get('label') or 'Backlog'} · no cards"
    head = (f'<header class="top"><div><b>Teammate</b> · <span class="src">{e(line)}</span></div>'
            f'<div>{loc.day} {loc:%b, %H:%M}</div></header>')
    boxes = time_boxes(now, tz); default = (boxes[0][0], 3)
    warn, rates, states, ordered, foot = [], {}, {}, [], []
    hero = timeline = hero_note = ''; pace_rows = []; lab = {}
    total = 0
    if backlog:
        backlog = [{'id': str(n + 1), 'value': None, 'score': None, 'depends': [], **i} for n, i in enumerate(backlog)]
        total = sum(i['sp'] for i in backlog)
        ordered, warn = order_backlog(backlog, unsized)
        cp = critical_path(backlog)
        sim = simulate_backlog(backlog, cal) if has else {'oversize': [i for i in backlog if i['sp'] > MAX_SP]}
        days = sim.get('days')
        conc = observed_concurrency(rows, cal) if days else None; c = conc['value'] if conc else 1.0
        one = parallel_days(days, 1, total, cp['sp'], c) if days else None
        wall = spread(cal['per_sp'].get('wall_h') or []) if has else None
        if has:
            rates = {'active': spread(cal['per_sp'].get('active_h') or []), 'usd': spread(cal['per_sp'].get('usd') or []),
                     'tok': spread(tokens_per_sp(rows, cal))}
        lab = {i['id']: i.get('label') or i['id'] for i in reversed(ordered)}
        if one and wall and not cp['cycle'] and min(one[q] for _, q in TIERS) > 0:
            rates.update(wall=wall, sp_day={q: total / one[q] for _, q in TIERS})
            plans = {}
            states = {(b[0], a): plan_state(ordered, b, a, rates, plans) for b in boxes for a in PARALLEL}
            ratio = one['hi'] / one['mid'] if one['mid'] > 0 else 0
            span = max(math.ceil(days.get('span_days') or 0), 1)
            foot.append(f"{ {'high': 'Good', 'medium': 'Fair'}.get(days.get('confidence'), 'Rough')} guess: {_prs(days['prs'])} merged "
                        f"over {span} day{'s' if span != 1 else ''}" + (f'; it can easily take {round(ratio)}× longer.' if ratio >= 1.5 else '.'))
            for a in PARALLEL:
                pd = parallel_days(days, a, total, cp['sp'], c)
                pace_rows.append((f'All {total} SP, {_agents(a)}, wall clock', f"<b>{_num(pd['mid'])} d</b>",
                                  f"<small>{_num(pd['lo'])}–{_num(pd['hi'])}{' · dependencies wait' if pd['limited'] else ''}</small>"))
        elif cp['cycle']:
            loop = ' → '.join('#' + lab.get(i, i) for i in cp['cycle'])
            hero_note = f'Cards {loop} wait for each other in a loop: remove one of their Depends links to plan.'
        elif not has: hero_note = ('No history yet: run `estimate init --last 30` to size recent merged PRs blind and seed the pace, '
                                   'or the planner appears once estimated PRs merge (`estimate record`).')
        else: hero_note = 'Not enough merge history yet to plan days.'
        if days and (short := short_history(days)): warn.append(short[:1].upper() + short[1:] + '.')
        if sim['oversize']:
            n = len(sim['oversize'])
            warn.append(f"{n} card{'s' if n != 1 else ''} over {MAX_SP} SP: split {'them' if n != 1 else 'it'} first; "
                        f"{'their' if n != 1 else 'its'} numbers are the least reliable.")
        if not states:
            hero = (f'<section class="hero" aria-label="Backlog"><div><h1 class="huge short disp">{total}<small>SP in '
                    f'{len(backlog)} card{"s" if len(backlog) != 1 else ""}</small></h1><p class="fine">{e(hero_note)}</p></div></section>')
    elif unsized:
        n = len(unsized)
        hero = (f'<section class="hero" aria-label="Backlog"><div><h1 class="huge short disp">{n}<small>card{"s need" if n != 1 else " needs"} '
                'sizing</small></h1><p class="fine">Ask your agent to “estimate the backlog” (the <code>estimate-backlog</code> skill): it sets '
                'effort and value, then the planner appears here.</p></div></section>')
    else:
        hero = ('<section class="hero" aria-label="Backlog"><div><h1 class="huge short disp">No backlog yet</h1><p class="fine">Put a Markdown table '
                '<code>| id | title | effort | value | depends |</code> in <code>BACKLOG.md</code>, or connect Tiden with '
                '<code>estimate setup</code>.</p></div></section>')

    if not has and not backlog:
        foot.append('No measured pace yet: `estimate init --last 30` sizes recent merged PRs blind and seeds it; '
                    'otherwise it starts once estimated PRs merge (`estimate record`).')

    def variants(fn, tag='span'):
        return ''.join(f'<{tag} class="v {b[0]}a{a}">{fn(states[(b[0], a)], b, a)}</{tag}>' for b in boxes for a in PARALLEL)

    def cards_html(s, b, a):
        return (f'<span data-sure="{s["sure"]["sp"]}" data-likely="{s["likely"]["sp"]}" data-maybe="{s["maybe"]["sp"]}">'
                f'{s["likely"]["cards"]}</span><small>of {len(ordered)} cards</small>')

    def answer(s, b, a):
        box = {'Today': 'the rest of today', 'Week': 'a week'}.get(b[1], b[1])
        if not s['likely']['cards']:
            return f'Not even the first card ({ordered[0]["sp"]} SP) is likely done in {e(box)} with {_agents(a)}.'
        lead = f'<b>All {total} SP</b> fit' if s['all'] else f'<b>{s["likely"]["sp"]} SP</b> likely done'
        parts = ([f'about <b>{e(_hrs(s["hours"]))}</b> of agent active time, summed'] if s['hours'] is not None else []) + \
                ([f'<b>{e(_usd(s["usd"]))}</b>'] if s['usd'] is not None else []) + \
                ([f'<b>{e(_tok(s["tok"]))}</b> tokens'] if s['tok'] is not None else [])
        return f'{lead} in {e(box)} with {_agents(a)}' + (': ' + ', '.join(parts) if parts else '') + '.'

    price = (windows or {}).get('price') or {}

    def fine(s, b, a):
        share = ''
        if s['usd'] and price.get('h5') and price.get('w7'):
            p5, p7 = s['usd'] / price['h5'], s['usd'] / price['w7']
            share = (f'That is <em>{p5:.0f}%</em> of a 5-hour window and <em>{p7:.0f}%</em> of the week. ' if p5 < 100 else
                     f'That is about <em>{p5 / 100:.1f}</em> 5-hour windows and <em>{p7:.0f}%</em> of the week. ')
        need = ('' if s['all'] else f'All {total} SP in this box would need about <em>{_agents(s["need"])}</em>. ' if s['need']
                else f'All {total} SP do not fit this box with any number of agents: a card or a chain of dependent cards is longer. ')
        sure = s['sure']['cards']
        return share + need + f'Sure: {sure} card{"s" if sure != 1 else ""} · likely: {s["likely"]["cards"]} · maybe: {s["maybe"]["cards"]}.'

    def lanes(s, b, a):
        """The schedule up to the maybe waterline, one track per agent; a track is one agent's maybe capacity."""
        if not s['maybe']['cards']: return '<p class="empty">Not even the first card fits this box at the fastest pace.</p>'
        cap = box_capacity(b, 'maybe', rates); x = min(box_capacity(b, 'likely', rates) / cap, 1)
        tracks = [[] for _ in range(a)]
        for i, (k, start, _) in enumerate(plans[a][:s['maybe']['cards']]):
            it = ordered[i]
            tracks[k].append(f'<div class="card {tier_of(i, s)}" style="--l:{start / cap:.4f};--w:{it["sp"] / cap:.4f}" '
                             f'title="{e(it["title"])} · {it["sp"]} SP · {tier_of(i, s)}">{e(_title(it)[1])}</div>')
        return (''.join(f'<div class="lane"><span class="who">agent {k}</span><div class="track">{"".join(t)}</div></div>'
                        for k, t in enumerate(tracks, 1))
                + f'<div class="mark{" end" if x > .8 else ""}" style="--x:{x:.3f}"><b>likely by here</b></div>' + reset_mark(b))

    def reset_mark(b):
        """Where the Claude 5-hour window resets inside the Today box (a track spans the whole box)."""
        w = ((windows or {}).get('claude') or {}).get('h5'); resets = w and w['resets_at']
        if b[3] != 'h' or not resets or not now < resets < now + timedelta(hours=b[4]): return ''
        x = (resets - now).total_seconds() / 3600 / b[4]
        return f'<div class="mark lo{" end" if x > .8 else ""}" style="--x:{x:.3f}"><b>5-hour window resets {resets.astimezone(tz) if tz else resets.astimezone():%H:%M}</b></div>'

    radios = ''
    try:  # the Usage section is optional: a drawing error shows its error card and leaves the rest of the page
        usual = next((u['parallel']['usually'] for d in USAGE_PERIODS if isinstance(u := (usage or {}).get(d), dict)
                      and u['parallel'].get('usually')), None)
        u_radios, u_section, u_css = usage_section(usage, tz)
    except Exception as err:  # noqa: BLE001
        print(f'estimate: the Usage section was not drawn: {type(err).__name__}', file=sys.stderr)
        usual = None; u_radios, u_section, u_css = usage_section({'error': True})
    usually = f'<span class="hint">you usually run ~{usual}</span>' if usual else ''
    if states:
        radios = (''.join(f'<input type="radio" class="radio" name="tb" id="tb-{b[0]}" aria-label="Time box: {e(b[1])}, {e(b[2])}"'
                                                   f'{" checked" if b[0] == default[0] else ""}>' for b in boxes)
                  + ''.join(f'<input type="radio" class="radio" name="ag" id="ag-{a}" aria-label="{_agents(a)}"{" checked" if a == default[1] else ""}>'
                            for a in PARALLEL))
        seg = lambda name, items: (f'<div class="seg" role="group" aria-label="{name}" aria-hidden="true">'
                                   + ''.join(f'<label for="{i}">{inner}</label>' for i, inner in items) + '</div>')
        hero = ('<section class="hero" aria-label="Planner"><div aria-live="polite">'
                f'<h1 class="huge disp">{variants(cards_html)}</h1><p class="line">{variants(answer)}</p><p class="fine">{variants(fine)}</p></div>'
                '<div class="controls">'
                f'<div class="row"><span>Time box</span>{seg("Time box", [(f"tb-{b[0]}", f"{e(b[1])}<small>{e(b[2])}</small>") for b in boxes])}</div>'
                f'<div class="row"><span>Agents</span>{seg("Agents", [(f"ag-{a}", str(a)) for a in PARALLEL])}{usually}</div></div></section>')
        axis = variants(lambda s, b, a: f'<div class="axis"><span>now · {loc:%H:%M}</span><span>{e(b[1])} · {e(b[2])}</span></div>', 'div')
        timeline = (f'<section class="tl" aria-label="Agent lanes">{axis}'
                    + ''.join(f'<div class="v {b[0]}a{a}"><div class="lanes">{lanes(states[(b[0], a)], b, a)}</div></div>' for b in boxes for a in PARALLEL)
                    + '<p class="legend"><span><i style="background:var(--sure)"></i>sure</span><span><i style="background:var(--likely)">'
                    '</i>likely</span><span><i style="background:var(--maybe)"></i>maybe</span>'
                    '<span>Lanes are an ideal schedule in Score order, dependencies first; review and merge waits are not drawn.</span></p></section>')
    ahead = ''
    if plan and plan.get('agents'):
        own = not states
        if own:
            radios += ''.join(f'<input type="radio" class="radio" name="ag" id="ag-{a}" aria-label="{_agents(a)}"{" checked" if a == default[1] else ""}>'
                              for a in PARALLEL)
        switch = ('<div class="seg" role="group" aria-label="Agents" aria-hidden="true">'
                  + ''.join(f'<label for="ag-{a}">{a}</label>' for a in PARALLEL) + '</div>' + usually) if own else ''
        ahead = ahead_html(plan, now, tz, switch)
    strip = ('<section class="strip" aria-label="Plan windows">'
             + ''.join(f'<div class="w"><div class="lab"><span>{e(label)}</span><span>{e(right)}</span></div>'
                       + (f'<div class="big disp">{pct:.0f}<small>% used</small></div>' if pct is not None else '')
                       + f'<div class="bar" role="meter" aria-label="{e(label)} used" aria-valuemin="0" aria-valuemax="100"'
                       + (f' aria-valuenow="{pct:.0f}"><i class="{"x" if label.startswith("Codex") else ""}" style="width:{min(pct, 100):.0f}%"></i>'
                          if pct is not None else '>') + '</div>' + (f'<div class="sub">{e(note)}</div>' if note else '') + '</div>'
                       for label, right, pct, note in window_cards(windows, now, tz)) + '</section>')

    def li(index, i):
        key, title = _title(i)
        tiers = ' '.join(f'k{k[0]}a{k[1]}-{t}' for k, s in states.items() if (t := tier_of(index, s)))
        wait = f'<span class="wait">waits for #{e(lab.get(i["waits_for"], i["waits_for"]))}</span>' if i.get('waits_for') else ''
        score = (f'<span class="vh">score </span>{i["score"]:.1f}' if i['score'] is not None
                 else '<span aria-hidden="true">–</span><span class="vh">no score</span>')
        return (f'<li class="{tiers}"><span class="n">{i["rank"]}</span><span class="t" title="{e(i["title"])}">{wait}'
                + (f'<span class="k">{e(key)}</span>' if key else '') + f'{e(title)}</span>'
                f'<span class="s">{score}</span><span class="p">{i["sp"]}<span class="vh"> SP</span></span></li>')
    items = [li(n, i) for n, i in enumerate(ordered)]
    if done := maybe_done_warning(backlog or [], unsized): warn.append(done[:1].upper() + done[1:] + '.')
    for u in unsized:
        key, title = _title(u)
        items.append('<li class="none"><span class="n"></span>'
                     f'<span class="t" title="{e(u["title"])} (no size yet)">' + (f'<span class="k">{e(key)}</span>' if key else '')
                     + f'{e(title)}</span><span class="s" aria-hidden="true">–</span>'
                     '<span class="p"><span aria-hidden="true">?</span><span class="vh">no size yet</span></span></li>')
    lst = ''
    if items:
        more = (f'<input type="checkbox" id="all" aria-label="Show all {len(items)} cards"><ol role="list" class="rest" start="{TOP_ROWS + 1}">'
                f'{"".join(items[TOP_ROWS:])}</ol><label for="all" class="more"><span class="off">Show all {len(items)}</span>'
                f'<span class="on">Show top {TOP_ROWS} only</span></label>' if len(items) > TOP_ROWS else '')
        lst = (f'<div class="list"><h2>Do first</h2><ol role="list">{"".join(items[:TOP_ROWS])}</ol>{more}</div>')

    kv = burn_rows(burn)
    if subs:
        monthly = sum(x['usd'] for x in subs); week = ((burn or {}).get('week') or {}).get('usd')
        kv.insert(2 if len(kv) > 1 else len(kv), ('Subscriptions', f'<b>{e(_usd(monthly))}/mo</b><small>set by you</small>',
                  f"<small class=\"ln\">{e(' + '.join(x['name'] for x in subs))}"
                  + (f' · 7 days of use ≈ {week / monthly:.1f}× the monthly price' if week else '') + '</small>'))
    tp = (cal or {}).get('throughput') or {}
    if has and tp.get('sp_per_day') is not None:
        conc = observed_concurrency(rows, cal); c = conc['value'] if conc else 1.0
        kv.append(('Pace', f"<b>{_num(tp['sp_per_day'])} SP/day</b>",
                   f"<small>{_num(tp['sp_per_day'] / c)} per agent · {_prs(cal.get('prs', cal['n']))} over {_num(tp['span_days'])} days</small>"))
    for key, label, fmt in (('active', 'Agent work per SP', _hrs), ('usd', '$ per SP', _usd), ('tok', 'Tokens per SP', _tok)):
        r = rates.get(key)
        if r: kv.append((label, f"<b>{e(fmt(r['mid']))}</b>",
                         f"<small>{'floor · ' if key == 'usd' else ''}{_kind(r)} {e(fmt(r['lo']))}–{e(fmt(r['hi']))} · {_prs(r['n'])}</small>"))
        elif has: kv.append((label, '<span class="no">not enough data</span>', ''))
    kv += pace_rows
    rates_html = ('<div><h2>Burn and rates</h2><div class="kv">'
                  + ''.join(f'<div>{e(k)}</div><div>{v}{n}</div>' for k, v, n in kv) + '</div></div>')
    grid = f'<section class="grid">{lst or "<div></div>"}{rates_html}</section>'
    if backlog or burn: foot.append('Dollars are API-equivalent list prices, not a bill: per SP they count branch work only, a floor; '
                                    'burn counts every local Claude Code and Codex log.')
    if price.get('h5') or price.get('w7'):
        foot.append(f"Window prices are rough: API-equivalent $ over the % rise in clean hours of status-line snapshots "
                    f"({price['hours']['h5']} for the 5-hour window, {price['hours']['w7']} for the week).")
    if unsized: foot.append(f"{len(unsized)} card{'s have' if len(unsized) != 1 else ' has'} no size yet and {'are' if len(unsized) != 1 else 'is'} not counted.")
    if backlog: foot.append('Score is value ÷ effort: higher goes first, dependencies before.')
    if states: foot.append(f'Agent hours are {ACTIVE_LABEL}; the “All … SP” rows are wall clock for that many agents, '
                           'never shorter than the longest dependency chain.')
    if u_radios:
        foot.append('Usage counts every local Claude Code and Codex log by repo (the git remote of each session’s folder, so '
                    'worktrees pool); labels are the first line of a session’s first prompt, hidden with --no-labels.')
    foot_html = ('<footer class="foot">' + ''.join(f'<p class="warn">{e(w)}</p>' for w in warn)
                 + ''.join(f'<p>{e(f)}</p>' for f in foot) + '</footer>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a0b12" media="(prefers-color-scheme: dark)"><meta name="theme-color" content="#f4f4fb" media="(prefers-color-scheme: light)">
<title>Teammate · {e(repo)}</title><style>{CSS}{state_css(boxes) if states else ''}{ahead_css(not states) if ahead else ''}{u_css}</style></head><body>{radios}{u_radios}<main>
{head}
{hero}
{timeline}
{ahead}
{strip}
{grid}
{u_section}
{foot_html}
</main></body></html>"""


# ---------- commands: where the data live ----------

REGISTRY = '~/.config/agent-estimate/projects.json'  # {"owner/repo": {"dir": ..., "claude_project_dir": ...}} ($ESTIMATE_REGISTRY)
DATA_FILES = ('ledger.jsonl', 'history.json', 'config.json')  # a walked-up .estimate counts only with one of these


def registry_path():
    return Path(os.environ.get('ESTIMATE_REGISTRY') or REGISTRY).expanduser()


def read_registry():
    """The per-user project registry; missing or damaged reads as empty (a damaged file is named on stderr)."""
    path = registry_path()
    if not path.exists(): return {}
    try: reg = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): reg = None
    if not isinstance(reg, dict):
        print(f'estimate: ignoring {path}: not a JSON object', file=sys.stderr); return {}
    out = {}
    for repo, v in reg.items():
        v = v if isinstance(v, dict) else {}
        cp = v.get('claude_project_dir')
        cp = [cp] if isinstance(cp, str) else cp if isinstance(cp, list) and all(isinstance(x, str) for x in cp) else None
        entry = {**({'dir': v['dir']} if isinstance(v.get('dir'), str) and v['dir'] else {}), **({'claude_project_dir': cp} if cp else {})}
        if set(entry) != {k for k in ('dir', 'claude_project_dir') if k in v}:
            print(f'estimate: {path}: ignoring damaged fields of {repo!r} (dir: a path; claude_project_dir: a path or a list)', file=sys.stderr)
        if entry: out[repo] = entry
    return out


def write_registry(repo, entry):
    """Set one project's entry (dir, optional claude_project_dir); other projects are kept."""
    reg = read_registry(); reg[repo] = entry; path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(reg, ensure_ascii=False, indent=1, sort_keys=True) + '\n')
    return path


def remote_repo(cwd=None, timeout=None):
    """owner/name from the git remote (origin, else the first remote), so every worktree of a repo agrees; None outside git.
    With `timeout`, a git call that takes longer raises subprocess.TimeoutExpired."""
    def git(*a):
        try: return subprocess.run(['git', *a], capture_output=True, text=True, check=True, cwd=cwd, timeout=timeout).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError): return ''
    names = git('remote').split()
    if not names: return None
    url = git('remote', 'get-url', 'origin' if 'origin' in names else names[0])
    # GitHub only (the tool reads PRs through gh): https/ssh URL or scp-like, exactly owner/name; never a path or another host.
    m = re.fullmatch(r'(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::\d+)?/|[^@/\s]+@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?', url)
    return f'{m.group(1)}/{m.group(2)}' if m else None


def walk_up_dir(start=None):
    """The nearest `.estimate/` at or above `start` (cwd) holding a ledger, history or config, else None."""
    here = Path(start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        cand = d / '.estimate'
        if cand.is_dir() and any((cand / f).exists() for f in DATA_FILES): return cand
    return None


def _quiet_config(d):
    """config.json of a data dir, or None when missing or damaged (the command that needs it reports that)."""
    try: return Store(d).read_json('config.json') if d else None
    except (OSError, ValueError): return None


def transcript_dir(path, claude_root):
    """(dir to use, note or None) for a --claude-project-dir value. A dir under the Claude projects root is
    kept; a checkout maps to its transcript dir there (Claude names it after the path, every character other
    than a letter or digit as '-') when that exists; anything else is kept as given, with a warning."""
    p = Path(path).expanduser().absolute(); root = Path(claude_root).expanduser().absolute()
    if p.resolve().is_relative_to(root.resolve()): return str(p), None
    for cand in dict.fromkeys((p, p.resolve())):
        enc = root / re.sub(r'[^A-Za-z0-9]', '-', str(cand))
        if enc.is_dir(): return str(enc), f'{p} is a checkout: using its transcript dir {enc}'
    return str(p), (f'warning: {p} is not a transcript dir (none under {root}); omit it, '
                  'matching by repo/branch is the default')


def resolve(args):
    """Data dir, repo and trusted Claude transcript dirs, each with where it came from.

    Data dir: --dir, $ESTIMATE_DIR, the nearest `.estimate/` above cwd, the registry entry of the git remote,
    else <git root>/.estimate. Repo: --repo, $ESTIMATE_REPO, the git remote, the data dir's config.json, `gh`.
    Claude project dirs: --claude-project-dir, $ESTIMATE_CLAUDE_PROJECT_DIR, the registry entry.
    """
    if getattr(args, '_resolved', None): return args._resolved
    remote = remote_repo(); entry = read_registry().get(args.repo or os.environ.get('ESTIMATE_REPO') or remote or '') or {}
    reg = f'registry {registry_path()}'
    if args.dir: d, d_from = args.dir, '--dir'
    elif os.environ.get('ESTIMATE_DIR'): d, d_from = os.environ['ESTIMATE_DIR'], '$ESTIMATE_DIR'
    elif found := walk_up_dir(): d, d_from = str(found), 'nearest .estimate/ above the current directory'
    elif entry.get('dir'): d, d_from = os.path.expanduser(entry['dir']), reg
    elif top := git_root(): d, d_from = os.path.join(top, '.estimate'), 'default <git root>/.estimate'
    else: d, d_from = None, None
    if args.repo: repo, r_from = args.repo, '--repo'
    elif os.environ.get('ESTIMATE_REPO'): repo, r_from = os.environ['ESTIMATE_REPO'], '$ESTIMATE_REPO'
    elif remote: repo, r_from = remote, 'git remote'
    elif isinstance(cfg := _quiet_config(d), dict) and cfg.get('repo'): repo, r_from = cfg['repo'], 'config.json'
    else: repo, r_from = None, 'gh'
    cpd, c_from = args.claude_project_dir, '--claude-project-dir'
    if not cpd and os.environ.get('ESTIMATE_CLAUDE_PROJECT_DIR'):
        cpd, c_from = [p for p in os.environ['ESTIMATE_CLAUDE_PROJECT_DIR'].split(os.pathsep) if p], '$ESTIMATE_CLAUDE_PROJECT_DIR'
    if not cpd and entry.get('claude_project_dir'): cpd, c_from = list(entry['claude_project_dir']), reg
    if not cpd: c_from = None
    mapped = [transcript_dir(p, args.claude_dir) for p in cpd or []]
    args.claude_project_dir = list(dict.fromkeys(t for t, _ in mapped))
    args._resolved = {'dir': d, 'dir_from': d_from, 'repo': repo, 'repo_from': r_from,
                      'claude_project_dir': args.claude_project_dir, 'claude_project_dir_from': c_from,
                      'claude_project_dir_notes': [n for _, n in mapped if n]}
    return args._resolved


def data_store(args):
    d = resolve(args)['dir']
    if not d: sys.exit('estimate: not in a git repo; pass --dir, set $ESTIMATE_DIR, or run `estimate link --dir D --repo owner/name`')
    return Store(d)


def ctx(args):
    r = resolve(args)
    repo = r['repo'] or detect_repo()
    now = utc(args.now) if args.now else datetime.now(timezone.utc)
    return repo, data_store(args), now


def is_empty(store):
    """No ledger, no merge history and no config.json: a wrong data dir, or one `estimate setup` never ran in.
    A dir with only config.json is a fresh, set-up project: its dashboard shows how to seed a pace."""
    return not any((store.dir / f).exists() for f in DATA_FILES)


def require_data(store, args):
    """Exit with one message when the data dir is empty, instead of printing n=0 numbers."""
    if not is_empty(store): return
    r = resolve(args)
    sys.exit(f"estimate: empty data dir {store.dir} (from {r['dir_from']}): no ledger.jsonl, history.json or config.json.\n"
             '  Point estimate at your data in one of 3 ways:\n'
             '    --dir DIR                      on this command\n'
             '    export ESTIMATE_DIR=DIR        for this shell\n'
             '    estimate link --dir DIR        once per repo; works in every worktree and agent\n'
             '  New repo? `estimate setup` first. '
             'Check with `estimate doctor`.')


def measure(prs, store, repo, args, since):
    """Actuals for `prs`. Attribution sees every known PR, so reused branches and multi-PR sessions
    are split correctly even when only some PRs are being settled."""
    sessions, links = scan_claude_logs(args.claude_dir, repo.split('/')[-1], repo, since=since,
                                     scan_all=args.claude_all, project_dirs=args.claude_project_dir)
    sessions.update(scan_codex_logs(args.codex_dir, repo, since=since))
    known = {p['number']: p for p in store.read_json('history.json', []) if p.get('createdAt') and p.get('headRefName')}
    known.update({p['number']: p for p in prs})
    for r in store.rows():
        # Superseded estimates stay start evidence, so a refresh reproduces the original settlement.
        if r.get('source') in ('retro', 'pr-body'): continue
        p = known.get(r['actual']['pr']) if r.get('actual') else match_pr(r, list(known.values()))
        if p:
            made = r['created_at']
            p['estimateStartAt'] = min(p.get('estimateStartAt') or made, made)
    previous_merge = {}
    for p in sorted(known.values(), key=lambda p: p['createdAt']):
        branch = p['headRefName']
        p.pop('previousMergedAt', None)
        if branch in previous_merge: p['previousMergedAt'] = previous_merge[branch]
        if p.get('mergedAt'): previous_merge[branch] = p['mergedAt']
    agent = attribute(list(known.values()), sessions, links, store.prices(), args.idle_min)
    if (store.dir / 'prices.json').exists():
        for a in agent.values(): a['pricing_basis'] = 'API-equivalent usage at custom prices.json rates'
    return {p['number']: pr_actual(known[p['number']], agent.get(p['number'])) for p in prs if p.get('mergedAt')}


def save_history(store, prs):
    old = {p['number']: p for p in store.read_json('history.json', [])}
    for p in prs:
        old[p['number']] = {**old.get(p['number'], {}),
                            **{k: p[k] for k in ('number', 'createdAt', 'mergedAt', 'headRefName', 'commits') if k in p}}
    store.write_json('history.json', sorted(old.values(), key=lambda p: p['number']))


def merged_times(store):
    return [utc(p['mergedAt']) for p in store.read_json('history.json', []) if p.get('mergedAt')]


def recalibrate(store, now, window, since=None):
    """`since` (a process change) persists in config.json until replaced or cleared with `--since none`."""
    since = since if since is not None else opt_utc((store.read_json('config.json') or {}).get('since'))
    cal = calibrate(store.rows(), now, window, since, merged_times(store))
    store.write_json('calibration.json', cal)
    return cal


def who(args):
    return args.agent or os.environ.get('ESTIMATE_AGENT') or detect_agent(), args.model or os.environ.get('ESTIMATE_MODEL') or 'unknown'


MECHANICAL_FILES = 50  # a PR touching at least this many files is flagged as possibly mechanical
_MECHANICAL_PATH = re.compile(
    r'(?i)(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|cargo\.lock|go\.sum|gemfile\.lock|composer\.lock)$'
    r'|(^|/)(__snapshots__|snapshots?|vendor|vendored|third_party|node_modules|dist|generated|drafts?|prototypes?)/'
    r'|\.(snap|min\.js|min\.css|svg|png|jpe?g|gif|pdf|lock)$')
_MECHANICAL_WORDS = re.compile(r'(?i)\b(snapshots?|wip|drafts?|vendor(ed|ing)?|regenerated?|lockfile)\b')
PACKET_BODY = 1500     # characters of a PR body shown in the retro packet


def mechanical_reasons(p):
    """Why a merged PR's line and file counts may overstate its work: many files, most changed lines in
    lockfiles, snapshots, drafts, vendored, generated or binary paths, or such words in the title (in the
    body they only add to another reason)."""
    reasons, files = [], p.get('files') or []
    if (p.get('changedFiles') or len(files)) >= MECHANICAL_FILES: reasons.append(f"{p.get('changedFiles') or len(files)} files")
    total = sum(f['additions'] + f['deletions'] for f in files)
    bulk = sum(f['additions'] + f['deletions'] for f in files if _MECHANICAL_PATH.search(f['path']))
    if total and bulk / total >= 0.3:
        reasons.append(f'{round(100 * bulk / total)}% of listed lines in lockfiles, snapshots, drafts, generated or binary files')
    words = lambda text: sorted({m.group(0).lower() for m in _MECHANICAL_WORDS.finditer(text or '')})
    if title := words(p.get('title')): reasons.append('title mentions ' + ', '.join(title))
    elif reasons and (body := words(p.get('body'))): reasons.append('body mentions ' + ', '.join(body))  # too common alone
    return reasons


def retro_packet_md(repo, order, sizes_path):
    """The blind sizing packet. Each PR's body is quoted with its Markdown headings escaped and closed by an
    end marker, so text from one PR's body can never read as the start of the next PR."""
    lines = [f'# Blind retro-sizing packet: {repo}, {len(order)} merged PRs', '',
             'Size each PR in Fibonacci story points (1, 2, 3, 5, 8, 13) by how much work and difficulty it',
             'took, the way you would size it before starting. Dates and durations are hidden on purpose: do not',
             'look them up (no `gh`, no `git log` dates). Use the whole scale.', '',
             f'The points are the same unit as `estimate-task` and `estimate-backlog`. Those cap a task at {MAX_SP} and',
             f'split anything bigger before work; a merged PR cannot be split, so here 13 means "more than {MAX_SP}, would',
             'have been split". Size the work, not the diff: a PR marked **Mechanical?** may owe its line count to',
             'lockfiles, snapshots, drafts or generated files.', '',
             'Each PR starts at its `## PR #n` heading and ends at `(end of PR #n)`; headings inside a body are the',
             "author's and are escaped.", '',
             f'Write `{{"<pr number>": <sp>, ...}}` to {sizes_path}, then run, with your own agent and model id:',
             f'    estimate init --apply {sizes_path} --agent <claude-code|codex|...> --model <model id>', '']
    for p in order:
        n = p['number']
        files = sorted(p.get('files') or [], key=lambda f: -(f['additions'] + f['deletions']))
        shown = ', '.join(f"{f['path']} +{f['additions']}/-{f['deletions']}" for f in files[:25])
        more = f' (+{len(files) - 25} more)' if len(files) > 25 else ''
        body = (p.get('body') or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        body = body[:PACKET_BODY] + (' …' if len(body) > PACKET_BODY else '')
        body = re.sub(r'(?m)^(\s*)(#)', r'\1\\\2', body)                      # "## What" stays text in the quote
        lines += ['---', '', f"## PR #{n}: {p['title']}",
                  f"Files ({p.get('changedFiles')}, +{p.get('additions')}/-{p.get('deletions')}): {shown}{more}",
                  f"Commits: {len(p.get('commits') or [])}"]
        if why := mechanical_reasons(p): lines.append(f"**Mechanical?** {'; '.join(why)}. Line counts may overstate the work.")
        lines += ['', f'Body of PR #{n}:', '', '> ' + (body.replace('\n', '\n> ') if body else '(empty)'), '',
                  f'(end of PR #{n})', '']
    return '\n'.join(lines)


def cmd_init(args):
    repo, store, now = ctx(args)
    if args.apply:
        raw = json.loads(Path(args.apply).read_text(encoding='utf-8'))
        sizes = {int(str(k).lstrip('#')): int(v) for k, v in raw.items()}
        bad = {k: v for k, v in sizes.items() if v not in FIB}
        if bad: sys.exit(f'estimate: sizes must be one of {FIB}: {bad}')
        packet = store.read_json('retro-packet.json')
        if not packet: sys.exit('estimate: run `estimate init` first')
        agent, model = who(args); key = f'{agent}/{model}'
        known = {r['actual']['pr'] for r in settled(store.rows()) if estimator_key(r) == key}
        prs = [p for p in packet['prs'] if p['number'] in sizes and p['number'] not in known]
        acts = measure(prs, store, repo, args, None)
        events = []
        for p in prs:
            est = new_estimate(sizes[p['number']], f"#{p['number']}", 'retro', now, agent, model)
            events += [est, {**acts[p['number']], 'kind': 'actual', 'id': est['id'], 'recorded_at': iso(now), 'provenance': 'retro'}]
        store.append(events)
        cal = recalibrate(store, now, args.window)
        print(f'estimate: {len(prs)} retro-sized PRs settled as {agent}/{model}; calibration n={cal["n"]}')
        for line in audit_lines(cal.get('audit'), store.dir / 'prices.json')[1:]: print(f'  {line}')
        missing = [n for n in sizes if n not in {p['number'] for p in packet['prs']}]
        if missing: print(f'estimate: not in the packet, ignored: {missing}')
        return
    prs = fetch_prs(repo, 'merged', max(args.history, args.last))
    save_history(store, prs)
    pick = sorted(prs, key=lambda p: p['mergedAt'], reverse=True)[:args.last]
    store.write_json('retro-packet.json', {'repo': repo, 'created_at': iso(now), 'prs': pick})
    order = pick[:]; random.Random(args.seed).shuffle(order)
    out = store.dir / 'retro-packet.md'; sizes_path = store.dir / 'retro-sizes.json'
    out.write_text(retro_packet_md(repo, order, sizes_path), encoding='utf-8')
    print(f'estimate: wrote {out} ({len(order)} PRs). Size them blind, save {sizes_path}, then run `estimate init --apply {sizes_path} --agent ... --model ...`.')


def unsettled_reasons(pending, prs, known):
    """(estimate, why it did not settle) for each pending estimate that has no PR to settle with now."""
    out = []
    for r in pending:
        p = match_pr(r, prs); n = task_pr(r['task_ref']); ref = r['task_ref']
        if p and (estimator_key(r), p['number']) in known:
            why = f"#{p['number']} already has a settled estimate by {estimator_key(r)}"
        elif p: continue
        elif n: why = f'PR #{n} is not merged yet'
        else:
            same = sorted((q for q in prs if q['headRefName'] == ref), key=lambda q: q['mergedAt'])
            made = utc(r['created_at'])
            if not same: why = f'no merged PR from branch {ref} among the fetched PRs'
            elif all(utc(q['mergedAt']) < made for q in same):
                why = f"branch {ref}: its merged PRs ({_pr_list([q['number'] for q in same])}) merged before the estimate"
            else: why = f'branch {ref}: its merged PR was opened more than 14 days before the estimate'
        out.append((r, why))
    return out


def record_report(prs, rows, pending, history, unsettled, ruled_out_sp):
    """What `record` checked, for the case where nothing settled."""
    nums = sorted(p['number'] for p in prs)
    merged = sorted(p['mergedAt'] for p in prs)
    sized = {r['actual']['pr'] for r in settled(rows)}
    lines = [f"estimate: nothing to settle. Checked {len(prs)} merged PRs (--history {history}"
             + (f"; #{nums[0]}–#{nums[-1]}, merged {merged[0][:10]} → {merged[-1][:10]}" if prs else '')
             + f') and {len(pending)} pending estimate(s).']
    page_sized = sum(1 for p in prs if p['number'] in sized)
    lines.append(f'  already settled: {page_sized}; no logged estimate and no `SP: n` line in the body: '
                 f'{len(prs) - page_sized - len(ruled_out_sp)}'
                 + (f"; SP line off the scale {FIB}: {_pr_list(ruled_out_sp)}" if ruled_out_sp else ''))
    for r, why in unsettled:
        lines.append(f"  pending {r['id']} ({r['task_ref']}, {r['sp']} SP, logged {r['created_at'][:10]}): {why}")
    if not pending:
        lines.append('  no estimates are waiting: log one before work with `estimate forecast --sp N --task <PR or branch>`')
    return lines


def print_marks(marks, rows, days):
    if not marks: return
    refs = {r['id']: r['task_ref'] for r in rows}
    print(f"  marked {len(marks)} estimate{'s' if len(marks) != 1 else ''} 'no PR' (no PR for the branch after {days:g} days; "
          f"kept in the ledger, listed by `estimate doctor`): {', '.join(refs[m['id']] for m in marks[:8])}"
          + (f' +{len(marks) - 8} more' if len(marks) > 8 else ''))


def cmd_record(args):
    """Settle pending estimates from merged PRs, and ingest merged PRs that state `SP: n` in their body."""
    repo, store, now = ctx(args)
    rows = store.rows()
    pending = pending_rows(rows)
    known = {(estimator_key(r), r['actual']['pr']) for r in settled(rows)}
    sized = {pr for _, pr in known}
    prs = fetch_prs(repo, 'merged', args.history)
    by_num = {p['number']: p for p in prs}
    refresh = {r['actual']['pr'] for r in settled(rows)} if args.refresh else set()
    tracked = refresh | {n for r in pending if (n := task_pr(r['task_ref']))}
    for n in sorted(tracked - by_num.keys()):
        p = fetch_pr(repo, n)
        if p.get('mergedAt'): by_num[n] = p
    prs = list(by_num.values())
    seen = {p['number'] for p in store.read_json('history.json', []) or []}
    new_prs = sum(1 for p in prs if p['number'] not in seen)
    save_history(store, prs)
    match = defaultdict(list)
    for r in unsettled(rows):
        p = match_pr(r, prs)
        if p and (estimator_key(r), p['number']) not in known: match[p['number']].append(r)
    body_sp = {}; off_scale = []
    for p in prs:
        if p['number'] in sized or p['number'] in match: continue
        m = SP_LINE.search(p.get('body') or '')
        if m and int(m.group(1)) in FIB: body_sp[p['number']] = int(m.group(1))
        elif m: off_scale.append(p['number'])
    todo = [by_num[n] for n in sorted(set(match) | set(body_sp) | (refresh & by_num.keys()))]
    waiting = unsettled_reasons(pending, prs, known)
    marks = no_pr_marks([r for r, _ in waiting], prs, now, args.no_pr_days)
    try: has_pr = {b for b in {m['ref'] for m in marks} if branch_has_pr(repo, b)}
    except SystemExit as err:  # marking is optional: a failed lookup must never stop settlement
        print(f'estimate: no estimates marked \'no PR\' this run: {str(err.code).splitlines()[0]}', file=sys.stderr)
        has_pr = {m['ref'] for m in marks}
    marks = [{k: v for k, v in m.items() if k != 'ref'} for m in marks if m['ref'] not in has_pr]
    if not todo:
        store.append(marks)
        cal = recalibrate(store, now, args.window)
        print('\n'.join(record_report(prs, rows, pending, args.history, waiting, sorted(off_scale))))
        print_marks(marks, rows, args.no_pr_days)
        for line in audit_lines(cal.get('audit'), store.dir / 'prices.json'): print(f'  {line}')
        return {'settled': 0, 'new_prs': new_prs, 'no_pr': len(marks), 'n': cal['n'], 'prework': cal['prework']}
    acts = measure(todo, store, repo, args, None)
    events = []
    for n, rs in match.items():
        by_est = defaultdict(list)
        for r in rs: by_est[estimator_key(r)].append(r)
        for group in by_est.values():  # one settled estimate per PR and estimator; earlier ones are superseded
            group.sort(key=lambda r: r['created_at'])
            events += [{'kind': 'superseded', 'id': r['id']} for r in group[:-1]]
            events.append({**acts[n], 'kind': 'actual', 'id': group[-1]['id'], 'recorded_at': iso(now)})
    for n, sp in body_sp.items():
        est = new_estimate(sp, f'#{n}', 'pr-body', now, 'unknown', 'unknown')
        est.update(timing='unknown', observed_at=iso(now))
        events += [est, {**acts[n], 'kind': 'actual', 'id': est['id'], 'recorded_at': iso(now)}]
    refreshed = 0
    for r in settled(rows):
        n = r['actual']['pr']
        if n in refresh and n in acts:
            events.append({**acts[n], 'kind': 'actual', 'id': r['id'], 'recorded_at': iso(now),
                           'provenance': r['actual'].get('provenance', 'auto'), 'refresh': True})
            refreshed += 1
    store.append(events + marks)
    cal = recalibrate(store, now, args.window)
    print(f'estimate: settled estimates on {len(match)} PRs, ingested {len(body_sp)} PRs with an SP line, '
          f'refreshed {refreshed} actuals; calibration n={cal["n"]}')
    for r, why in waiting: print(f"  still pending {r['id']} ({r['task_ref']}, {r['sp']} SP): {why}")
    print_marks(marks, rows, args.no_pr_days)
    for line in audit_lines(cal.get('audit'), store.dir / 'prices.json'): print(f'  {line}')
    return {'settled': len(match) + len(body_sp), 'new_prs': new_prs, 'no_pr': len(marks), 'n': cal['n'], 'prework': cal['prework']}


def cmd_calibrate(args):
    repo, store, now = ctx(args)
    if args.since:
        cfg = store.read_json('config.json') or {}
        try: cfg['since'] = None if args.since.lower() == 'none' else iso(utc(args.since))
        except ValueError: sys.exit(f'estimate: --since needs an ISO date such as 2026-10-19, or none: {args.since!r}')
        store.write_json('config.json', cfg)
    cal = recalibrate(store, now, args.window)
    if args.check:
        if cal['n'] == 0: print('estimate: no settled PRs in the window yet; estimates still get logged and measured'); sys.exit(1)
        if cal['n'] < MIN_INTERVAL_N: print(f"estimate: only {cal['n']} settled PRs in the window: quotes are low confidence"); sys.exit(1)
        print(f"estimate: ok, n={cal['n']} in the last {cal['window']['days']:g} d"); return
    if args.json: print(json.dumps(cal, indent=1)); return
    tp = cal['throughput'] or {}
    spd = tp.get('sp_per_day')
    print(f"calibration {cal['id']}: n={cal['n']}, window {cal['window']['days']:g} d, "
          f"{'under 1 day of throughput' if spd is None else f'{spd} SP/day'}"
          + (f", coverage {tp['coverage']:.0%}" if tp.get('coverage') is not None else ''))
    for m, d in cal['drift'].items(): print(f"  drift {m}: ×{d['ratio']}{'  FLAG' if d['flag'] else ''}")
    for g, s in cal['groups'].items():
        c = s['consistency']
        print(f"  {g}: n={s['n']}, pre-work {s.get('prework', 0)}/{ESTIMATOR_MIN_N}, spread wall ×{c['wall_h'] or '–'}, agent ×{c['active_h'] or '–'}")
    for sp in (1, 2, 3, 5, 8): print('  ' + quote_line(quote(cal, sp, now)))
    for line in audit_lines(cal.get('audit'), store.dir / 'prices.json'): print(f'  {line}')


def maybe_done_warning(items, unsized=()):
    """One line naming backlog items (sized or not) whose note says Tiden links merged code to them, or None."""
    flagged = [i for i in list(items) + list(unsized) if maybe_done(i)]
    if not flagged: return None
    n, counted = len(flagged), sum(maybe_done(i) for i in items)
    held = ('still counted' if counted == n else 'not sized, not counted' if not counted else f'{counted} of them still counted')
    return (f"{n} card{'s' if n != 1 else ''} may already be done (Tiden links merged code, status still open), "
            f"{held}: {_short_list(['#' + (i.get('label') or i['id']) for i in flagged])}; close {'them' if n != 1 else 'it'} in Tiden and pull again")


def order_marks(item, label):
    """`  unblocks #a, #b  waits for #c  maybe done` for one ordered backlog item; ids shown by their labels."""
    out = ''
    if item.get('unblocks'): out += '  unblocks ' + ', '.join('#' + label.get(x, x) for x in item['unblocks'])
    if item.get('waits_for'): out += f"  waits for #{label.get(item['waits_for'], item['waits_for'])}"
    if maybe_done(item): out += '  maybe done'
    return out


def cmd_forecast(args):
    repo, store, now = ctx(args)
    if args.backlog or not args.task: require_data(store, args)   # logging with --task starts a fresh ledger
    cal = recalibrate(store, now, args.window)
    if args.backlog:
        path = backlog_path(store, args.backlog)
        unsized = [] if args.skip_unsized else None
        try: items = parse_backlog(path.read_text(encoding='utf-8'), unsized)
        except ValueError as err: sys.exit(f'estimate: {path}: {err}' + ('' if 'size it first' not in str(err) else
                                          ' (or pass --skip-unsized to leave unsized items out of the totals)'))
        unsized = unsized or []
        sim = simulate_backlog(items, cal, seed=args.seed)
        ordered, warnings = order_backlog(items, unsized)
        if done := maybe_done_warning(items, unsized): warnings.insert(0, done)
        if unsized:
            warnings.insert(0, f"skipped {len(unsized)} unsized item{'s' if len(unsized) > 1 else ''}, not in the totals "
                               f"or the order: {', '.join('#' + u['label'] for u in unsized)}")
        if args.json:
            basis, wait = calendar_basis(sim)
            print(json.dumps({**sim, 'basis': basis, 'waits_warning': wait, 'short_history': short_history(sim.get('days')),
                              'audit': cal.get('audit'),
                              'order': ordered, 'unsized': unsized, 'warnings': warnings}, indent=1)); return
        print(f"backlog: {sim['items']} items, {sim['sp']} SP (calibration n={cal['n']}, window {cal['window']['days']:g} d)")
        if unsized: print(f'  warning: {warnings[0]}')
        for m, label, formatter in (('active_h', f'{ACTIVE_LABEL}:', fmt_h), ('usd', 'API-equivalent cost', fmt_usd)):
            if sim.get(m):
                v = sim[m]
                print(f"  {label} {formatter(v['lo'], v['hi'])} (median {v['mid']:.2f}; n={v['n']}, {v['confidence']}, {v['kind']}; {v['basis']})")
        if sim.get('days'):
            d = sim['days']
            conc = observed_concurrency(store.rows(), cal)
            print(f"  calendar     {_num(d['lo'])}–{_num(d['hi'])} d (median {_num(d['mid'])} d; "
                  f"{d['n']} days, {d['prs']} PRs; {d['confidence']}, {d['kind']}"
                  + (f"; this repo's pace with ≈{conc['value']:.1f} PRs in flight" if conc and conc['value'] > 1.05 else '') + ')')
            for line in wall_clock_lines(items, d, conc): print(f'  {line}')
            if short := short_history(d): print(f'  warning: {short}')
        basis, wait = calendar_basis(sim)
        if basis: print(f'  based on: {basis}')
        if wait: print(f'  warning: {wait[0].lower() + wait[1:]}')
        if line := audit_summary(cal.get('audit')): print(f'  {line}')
        for i in sim['oversize']: print(f"  split first: {i['sp']} SP {i['title']}")
        print('order (Score = Value / Effort, dependencies first):')
        label = {i['id']: i['label'] for i in reversed(ordered)}
        w = max(len(i['label']) for i in ordered) + 1 if ordered else 2
        for i in ordered:
            score = f"{i['score']:.2f}" if i['score'] is not None else '   –'
            value = i['value'] if i['value'] is not None else '–'
            key = i.get('key') or ''
            title = f"{key} · {i['title']}" if key and key != i['label'] and not i['title'].startswith(key) else i['title']
            print(f"  {i['rank']:>2}. {'#' + i['label']:<{w}}  {score}  V{value} E{i['sp']}  {title}{order_marks(i, label)}")
        for msg in warnings[1 if unsized else 0:]: print(f'  warning: {msg}')
        return
    if args.sp is None: sys.exit('estimate: pass --sp N or --backlog FILE')
    if args.sp not in FIB: sys.exit(f'estimate: SP must be one of {FIB}')
    if args.sp > MAX_SP:
        print(f'{args.sp} SP: split into parts of at most {MAX_SP} SP before work, then forecast each part'); sys.exit(2)
    agent, model = who(args); key = f'{agent}/{model}'
    closest = int(str(args.closest).lstrip('#')) if args.closest else None
    q = quote(cal, args.sp, now, key); line = quote_line(q, closest)
    if args.task:
        store.append([new_estimate(args.sp, args.task, 'agent', now, agent, model, closest, q, cal['id'])])
    print(json.dumps({'line': line, 'estimator': key, **q, 'audit': cal.get('audit')}, indent=1) if args.json else line)
    # The quote stays one line on stdout; why its n is what it is goes to stderr.
    if not args.json and (note := audit_summary(cal.get('audit'))): print(note, file=sys.stderr)


def cmd_plan(args):
    repo, store, now = ctx(args)
    require_data(store, args)
    if args.agents < 1: sys.exit('estimate: --agents must be at least 1')
    if args.top is not None and args.top < 1: sys.exit('estimate: --top must be at least 1')
    cal = recalibrate(store, now, args.window)
    path = backlog_path(store, args.backlog)
    text = path.read_text(encoding='utf-8'); unsized = []
    try: items = parse_backlog(text, unsized)
    except ValueError as err: sys.exit(f'estimate: {path}: {err}')
    caps, problems = lane_caps(text, _quiet_config(store.dir))
    for msg in problems: print(f'estimate: {msg}', file=sys.stderr)
    plan = plan_forecast(items, unsized, store.rows(), cal, commit_times(store), caps, args.agents, args.top, seed=args.seed)
    if args.json:
        print(json.dumps({**plan, 'now': iso(now), 'audit': cal.get('audit')}, indent=1)); return
    for line in plan_lines(plan, now): print(line)


def backlog_path(store, arg):
    """FILE from `--backlog FILE`, else the configured source's file (it must exist)."""
    if arg is not CONFIGURED: return Path(arg)
    try: cfg = backlog_config(store)
    except ValueError as err: sys.exit(f'estimate: {err}')
    if not cfg['path'].exists():
        hint = 'run `estimate backlog pull` first' if cfg['source'] == 'tiden' else \
            'create it, pass --backlog FILE, or set backlog.path in .estimate/config.json'
        sys.exit(f"estimate: no backlog at {cfg['path']}: {hint}")
    return cfg['path']


def write_atomic(path, text):
    """Replace `path` via a unique temp file in its directory; the temp file is removed on failure."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh: fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise


def cmd_backlog_pull(args):
    store = data_store(args)
    try: cfg = backlog_config(store)
    except ValueError as err: sys.exit(f'estimate: {err}')
    if cfg['source'] != 'tiden':
        sys.exit(f"estimate: the backlog source is a Markdown file ({cfg['path']}); pull reads Tiden. "
                 'Set {"backlog": {"source": "tiden"}} in .estimate/config.json')
    top = git_root()
    if not cfg['product_id'] and (not top or store.dir.resolve() != (Path(top) / '.estimate').resolve()):
        # Without a product id, tiden lists the product of the current checkout, which a custom data dir
        # (or --repo) does not select: the pull could overwrite this backlog with another product's cards.
        sys.exit(f'estimate: {store.dir} is not the .estimate directory of the current checkout ({top or "not a git repo"}). '
                 f'Set backlog.product_id in {store.dir / "config.json"}, or run the pull from the target repo without --dir.')
    try: specs = run_tiden(cfg['product_id'], cwd=top)
    except RuntimeError as err: sys.exit(f'estimate: {err}')
    path = cfg['path']; existing = path.read_text(encoding='utf-8') if path.exists() else ''
    product = tiden_product(cfg['product_id'], specs, cwd=top)
    text, notes = pull_tiden(specs, existing, cfg['product_id'], utc(args.now) if args.now else datetime.now(timezone.utc),
                             cfg['statuses'], product['name'], product['code'])
    store.ensure(); write_atomic(path, text)
    cards = sum(bool(re.fullmatch(_UUID, i.lower())) for i in _local_fields(text))  # hand-written rows are not cards
    print(f'estimate: wrote {path}: {cards} open card(s) of {len(specs)} specs')
    for msg in notes: print(f'  note: {msg}')


def cmd_refs(args):
    repo, store, now = ctx(args)
    for r in refs(store.rows(), args.sp, [p for p in (args.paths or '').split(',') if p], args.limit):
        a = r['actual']
        print(f"#{a['pr']} {r['sp']} SP {a['title']} — {a.get('files') or 0} files, {a.get('lines') or 0} lines; {', '.join(a.get('areas') or [])}")


def cmd_backtest(args):
    repo, store, now = ctx(args)
    since = opt_utc((store.read_json('config.json') or {}).get('since'))
    print(json.dumps(backtest(store.rows(), args.window, args.include_retro, since=since), indent=1))


def cmd_compare(args):
    stores = [Store(d) for d in args.dirs]
    missing = [str(s.ledger) for s in stores if not s.ledger.exists()]
    if missing: sys.exit(f'estimate: no ledger in: {", ".join(missing)}')
    summaries = [project_summary(s.rows(), project_label(s)) for s in stores]
    print(json.dumps(summaries, indent=1) if args.json else compare_text(summaries))


def dashboard_backlog_path(store):
    """The configured backlog (backlog_config: git-root-relative Markdown, or the pulled Tiden table).
    Without a configured backlog and without BACKLOG.md, the legacy .estimate/backlog.txt. The file may not exist."""
    path = backlog_config(store)['path']
    legacy = store.dir / 'backlog.txt'
    cfg = store.read_json('config.json'); configured = isinstance(cfg, dict) and cfg.get('backlog')
    return legacy if not configured and not path.exists() and legacy.exists() else path


def dashboard_source(store, bpath, text, repo):
    """The dashboard's source line: a pulled Tiden table names Tiden and the product (never the local file),
    with the pull time; any other backlog file names itself, with its modification time."""
    try: cfg = backlog_config(store)
    except ValueError: cfg = {}
    if cfg.get('source') == 'tiden' and text is not None and bpath.resolve() == cfg['path'].resolve():
        name, pulled = pulled_source(text)
        return {'label': f"Tiden · {name or repo.split('/')[-1]}", 'updated': pulled}
    updated = datetime.fromtimestamp(bpath.stat().st_mtime, timezone.utc) if text is not None else None
    return {'label': bpath.name, 'updated': updated}


def cmd_dashboard(args):
    repo, store, now = ctx(args)
    require_data(store, args)
    cal = recalibrate(store, now, args.window)
    try: bpath = Path(args.backlog) if args.backlog else dashboard_backlog_path(store)
    except ValueError as err: sys.exit(f'estimate: {err}')
    text = bpath.read_text(encoding='utf-8') if bpath.exists() else None
    unsized = []
    try: backlog = parse_backlog(text, unsized) if text is not None else None
    except ValueError as err: sys.exit(f'estimate: {bpath}: {err}')
    out = Path(args.out) if args.out else store.dir / 'dashboard.html'
    burn = windows = usage = None; recs, limits = [], []
    if not args.no_burn:
        # Unreadable or odd agent logs must never stop the dashboard: the burn rows say so instead.
        # One scan of the longest Usage period feeds the burn rows, the window prices and the Usage section.
        labels = labels_on(args, _quiet_config(store.dir)); meta = {}
        try:
            recs = scan_burn(args.claude_dir, args.codex_dir, now - timedelta(days=max(USAGE_PERIODS)), store.prices(), limits, meta, labels)
            burn = burn_report(recs, now, idle_min=args.idle_min)
        except Exception as err:  # noqa: BLE001
            print(f'estimate: agent logs not read for the burn rows: {type(err).__name__}', file=sys.stderr); burn = {'error': True}; recs = []
        if not burn.get('error'):
            try:  # the Usage section is optional: a failure there leaves the rest of the page as it was
                reg = read_registry(); repos = session_repos(meta, reg); merged = merged_by_repo(reg, [(repo, str(store.dir))])
                usage = {d: usage_report(recs, meta, repos, now, d, store.prices(), idle_min=args.idle_min, labels=labels, merged=merged)
                         for d in USAGE_PERIODS}
            except Exception as err:  # noqa: BLE001
                print(f'estimate: the Usage section was not built: {type(err).__name__}', file=sys.stderr); usage = {'error': True}
    try: windows = window_report(read_snapshots(now), recs, limits[0] if limits else None, now)
    except Exception as err:  # noqa: BLE001 - window snapshots are optional; the cards then say "no snapshot yet"
        print(f'estimate: plan-window snapshots not read: {type(err).__name__}', file=sys.stderr)
    subs, problem = subscriptions(store.read_json('config.json'))
    if problem: print(f'estimate: {problem}', file=sys.stderr)
    plan = None
    if backlog:
        try:  # the Ahead section is optional: a planning error must never stop the dashboard
            caps, problems = lane_caps(text, _quiet_config(store.dir))
            for msg in problems: print(f'estimate: {msg}', file=sys.stderr)
            plan = plan_forecast(backlog, unsized, store.rows(), cal, commit_times(store), caps, top=AHEAD_CARDS)
        except Exception as err:  # noqa: BLE001
            print(f'estimate: the Ahead plan was not built: {type(err).__name__}', file=sys.stderr)
    store.ensure(); out.write_text(dashboard_html(repo, store.rows(), cal, backlog, now, dashboard_source(store, bpath, text, repo), unsized,
                                                  burn, windows=windows, subs=subs, plan=plan, usage=usage), encoding='utf-8')
    print(f'estimate: wrote {out}')
    return {'out': str(out), 'n': cal['n'], 'prework': cal['prework']}


def all_line(cmd, res):
    if cmd == 'record':
        return (f"settled {res['settled']}, new PRs {res['new_prs']}, calibration n={res['n']}, pre-work n={res['prework']}"
                + (f", marked no PR {res['no_pr']}" if res['no_pr'] else ''))
    return f"wrote {res['out']}, calibration n={res['n']}, pre-work n={res['prework']}"


def run_all(args):
    """`record --all` / `dashboard --all`: every project in the registry, one line each. A project's failure is
    reported and the loop goes on; the exit status is non-zero only when every project failed."""
    if args.dir or args.repo or getattr(args, 'out', None) or getattr(args, 'backlog', None):
        sys.exit('estimate: --all reads every project from the registry; drop --dir, --repo, --out and --backlog')
    reg = {repo: e for repo, e in read_registry().items() if e.get('dir')}
    if not reg: sys.exit(f'estimate: no projects with a data dir in {registry_path()} (`estimate link --dir D` in each repo)')
    failed = 0
    # Each project's transcript dirs come from its own registry entry, never from this shell's environment.
    os.environ.pop('ESTIMATE_CLAUDE_PROJECT_DIR', None)
    # One clock and one log scan for the whole run: every project reads the same transcripts (`scan_burn` cache).
    now = args.now or iso(datetime.now(timezone.utc))
    global _SCAN_CACHE
    def shows_labels(entry):
        try: return labels_on(args, _quiet_config(os.path.expanduser(entry['dir'])))
        except Exception: return True  # noqa: BLE001 - an unreadable config is reported by that project's own run
    _SCAN_CACHE = {'labels': any(shows_labels(e) for e in reg.values())}
    try:
        for repo, entry in sorted(reg.items()):
            one = argparse.Namespace(**{**vars(args), 'all': False, 'repo': repo, 'dir': os.path.expanduser(entry['dir']),
                                        'claude_project_dir': None, '_resolved': None, 'now': now})
            try:
                with contextlib.redirect_stdout(io.StringIO()): res = args.f(one)
                print(f'{repo}: {all_line(args.cmd, res)}')
            except (Exception, SystemExit) as err:  # noqa: BLE001 - one project's failure must not stop the others
                failed += 1
                msg = err.code if isinstance(err, SystemExit) else f'{type(err).__name__}: {err}'
                print(f"{repo}: FAILED {str(msg).splitlines()[0] if msg else 'exit'}")
    finally:
        _SCAN_CACHE = None
    if failed == len(reg): sys.exit(1)


def gh_auth_ok():
    """(ok, detail) from `gh auth status`; read-only."""
    try: r = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True, check=False, timeout=20)
    except FileNotFoundError: return False, 'gh not found (https://cli.github.com)'
    except subprocess.TimeoutExpired: return False, 'gh auth status timed out'
    return (True, 'ok') if r.returncode == 0 else (False, 'not logged in: run `gh auth login`')


def statusline_installed():
    try: cfg = json.loads(_settings_file().read_text(encoding='utf-8'))
    except (OSError, ValueError): return False
    line = cfg.get('statusLine') if isinstance(cfg, dict) else None
    return isinstance(line, dict) and _ours(line.get('command'))


def cmd_doctor(args):
    """Which data dir, repo and Claude transcript dirs this command line resolves to, and from where; what is in
    the data dir; gh auth; the status-line hook; the full sample audit. Reads only: writes nothing."""
    r = resolve(args)
    repo = r['repo']; r_from = r['repo_from']
    if not repo:
        try: repo = detect_repo()
        except SystemExit: repo, r_from = None, 'not found (pass --repo or set $ESTIMATE_REPO)'
    d = r['dir']
    print(f"data dir     {d or 'none'}  (from {r['dir_from'] or 'nothing: not in a git repo'})")
    print(f"repo         {repo or 'none'}  (from {r_from})")
    cpd = r['claude_project_dir']
    print(f"claude dirs  {os.pathsep.join(cpd) if cpd else 'none extra'}  (from {r['claude_project_dir_from'] or 'default: scan ' + args.claude_dir + ' by repo name'})")
    for note in r['claude_project_dir_notes']: print(f'  {note}')
    reg = read_registry()
    print(f"registry     {registry_path()}: " + (f"{len(reg)} project{'s' if len(reg) != 1 else ''}"
          + (f", {repo} linked" if repo in reg else f", {repo or 'this repo'} not linked") if reg else 'none (`estimate link --dir D`)'))
    cal = None
    if d:
        store = Store(d)
        if is_empty(store):
            print(f'ledger       empty: no ledger.jsonl, history.json or config.json in {d}')
        else:
            rows = store.rows(); done = settled(rows)
            pending = pending_rows(rows); no_pr = [x for x in unsettled(rows) if x.get('no_pr')]
            print(f"ledger       {len(rows)} estimates ({len(done)} settled, {len(pending)} pending"
                  + (f', {len(no_pr)} no PR' if no_pr else '') + f"); history {len(store.read_json('history.json', []) or [])} merged PRs")
            if no_pr:
                print(f"no PR        {', '.join(str(x['task_ref']) for x in no_pr)} (`record` found no PR for the branch after "
                      f"--no-pr-days, default {NO_PR_DAYS}; a PR merged later still settles them)")
            now = utc(args.now) if args.now else datetime.now(timezone.utc)
            since = opt_utc((store.read_json('config.json') or {}).get('since'))
            cal = calibrate(rows, now, args.window, since, merged_times(store))
            print(f"calibration  n={cal['n']}, window {cal['window']['days']:g} d")
    ok, detail = gh_auth_ok()
    print(f'gh auth      {detail}')
    print(f"status line  {'installed: logs plan windows' if statusline_installed() else 'not installed (`estimate install --statusline`, optional)'}")
    if cal:
        for line in audit_lines(cal.get('audit'), Path(d) / 'prices.json'): print(f'  {line}')


def cmd_link(args):
    """Register a data dir for a repo in the per-user registry, so every worktree and agent of it finds the data."""
    repo = args.repo or os.environ.get('ESTIMATE_REPO') or remote_repo()
    if not repo: sys.exit('estimate: no git remote here; pass --repo owner/name')
    if not args.dir: sys.exit('estimate: pass --dir DIR (the data directory to use for this repo)')
    d = Path(args.dir).expanduser().resolve()
    if not d.is_dir(): sys.exit(f'estimate: {d} is not a directory')
    entry = {'dir': str(d)}
    if args.claude_project_dir:
        mapped = [transcript_dir(p, args.claude_dir) for p in args.claude_project_dir]
        for _, note in mapped:
            if note: print(f'estimate: {note}', file=sys.stderr)
        cp = list(dict.fromkeys(t for t, _ in mapped))
        entry['claude_project_dir'] = cp[0] if len(cp) == 1 else cp
    try: path = write_registry(repo, entry)
    except OSError as err: sys.exit(f'estimate: could not write {registry_path()}: {err.strerror or err}')
    print(f'estimate: linked {repo} -> {d} in {path}' + ('' if not is_empty(Store(d)) else
          ' (note: it has no ledger or history yet)'))


# ---------- install / setup ----------

REPO_ROOT = Path(__file__).resolve().parent
SKILL_HOMES = ('.claude/skills', '.codex/skills', '.agents/skills')


def _link_state(link, target):
    """missing | ok (already points at target) | other (a symlink elsewhere) | file (not a symlink)."""
    if not os.path.lexists(link): return 'missing'
    if not os.path.islink(link): return 'file'
    return 'ok' if os.path.realpath(link) == os.path.realpath(target) else 'other'


def _place(link, target, dry):
    link = Path(link)
    state = _link_state(link, target)
    if state == 'ok': print(f'  ok       {link} (already linked)')
    elif state == 'missing':
        if not dry:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
        print(f'  {"would link" if dry else "linked"} {link} -> {target}')
    else:
        what = 'is not a symlink' if state == 'file' else f'points to {os.readlink(link)}'
        print(f'  warning: skipped {link}: it {what}; remove it yourself to link this repo', file=sys.stderr)


def _skill_dirs():
    return sorted(d for d in (REPO_ROOT / 'skills').iterdir() if d.is_dir())


def _bin_dir(args):
    return Path(os.path.expanduser(args.bin_dir))


def _agent_skill_homes():
    return [Path.home() / h for h in SKILL_HOMES]


def cmd_install(args):
    dry = args.dry_run; bin_dir = _bin_dir(args)
    print(f'estimate install{" (dry run)" if dry else ""}: {REPO_ROOT}')
    _place(bin_dir / 'estimate', REPO_ROOT / 'estimate.py', dry)
    for home in _agent_skill_homes():
        if not home.is_dir(): print(f'  skipped  {home} (not found)'); continue
        for skill in _skill_dirs(): _place(home / skill.name, skill, dry)
    on_path = [os.path.realpath(os.path.expanduser(d)) for d in os.environ.get('PATH', '').split(os.pathsep) if d]
    if os.path.realpath(bin_dir) not in on_path:
        print(f'  warning: {bin_dir} is not on PATH; add `export PATH="{bin_dir}:$PATH"` to your shell profile', file=sys.stderr)
    try:
        gh = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True, check=False)
        if gh.returncode == 0: print('  gh: logged in')
        else: print('  warning: gh is not logged in; run `gh auth login`', file=sys.stderr)
    except FileNotFoundError:
        print('  warning: the GitHub CLI `gh` was not found (https://cli.github.com)', file=sys.stderr)
    launcher = Path(os.path.abspath(bin_dir / 'estimate'))
    if args.statusline and (_link_state(launcher, REPO_ROOT / 'estimate.py') == 'ok' or (dry and not os.path.lexists(launcher))):
        statusline_install(launcher, dry)
    elif args.statusline:
        print(f'  warning: status line not changed: {launcher} is not this checkout\'s `estimate`', file=sys.stderr)
    else: print('  plan windows: `estimate install --statusline` logs Claude 5-hour and weekly % from your status line')
    print('next: run `estimate setup` inside a project repo')


def cmd_uninstall(args):
    dry = args.dry_run
    print(f'estimate uninstall{" (dry run)" if dry else ""}: {REPO_ROOT}')
    # The status line goes back first: while it still calls `estimate`, the launcher must stay.
    slots = [(_bin_dir(args) / 'estimate', REPO_ROOT / 'estimate.py')] if statusline_uninstall(dry) else []
    slots += [(h / s.name, s) for h in _agent_skill_homes() for s in _skill_dirs()]
    for link, target in slots:
        if not os.path.islink(link): continue
        if _link_state(link, target) != 'ok':
            print(f'  warning: kept {link}: it does not point to {target}', file=sys.stderr); continue
        if not dry: link.unlink()
        print(f'  {"would remove" if dry else "removed"} {link}')


def _ask(question, default=None):
    try: answer = input(f'{question}{f" [{default}]" if default else ""} ').strip()
    except EOFError: sys.exit('estimate: no terminal to ask; pass --source (and --path or --product-id)')
    return answer or default


BACKLOG_TEMPLATE = """# Backlog

Effort: Fibonacci story points 1, 2, 3, 5, 8 (13+ must be split). Value: 1, 2, 3, 5, 8.
Depends: comma-separated ids that must be done first; leave empty when unsure.

| id | title | effort | value | depends |
|----|-------|--------|-------|---------|
"""


def cmd_setup(args):
    root = git_root(); r = resolve(args)
    if not r['dir']: sys.exit('estimate: not in a git repo; run setup inside your project or pass --dir')
    top = Path(root or Path.cwd())
    store = Store(r['dir'])
    try: current = backlog_config(store)
    except ValueError as err: sys.exit(f'estimate: {err}')
    cfg = store.read_json('config.json') or {}
    old = {'source': current['source']}
    if current['source'] == 'tiden': old['product_id'] = current['product_id']
    else:
        try: old['path'] = str(current['path'].relative_to(top))
        except ValueError: old['path'] = str(current['path'])
    source = args.source or ('markdown' if args.path else 'tiden' if args.product_id else None)
    interactive = source is None
    if interactive:
        print('Where is your backlog?\n  1) a Markdown file\n  2) Tiden')
        choice = _ask('Choose 1 or 2:', '2' if old['source'] == 'tiden' else '1')
        if choice not in ('1', '2'): sys.exit(f'estimate: choose 1 or 2, got {choice!r}')
        source = 'markdown' if choice == '1' else 'tiden'
    if source == 'markdown' and args.product_id: sys.exit('estimate: --product-id only applies to --source tiden')
    if source == 'tiden' and args.path: sys.exit('estimate: --path only applies to --source markdown')
    kept = {k: v for k, v in old.items() if v} if old['source'] == source else {}  # values for options not passed carry over
    if source == 'markdown':
        fallback = kept.get('path') or 'BACKLOG.md'
        path = args.path or (_ask('Backlog file path?', fallback) if interactive else fallback)
        backlog = {**kept, 'source': 'markdown', 'path': path}
        file = top / os.path.expanduser(path)
        if not file.exists():
            create = args.create_backlog or (interactive and _ask(f'{path} does not exist. Create it with an empty table? [y/N]', 'n').lower() in ('y', 'yes'))
            if create:
                file.parent.mkdir(parents=True, exist_ok=True); file.write_text(BACKLOG_TEMPLATE, encoding='utf-8')
                print(f'created {file}')
            else:
                print(f'note: {file} does not exist yet; create it or rerun setup with --create-backlog', file=sys.stderr)
    else:
        fallback = kept.get('product_id')
        product = args.product_id or (_ask('Tiden product id? (Enter to skip)', fallback) if interactive else fallback)
        backlog = {**kept, 'source': 'tiden'}
        if product: backlog['product_id'] = product
    cfg['backlog'] = backlog
    store.write_json('config.json', cfg)
    print(f'wrote {store.dir / "config.json"}: {json.dumps(backlog)}')
    if repo := args.repo or os.environ.get('ESTIMATE_REPO') or remote_repo():
        entry = {**read_registry().get(repo, {}), 'dir': str(store.dir.expanduser().resolve())}
        if r['claude_project_dir']: entry['claude_project_dir'] = r['claude_project_dir']
        try: print(f'linked {repo} -> {entry["dir"]} in {write_registry(repo, entry)} (every worktree of {repo} finds it)')
        except OSError as err:   # the registry is optional: setup's own config is already written
            print(f'note: could not write {registry_path()} ({err.strerror or err}); run `estimate link --dir {entry["dir"]}` later', file=sys.stderr)
    print('next steps:\n'
          '  1. ask your agent: "estimate the backlog" (skill estimate-backlog)\n'
          + ('  2. estimate backlog pull\n  3. estimate forecast --backlog\n  4. estimate dashboard' if source == 'tiden' else
             '  2. estimate forecast --backlog\n  3. estimate dashboard'))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='estimate', description='Fibonacci story points for coding agents, measured per estimator.')
    ap.add_argument('--version', action='version', version=__version__)
    ap.add_argument('--repo', help='owner/name (default: the current repo via gh)')
    ap.add_argument('--dir', help='data directory (default: <git root>/.estimate, or $ESTIMATE_DIR)')
    ap.add_argument('--claude-dir', default=os.environ.get('ESTIMATE_CLAUDE_DIR', '~/.claude/projects'))
    ap.add_argument('--codex-dir', default=os.environ.get('ESTIMATE_CODEX_DIR', '~/.codex/sessions'))
    ap.add_argument('--claude-all', action='store_true', help='compatibility flag: all Claude dirs are scanned; repository identity is required')
    ap.add_argument('--claude-project-dir', action='append',
                    help='explicitly trust this Claude transcript directory for --repo; repeatable '
                         '(default: $ESTIMATE_CLAUDE_PROJECT_DIR, several joined with os.pathsep)')
    ap.add_argument('--idle-min', type=float, default=5.0, help='largest log gap counted as observed activity, in minutes (heuristic)')
    ap.add_argument('--window', type=float, default=14.0, help='calibration window in days')
    ap.add_argument('--now', help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest='cmd', required=True)
    est = argparse.ArgumentParser(add_help=False)
    est.add_argument('--agent', help='estimating agent harness (default: detected; claude-code, codex, ...)')
    est.add_argument('--model', help='estimating model id (or $ESTIMATE_MODEL)')
    p = sub.add_parser('init', parents=[est], help='rough base for a repo with history: blind retro-sizing packet, then --apply')
    p.add_argument('--last', type=int, default=30); p.add_argument('--history', type=int, default=300)
    p.add_argument('--apply', metavar='SIZES_JSON'); p.add_argument('--seed', type=int, default=1); p.set_defaults(f=cmd_init)
    p = sub.add_parser('record', help='settle estimates from merged PRs (automatic actuals)')
    p.add_argument('--history', type=int, default=200)
    p.add_argument('--refresh', action='store_true', help='append recalculated actuals for all tracked PRs, preserving estimates')
    p.add_argument('--no-pr-days', type=float, default=NO_PR_DAYS,
                   help=f"mark a branch estimate 'no PR' when its branch has no PR after this many days (default {NO_PR_DAYS}; 0: never)")
    p.add_argument('--all', action='store_true', help='every project in ~/.config/agent-estimate/projects.json, one line each')
    p.set_defaults(f=cmd_record)
    p = sub.add_parser('calibrate', help='recompute ranges per SP and per estimator from the recent window')
    p.add_argument('--since', help='ignore history before this ISO date (a process change); saved; `none` clears it')
    p.add_argument('--check', action='store_true'); p.add_argument('--json', action='store_true'); p.set_defaults(f=cmd_calibrate)
    p = sub.add_parser('forecast', parents=[est], help='SP -> hours and $ for one task (logs it with --task), or a backlog file -> totals')
    p.add_argument('--sp', type=int); p.add_argument('--closest', help='optional: the closest past PR you compared with')
    p.add_argument('--task', help='PR number or branch: log this estimate so `record` can settle it')
    p.add_argument('--backlog', nargs='?', const=CONFIGURED, metavar='FILE',
                   help='a backlog table; without FILE, the source in .estimate/config.json (default BACKLOG.md)')
    p.add_argument('--skip-unsized', action='store_true',
                   help='with --backlog: leave items without effort out of the totals and order, with a warning')
    p.add_argument('--seed', type=int, default=1)
    p.add_argument('--json', action='store_true'); p.set_defaults(f=cmd_forecast)
    p = sub.add_parser('plan', help='critical-chain forecast of the backlog: waves, chain, p50/p80 dates per agent count')
    p.add_argument('--backlog', nargs='?', const=CONFIGURED, default=CONFIGURED, metavar='FILE',
                   help='a backlog table; default: the source in .estimate/config.json')
    p.add_argument('--agents', type=int, default=3, help='agent count for the waves and the buffer (default 3)')
    p.add_argument('--top', type=int, help='plan only the first N cards in Score order (their dependencies come first)')
    p.add_argument('--seed', type=int, default=1); p.add_argument('--json', action='store_true'); p.set_defaults(f=cmd_plan)
    p = sub.add_parser('backlog', help='backlog sources: `pull` copies open Tiden cards into .estimate/backlog.md')
    bsub = p.add_subparsers(dest='backlog_cmd', required=True)
    bsub.add_parser('pull', help='read-only: open Tiden cards -> .estimate/backlog.md, keeping effort/value/depends set locally'
                    ).set_defaults(f=cmd_backlog_pull)
    p = sub.add_parser('refs', help='optional: candidate past PRs to compare with')
    p.add_argument('--sp', type=int); p.add_argument('--paths', help='likely touched paths, comma-separated')
    p.add_argument('--limit', type=int, default=5); p.set_defaults(f=cmd_refs)
    p = sub.add_parser('backtest', help='leave-future-out accuracy of past quotes')
    p.add_argument('--include-retro', action='store_true'); p.set_defaults(f=cmd_backtest)
    p = sub.add_parser('compare', help='process traits side by side: one .estimate dir per project')
    p.add_argument('--dir', dest='dirs', action='append', required=True, metavar='DIR', help='an .estimate data directory; repeatable')
    p.add_argument('--json', action='store_true'); p.set_defaults(f=cmd_compare)
    p = sub.add_parser('dashboard', help='write a static HTML dashboard')
    p.add_argument('--out'); p.add_argument('--backlog')
    p.add_argument('--offline', action='store_true', help='accepted for old scripts; the dashboard makes no network calls')
    p.add_argument('--no-burn', action='store_true', help='skip reading local Claude Code and Codex logs for the burn rows')
    p.add_argument('--no-labels', action='store_true', help='hide session labels (the first line of a prompt) in the Usage section')
    p.add_argument('--all', action='store_true', help='every project in ~/.config/agent-estimate/projects.json, one line each')
    p.set_defaults(f=cmd_dashboard)
    p = sub.add_parser('usage', help='spend by repo, cache, daily trend, parallel sessions and models from every local agent log')
    p.add_argument('--days', type=int, choices=USAGE_DAYS, default=30, help='the last N local days, today included (default 30)')
    p.add_argument('--no-labels', action='store_true',
                   help='hide session labels (the first line of a prompt); also `"session_labels": false` in config.json')
    p.add_argument('--json', action='store_true'); p.set_defaults(f=cmd_usage)
    p = sub.add_parser('install', help='symlink `estimate` onto PATH and the skills into Claude Code, Codex and .agents')
    p.add_argument('--bin-dir', default='~/.local/bin'); p.add_argument('--dry-run', action='store_true'); p.set_defaults(f=cmd_install)
    p.add_argument('--statusline', action='store_true',
                   help='opt-in: wrap the Claude Code status line so it logs plan-window %% (your status line keeps running)')
    p = sub.add_parser('uninstall', help='remove the symlinks `install` made (only those pointing into this repo)')
    p.add_argument('--bin-dir', default='~/.local/bin'); p.add_argument('--dry-run', action='store_true'); p.set_defaults(f=cmd_uninstall)
    p = sub.add_parser('statusline-log', help='for a Claude Code status line: log plan-window %% from stdin, then run --then')
    p.add_argument('--then', metavar='COMMAND', help='your own status line command; it gets the same input')
    p.set_defaults(f=cmd_statusline_log)
    p = sub.add_parser('doctor', help='which data dir, repo and Claude dirs are used and why; counts, gh auth, status line')
    p.set_defaults(f=cmd_doctor)
    p = sub.add_parser('link', help='remember the data dir for this repo (all worktrees) in ~/.config/agent-estimate/projects.json')
    p.add_argument('--dir', default=argparse.SUPPRESS, help='the data directory')
    p.add_argument('--repo', default=argparse.SUPPRESS, help='owner/name (default: the git remote)')
    p.add_argument('--claude-project-dir', action='append', default=argparse.SUPPRESS,
                   help='Claude transcript directory trusted for this repo; repeatable')
    p.set_defaults(f=cmd_link)
    p = sub.add_parser('setup', help='ask where the backlog is and write .estimate/config.json')
    p.add_argument('--source', choices=('markdown', 'tiden')); p.add_argument('--path', help='markdown backlog file (default BACKLOG.md)')
    p.add_argument('--product-id', help='Tiden product id (optional)')
    p.add_argument('--create-backlog', action='store_true', help='create the markdown file with an empty table if it is missing')
    p.set_defaults(f=cmd_setup)
    args = ap.parse_args(argv)
    run_all(args) if getattr(args, 'all', False) else args.f(args)


if __name__ == '__main__':
    main()
