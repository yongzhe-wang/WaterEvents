"""event_agent.worker — THE DISCOVERY WORKER: a production loop that turns the `companies` queue into `events` rows.

用一句话讲完: 一个常驻进程,循环 { 从 companies 队列 SKIP-LOCKED 抢一家公司 → 一边跑 crawl_company(内存 BFS,已有)
一边每 60s 心跳续 lease → 把发现的 events 批量幂等落 events 表 → 翻 companies.status },队列空了退避轮询。**它把已经
验证过的 crawl_company 包成生产 worker:安全领取、崩溃续跑、批量落库、fail-loud**,不改抽取逻辑本身。

Flow (one company):
  claim(SKIP LOCKED + lease) ──► [heartbeat 每60s renew_lease]  ──► crawl_company(BFS)
                                                                        │ {events, status, failed_render, failed_extract}
                                                                        ▼
                                              flush_events(batch, ON CONFLICT merge)  ──► mark_company(discovered|_partial)
  crawl 抛错 → fail_company(reason)。崩溃(进程死) → lease 过期,reconcile cron 把公司回 queued 重认领。

Run (on GCP, never on Mac {MEMORY "never run compute on the local Mac"}):
  WATEREVENTS_DB_DSN=<supavisor 6543 dsn> QWEN_BASE_URLS=<runpod>/v1 python3 -m agent.event_agent.worker
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid

from providers.qwen_llm import QwenClient                   # ONE client reused across companies → keep-alive to RunPod

from . import db
from .crawl import crawl_company

# who am I — stamped into lease_owner so fencing works (only the owner can renew / mark). host+pid+rand is unique per proc.
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
# a run tag groups one full 2000-company batch; override per launch so completion-barrier queries scope to this run.
_RUN_ID = os.environ.get("WATEREVENTS_RUN_ID", "run-" + time.strftime("%Y%m%d"))
_HEARTBEAT_S = int(os.environ.get("WATEREVENTS_HEARTBEAT_S", "60"))     # lease renew interval (< LEASE_MIN so it never lapses mid-crawl)
_EMPTY_BACKOFF_S = int(os.environ.get("WATEREVENTS_EMPTY_BACKOFF_S", "10"))  # poll interval when the queue is drained
_MAX_IDLE_ROUNDS = int(os.environ.get("WATEREVENTS_MAX_IDLE_ROUNDS", "6"))   # exit after this many empty polls (job can end)


async def _heartbeat(pool, company_id) -> None:
    """Renew the soft lease every _HEARTBEAT_S while the crawl runs. If renew fails (we lost the lease / hit the hard
    deadline) shout LOUDLY — flush+mark are still fenced+idempotent so nothing corrupts, but a lost lease means another
    worker is now re-crawling this company and our work is wasted. Cancelled by process_company when the crawl finishes."""
    while True:
        await asyncio.sleep(_HEARTBEAT_S)
        ok = await db.renew_lease(pool, company_id, _WORKER_ID)
        if not ok:                                           # lost the lease — say it, don't silently keep working
            print(f"[worker] ⚠️ LOST LEASE on company {company_id} (reclaimed or hard-deadline) — "
                  f"another worker may be re-crawling; this work is now redundant", flush=True)
            return


async def process_company(pool, client: QwenClient, company) -> None:
    """Crawl ONE claimed company end-to-end and persist it. Heartbeat runs concurrently. crawl_company itself is the
    already-verified in-memory BFS; the worker only wraps it with lease-keeping + idempotent flush + terminal marking."""
    cid, url = company["id"], company["ir_url"]
    print(f"[worker] claimed company {cid} attempt={company['attempt']} → {url}", flush=True)
    hb = asyncio.create_task(_heartbeat(pool, cid))          # keep the lease alive during the (maybe ~20 min) crawl
    try:
        # INCREMENTAL PERSIST — flush each page's events to the DB the MOMENT they're found, not once at the end. WHY: a
        # company killed mid-crawl by the stall-watchdog never let crawl_company RETURN, so the old end-only flush never
        # ran → every already-extracted event was lost (77% of companies showed 0 events + churned). The per-page callback
        # persists as we go; flush_events is idempotent (ON CONFLICT merge) so the re-crawl of a killed company merges
        # instead of duplicating. {USER 2026-07-23 "worker flush 只在最后一次性 ... 中途被杀 events 没落库 ... fix this"}.
        async def _flush(evs, page=None):                    # per-page incremental flush callback (evs + the source page)
            await db.flush_events(pool, cid, _RUN_ID, evs)
            if page:                                         # persist the source page content → pages table (frontend "Source page")
                await db.save_pages(pool, cid, _RUN_ID, [page])
        # reuse the shared QwenClient (keep-alive to RunPod); crawl_company reads EVENT_MAX_PAGES/EVENT_USE_IMAGE from env
        result = await crawl_company(url, client=client, on_events=_flush)
        n = await db.flush_events(pool, cid, _RUN_ID, result.get("events") or [])   # final idempotent flush (safety net)
        await db.mark_company(pool, cid, _WORKER_ID, result)
        # mirror crawl's fail-loud at the worker level so a degraded company is visible in the worker log too
        banner = "" if result.get("status") == "ok" else \
            f"  ⚠️ PARTIAL (failed_render={result.get('failed_render')} failed_extract={result.get('failed_extract')})"
        print(f"[worker] ✅ company {cid} → {n} events flushed, {result.get('pages')} pages, "
              f"status={result.get('status')}{banner}", flush=True)
    except Exception as e:                                   # noqa: BLE001 — a hard crawl error must mark the company failed, not vanish
        print(f"[worker] ⛔ company {cid} CRAWL FAILED: {type(e).__name__}: {e} — marking failed", flush=True)
        await db.fail_company(pool, cid, _WORKER_ID, f"{type(e).__name__}: {e}")
    finally:
        hb.cancel()                                          # stop the heartbeat regardless of outcome


# Optional cap on how many companies THIS worker processes before it exits — 0 = unbounded (drain the queue). WHY: a
# controlled smoke test wants to validate the full claim→crawl→flush→mark path on a SMALL slice (e.g. 5 companies) of a
# large queue WITHOUT draining all 1000; set WATEREVENTS_MAX_COMPANIES=5 for that. {USER 2026-07-23 "first test 5
# companies"} [CONFIDENCE: CONFIRMED 100% — direct request to bound the smoke run before the full 1000 stress test].
_MAX_COMPANIES = int(os.environ.get("WATEREVENTS_MAX_COMPANIES", "0"))


async def worker_loop(pool) -> None:
    """Claim-process-repeat until the queue is drained (N consecutive empty polls) OR the optional _MAX_COMPANIES cap is
    hit. One shared QwenClient for the whole loop so the RunPod connection pool stays warm across companies."""
    client = QwenClient()
    idle = 0
    done = 0                                                  # companies this worker has fully processed (for the cap)
    while idle < _MAX_IDLE_ROUNDS:
        if _MAX_COMPANIES and done >= _MAX_COMPANIES:         # hit the smoke-test cap → stop claiming, exit cleanly
            print(f"[worker] {_WORKER_ID} reached MAX_COMPANIES={_MAX_COMPANIES} — exiting", flush=True)
            return
        company = await db.claim_company(pool, _WORKER_ID, _RUN_ID)
        if company is None:                                  # queue empty (for now) → back off, count idle rounds
            idle += 1
            print(f"[worker] queue empty ({idle}/{_MAX_IDLE_ROUNDS}) — backoff {_EMPTY_BACKOFF_S}s", flush=True)
            await asyncio.sleep(_EMPTY_BACKOFF_S)
            continue
        idle = 0                                             # got work → reset idle counter
        await process_company(pool, client, company)
        done += 1                                            # count toward the optional cap
    print(f"[worker] {_WORKER_ID} exiting — queue drained after {_MAX_IDLE_ROUNDS} idle rounds", flush=True)


async def main() -> None:
    print(f"[worker] starting {_WORKER_ID} run={_RUN_ID}", flush=True)
    pool = await db.connect_pool()
    try:
        await worker_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
