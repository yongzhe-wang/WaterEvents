"""event_agent.scan — execute ONE work_queue unit. UNIFIED path: full and incremental call the SAME crawl_company;
they differ ONLY by DEPTH (max_pages).

用一句话讲完: incremental = depth 1 → crawl_company(url, max_pages=1) = 只 render 当前 hub 那一页、抽它的 events、不跟
route 往深爬(BFS 的循环在 visited>=max_pages 时停,max_pages=1 就是「只当前页」);full = depth _FULL → 深 BFS。两者共用
同一套 render 全栈(playwright→residential→impersonate→camoufox)、同一套 extract(Lnn/grounding/chunk)、同一套幂等
flush(events 带 source_url + save_pages)。所以「两种类型」在代码里只是一个 max_pages 参数的差别。
{USER 2026-07-25 "unify the code, deep=1 means current page, both idempotent flush"} [CONFIDENCE: CONFIRMED — 直接指令].
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from ..crawl.engine import crawl_company                             # THE unified engine (BFS when max_pages>1, single page when =1)
from ..storage import events as db                                             # idempotent flush_events + save_pages (source_url wired)
from ..title import backfill_for_company                            # per-scan hook: curl title-less events' URLs → fill titles (no VLM)

_FULL_MAX_PAGES = int(os.environ.get("EVENT_MAX_PAGES", "60"))              # full = deep BFS
_INC_MAX_PAGES = int(os.environ.get("EVENTINC_INC_MAX_PAGES", "1"))        # incremental = depth 1 = current hub page only
_RUN_ID = os.environ.get("WATEREVENTS_RUN_ID", "eventinc")


async def _company_id(pool, url: str) -> str:
    """Resolve (or create) the companies row for this url → its uuid (events/pages FK). The unit usually CARRIES a
    company_id (set at enqueue); this is the fallback for a hub added before its company row existed. SELECT-then-INSERT
    (ir_url has no unique constraint). {MIRROR thekillerdeal._company_id}."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM companies WHERE ir_url = $1 LIMIT 1", url)
        if row:
            return row["id"]
        row = await conn.fetchrow(
            "INSERT INTO companies (ir_url, status, run_id) VALUES ($1, 'discovering', $2) RETURNING id", url, _RUN_ID)
        return row["id"]


def _make_gate(pool, cid):
    """Build the hash-gate closure for this company: gate(url, text) → True iff the page CHANGED since last scan (so the
    VLM should extract it). Compares sha256(text) to the stored pages.content_hash. First-seen (prev is None) → changed →
    extract. Unchanged → False → crawl_company skips the VLM call (the VLM-saving lever). WHY a closure over cid: the
    stored hash is keyed (company_id, url), so the gate needs this scan's company id in scope. {PLAN §hash-gate; save_pages
    writes content_hash from the SAME page_text string} [CONFIDENCE: CONFIRMED — hash algo + source string match save_pages]."""
    async def gate(url: str, text: str) -> bool:
        h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()   # hash the freshly-rendered reading-order text
        async with pool.acquire() as conn:                            # look up the hash stored on the LAST scan of this url
            prev = await conn.fetchval("SELECT content_hash FROM pages WHERE company_id=$1 AND url=$2", cid, url)
        return prev != h                                              # differ (or first-seen: prev None) → changed → extract
    return gate


async def _company_seeds(pool, cid, primary_url: str) -> list[str]:
    """The MULTI-ENTRY SEED set for a full BFS: the company's `event_hubs` (ir_url_agent's curated events / presentations /
    calendar entry URLs — the extra IR entry points shown on the IR_URLS page). These are SEEDS = where a full BFS STARTS
    (crawled from round 0 at max score, multi-frontier), NOT the incremental HUBS (which are deep=1 monitor targets in
    work_queue). WHY multi-entry: an IR homepage often renders only 6-9 default-visible events; the full archive lives on a
    sibling events page a JS mega-menu never emits as an <a href> → seeding those sub-pages directly guarantees coverage.
    Dedupes the primary ir_url out (crawl_company adds it as start_url). event_hubs elements are objects {url,...} OR legacy
    bare strings — extract the url from both. {USER 2026-07-26 "hub is different from seed; hub is only for incremental
    deep=1, seed is for full bfs"} [CONFIDENCE: CONFIRMED — direct correction; seeds = full-BFS multi-entry]."""
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT event_hubs FROM companies WHERE id=$1", cid)
    hubs = json.loads(raw) if isinstance(raw, str) else (raw or [])   # asyncpg returns jsonb as text → parse
    prim = (primary_url or "").rstrip("/")
    seeds, seen = [], {prim}
    for h in hubs:                                            # normalize object {url,...} + legacy bare-string shapes
        u = h.get("url") if isinstance(h, dict) else (h if isinstance(h, str) else None)
        k = (u or "").rstrip("/")
        if k and k not in seen:                              # drop empties + the primary (start_url already carries it)
            seen.add(k)
            seeds.append(u)
    return seeds


