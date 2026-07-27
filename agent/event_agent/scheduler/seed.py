"""event_agent.seed — populate work_queue: FULL units (every company, due_at SPREAD across the week) + INCREMENTAL
units (every hub = a source_url that produced events, due now).

用一句话讲完: full seed 从 waterevents.companies 拿全部 ir_url → enqueue type='full', due_at 按 rank 摊到未来 7 天(不是全设
now → 避免周一洪峰、负载平坦); incremental seed 从 waterevents.events 拿每家产出过 event 的 distinct source_url = hub →
enqueue type='incremental', due_at=now(立即开始 30-min 循环)。都走 db_queue.enqueue 的幂等 UPSERT,所以可重复跑 / 全量 run
发现新 hub 后再跑一次只会增量补。{USER 2026-07-25 "full spread across week; incremental hubs; ~6000+ pages"}.

Run:  PYTHONPATH=/workspace/WaterEvents WATEREVENTS_DB_DSN=<dsn> python -m agent.event_agent.seed [full|incremental|both]
"""
from __future__ import annotations

import asyncio
import sys

from ..storage import queue as q

_WEEK_S = 7 * 24 * 3600


async def seed_full(pool) -> int:
    """Every distinct company (waterevents.companies.ir_url) → a full-BFS unit, due_at SPREAD evenly across the next 7 days
    (rank/total × week) so the weekly deep-crawl load is flat, not a Monday spike.
    WHY waterevents, not public: `public` was the 07-22-frozen legacy ir-pipeline schema and was DROPPED 2026-07-26 — the
    canonical company universe (2785 rows, incl. event_hubs multi-seed) now lives in waterevents.companies, and the
    company_id this returns must be the waterevents uuid anyway (work_queue.company_id FKs into it).
    {SCAN whn86f4f7 "canonical = waterevents; public = 07-22 冻结 legacy"} [CONFIDENCE: CONFIRMED — public 已 drop]."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, ir_url FROM waterevents.companies "
                                "WHERE ir_url IS NOT NULL AND ir_url <> '' ORDER BY ir_url")
    n = len(rows)
    units = []
    for rank, r in enumerate(rows):
        offset = int((rank / max(n, 1)) * _WEEK_S)           # 0 … 7 days, evenly staggered
        units.append({"company_id": r["id"], "url": r["ir_url"], "type": "full",
                      "due_at": None})                       # placeholder; set staggered below via SQL now()+offset
        units[-1]["_offset_s"] = offset
    # enqueue with per-row staggered due_at (now() + offset) — db_queue.enqueue takes due_at as a timestamptz, so compute here
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO work_queue (company_id, url, type, priority, vlm_weight, due_at) "
            "VALUES ($1,$2,'full',100,5, now() + ($3 || ' seconds')::interval) "
            "ON CONFLICT (type, url) DO NOTHING;",
            [(u["company_id"], u["url"], str(u["_offset_s"])) for u in units])
    return n


async def seed_incremental(pool, run_id: str | None = None, top_k: int = 3, limit: int = 0) -> int:
    """Enqueue MONITORABLE hubs (not every source_url). A hub qualifies on TWO conditions: ① multiple events (≥2 → a
    LISTING, not a single-event leaf detail page) ② has a recent/upcoming event (a 2026+ date → a LIVE listing that keeps
    gaining events, not a frozen 2015-2020 archive). Per company keep only the top_k hubs (ranked by recent-events then
    total-events) so the fleet can cover them in a cycle. `limit` caps the total for a test subset (0 = all).
    run_id=None (the DEFAULT now) aggregates events across ALL runs → the FULL hub universe (a hub is a hub no matter which
    crawl discovered it); pass a specific run_id only to scope to one crawl. This is the full run FEEDING incremental:
    re-run as the full run discovers more hubs — idempotent UPSERT. {USER 2026-07-26 "199 hub?? you need all" — the earlier
    199 was a LIMIT=200 test subset; seed the whole ~5.3k universe} [CONFIDENCE: CONFIRMED — direct correction]."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT company_id, source_url FROM (
                SELECT company_id, source_url,
                       count(*) AS ev,
                       count(*) FILTER (WHERE event_date ~ '202[6-9]|20[3-9][0-9]') AS recent_ev,
                       row_number() OVER (PARTITION BY company_id
                           ORDER BY count(*) FILTER (WHERE event_date ~ '202[6-9]|20[3-9][0-9]') DESC,
                                    count(*) ASC) AS rk        -- prefer MORE-recent then FEWER-total (a live listing, not a mega archive)
                FROM events WHERE ($1::text IS NULL OR run_id = $1) AND source_url <> ''
                  -- run_id NULL → aggregate across ALL runs (the full universe); else scope to one crawl. {USER "you need all"}
                  -- EXCLUDE filings archives: 10-K/8-K/Form-4 lists (100+ rows, arrive via SEC-API not the crawl, change
                  -- quarterly not by-the-minute). {USER 2026-07-25 "big filling hub is def not the ones we should monitor"}
                  AND source_url !~* '/(sec-filings|edgar|financials/(sec|quarterly|annual|financial-results)|regulatory)'
                GROUP BY company_id, source_url
            ) h
            -- ① multiple events (≥2, a listing) ② has a recent/upcoming event (live) ③ NOT a mega archive (≤40 events —
            -- a real "new events" page shows only a few newest) ④ top-K per company. {USER 2026-07-25 "new page usually
            -- have only a few events that is newest"}.
            WHERE ev BETWEEN 2 AND 40 AND recent_ev >= 1 AND rk <= $2
            ORDER BY recent_ev DESC
            """ + (f" LIMIT {int(limit)}" if limit else ""),
            run_id, top_k)
    units = [{"company_id": r["company_id"], "url": r["source_url"], "type": "incremental"} for r in rows]
    return await q.enqueue(pool, units)


async def rederive_hubs_for_company(pool, company_id, run_id: str = "eventinc", top_k: int = 3) -> int:
    """UPDATABLE HUBS — after a FULL BFS of ONE company finishes, re-run the SAME 2-condition hub filter scoped to just
    that company's fresh events and idempotently UPSERT its qualifying hubs into the incremental queue. WHY: if we later
    improve watercrawl / the agent and full-re-crawl a company, it may surface MORE events on MORE source_urls → those new
    listings should start being monitored every cycle automatically, without a manual global re-seed. Idempotent (ON
    CONFLICT (type,url) DO NOTHING) → existing hubs are no-ops, only genuinely-new listings get added. Same filter as
    seed_incremental (≥2 events, has a 2026+ date, ≤40 events, not a filings archive, top_k per company). {USER 2026-07-26
    "hubs is updatable ... full rerun some companies and get more events → more hubs"} [CONFIDENCE: CONFIRMED — direct spec]."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source_url FROM (
                SELECT source_url,
                       count(*) AS ev,
                       count(*) FILTER (WHERE event_date ~ '202[6-9]|20[3-9][0-9]') AS recent_ev,
                       row_number() OVER (ORDER BY count(*) FILTER (WHERE event_date ~ '202[6-9]|20[3-9][0-9]') DESC,
                                          count(*) ASC) AS rk       -- MORE-recent then FEWER-total = a live listing
                FROM events WHERE company_id = $1 AND run_id = $2 AND source_url <> ''
                  AND source_url !~* '/(sec-filings|edgar|financials/(sec|quarterly|annual|financial-results)|regulatory)'
                GROUP BY source_url
            ) h
            WHERE ev BETWEEN 2 AND 40 AND recent_ev >= 1 AND rk <= $3
            """,
            company_id, run_id, top_k)
    units = [{"company_id": company_id, "url": r["source_url"], "type": "incremental"} for r in rows]
    return await q.enqueue(pool, units)                          # idempotent: new hubs added, existing no-op


async def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "both"
    pool = await q.connect_pool()
    if what in ("full", "both"):
        nf = await seed_full(pool)
        print(f"[seed] full: {nf} companies enqueued (due_at spread across 7 days)", flush=True)
    if what in ("incremental", "both"):
        import os
        top_k = int(os.environ.get("EVENTINC_TOP_K", "3"))
        limit = int(os.environ.get("EVENTINC_SEED_LIMIT", "0"))          # 0 = all (default); e.g. 200 for a test subset
        # DEFAULT = None → aggregate hubs across ALL runs (the full universe). Set EVENTINC_SEED_FROM_RUN to scope to one.
        # {USER 2026-07-26 "199 hub?? you need all" — the 199 was a LIMIT=200/single-run subset; seed the whole universe}.
        src_run = os.environ.get("EVENTINC_SEED_FROM_RUN") or None
        ni = await seed_incremental(pool, run_id=src_run, top_k=top_k, limit=limit)
        print(f"[seed] incremental: {ni} hubs enqueued (2-cond filter, top_k={top_k}, limit={limit or 'all'}, run={src_run or 'ALL'})", flush=True)
    async with pool.acquire() as conn:
        for t in ("full", "incremental"):
            c = await conn.fetchval("SELECT count(*) FROM work_queue WHERE type=$1", t)
            print(f"[seed] work_queue {t}: {c} rows", flush=True)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