async def scan_unit(pool, client, url: str, unit_type: str, company_id=None) -> dict:
    """Run ONE unit through the unified engine → return a STATS dict {events, duration_s, render_pages, vlm_calls,
    vlm_skipped}. incremental = deep=1 monitor of ONE hub, hash-gated (skip VLM when unchanged); full = deep BFS discovery
    seeded MULTI-ENTRY from the company's event_hubs (ir_url + events/presentations/calendar entry URLs), NOT hash-gated
    (full always fully extracts). Events flush LIVE per
    page (on_events) with source_url + content(+hash) persisted to `pages`; a final safety flush covers the last page.
    Idempotent throughout — a re-scan MERGES (ON CONFLICT dedup_key), so incremental re-scanning a hub every cycle only
    ADDS newly-announced events, never duplicates. The stats feed scan_log → C_R/C_V/hit_rate → the packing solver.
    {USER 2026-07-26 "calculate render and vlm usage for parallel"} [CONFIDENCE: CONFIRMED — direct instruction]."""
    depth = _INC_MAX_PAGES if unit_type == "incremental" else _FULL_MAX_PAGES
    cid = company_id or await _company_id(pool, url)

    async def _flush(evs, page=None):                        # per-page live flush (same contract as event_agent worker)
        await db.flush_events(pool, cid, _RUN_ID, evs)       # events carry source_url (tagged in crawl._harvest)
        if page:                                             # persist the source page content(+content_hash) → pages table
            await db.save_pages(pool, cid, _RUN_ID, [page])  # save_pages computes+stores sha256 → next scan's gate reads it

    # SEED vs HUB — two DIFFERENT things (per USER 2026-07-26 correction):
    #   • full  = deep BFS DISCOVERY, seeded MULTI-ENTRY from the company's event_hubs (ir_url + its events/presentations/
    #     calendar entry URLs). NO hash-gate — a full run always fully extracts (that's the point of discovery).
    #   • incremental = deep=1 MONITOR of ONE hub, hash-gated (skip VLM when the hub page is unchanged), NO extra seeds.
    # {USER 2026-07-26 "hub is different from seed; hub is only for incremental deep=1, seed is for full bfs"}
    # [CONFIDENCE: CONFIRMED — direct correction].
    if unit_type == "incremental":
        gate = _make_gate(pool, cid)                         # deep=1 monitor → skip VLM when unchanged
        seeds = None                                         # a hub is a single page; no multi-frontier
    else:                                                    # full
        gate = None                                          # full discovery always extracts (no skip)
        seeds = await _company_seeds(pool, cid, url)         # MULTI-ENTRY BFS: ir_url + event_hubs (the seeds)
    t0 = time.time()                                          # per-unit wall clock → work_queue.duration_s (finish-time EWMA)
    res = await crawl_company(url, max_pages=depth, client=client, on_events=_flush, gate=gate, seeds=seeds)
    dt = time.time() - t0
    await db.flush_events(pool, cid, _RUN_ID, res.get("events") or [])   # final idempotent safety flush
    stats = {                                                # resource-usage record for this scan → scan_log + work_queue
        "events": len(res.get("events") or []),
        "duration_s": round(dt, 2),
        "render_pages": res.get("pages") or 0,               # pages rendered → Σ/window = C_R
        "vlm_calls": res.get("vlm_calls") or 0,              # real VLM extracts → Σ/window = C_V
        "vlm_skipped": res.get("vlm_skipped") or 0,          # hash-gated skips → hit_rate denominator
        # THE SIGNAL THIS DICT USED TO DROP. crawl_company has always returned `status` ("ok"/"incomplete") plus the
        # failure counts, and has always printed a run-level ⚠️⚠️ INCOMPLETE RUN banner — but this dict took only the
        # resource numbers, so everything above the engine saw a successful scan that merely found nothing. On
        # 2026-07-28 the pod's vLLM died for 11h52m and that one gap turned ~18,000 correctly-detected failures into
        # 6,304 queue units re-armed as "scanned, nothing found", pushing due_at forward (full: +7 days) as if the work
        # had been done. No data was corrupted — the SCHEDULE was. Carrying these two keys is what lets worker.py tell
        # "this page has no events" apart from "we never actually got to read this page".
        # {W1.LOG 2026-07-29 "⛔ EXTRACT FAILED ... APIConnectionError: Connection error. — page's events LOST" ×1628}
        # {DB 2026-07-29 "6,304 OF 6,310 UNITS RE-ARMED WITH LAST_EVENT_COUNT=0; 19,829 SCAN_LOG ROWS; 0 EVENTS"}
        # [CONFIDENCE: CONFIRMED 100% — the dropped keys verified by reading both sides; the damage counted live].
        "status": res.get("status") or "ok",                 # "ok" | "incomplete" — the engine's run-level verdict
        "extract_errors": res.get("extract_errors") or 0,    # pages whose events were LOST to a hard VLM/transport failure
    }
    await db.log_scan(pool, unit_type, url, stats)           # append to scan_log (the windowed C_R/C_V/hit_rate source)
    # TITLE HOOK — after every full/incremental scan, curl THIS company's title-less events' URLs and fill what we can
    # (HTML og:title / PDF /Title, junk-filtered). Cheap (HTTP-only, no VLM), never raises. So freshly-discovered
    # empty-title events get a human title right away. {USER 2026-07-27 "use it along full+incremental after crawl"}.
    stats["titles_filled"] = await backfill_for_company(pool, cid, _RUN_ID)
    return stats
