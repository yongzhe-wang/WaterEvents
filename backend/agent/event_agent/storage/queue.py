"""event_agent.db_queue — the work_queue interface: claim (priority ladder + lease reclaim), complete (self re-arm),
enqueue (idempotent dynamic-add). This is the whole "pausable / resumable / dynamic / VLM-paced" machinery in ~4 funcs.

用一句话讲完: worker 调 claim_work 用 (priority, due_at) SKIP-LOCKED 抢一个到期的 unit(incremental 优先、没有就 full;
可选 type_filter 做预留槽)→ 扫完调 complete_work 把 status 翻回 'queued' + due_at 推到下次(full +7d / incremental
+30min)= 自 re-arm 的循环队列 → enqueue 随时 UPSERT 加公司/加 hub(UNIQUE(type,url) 幂等)。停 worker 就暂停(状态全在
表里),重启就续。{USER 2026-07-25 "one queue, pausable/resumable, dynamic add, VLM never idle"} [CONFIDENCE: CONFIRMED].
"""
from __future__ import annotations

import os

import asyncpg

_DSN = os.environ.get("WATEREVENTS_DB_DSN",
                      "postgresql://postgres.ezuvmolyfgsadkehjnef:FocusAlpha2026@aws-1-us-east-1.pooler.supabase.com:6543/postgres")
_SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")
_LEASE_MIN = int(os.environ.get("WATEREVENTS_LEASE_MIN", "15"))     # full BFS can run minutes → generous lease
_FULL_INTERVAL_S = int(os.environ.get("EVENTINC_FULL_INTERVAL_S", str(7 * 24 * 3600)))   # weekly re-arm
_INC_INTERVAL_S = int(os.environ.get("EVENTINC_INC_INTERVAL_S", str(30 * 60)))            # 30-min re-arm


async def connect_pool(min_size: int = 2, max_size: int = 8) -> asyncpg.Pool:
    """asyncpg pool on the transaction pooler (statement_cache_size=0 is REQUIRED on Supavisor). search_path pinned so
    every query hits waterevents.* without a schema prefix."""
    return await asyncpg.create_pool(_DSN, min_size=min_size, max_size=max_size, statement_cache_size=0,
                                     server_settings={"search_path": _SCHEMA})


async def claim_work(pool: asyncpg.Pool, worker_id: str, type_filter: str | None = None) -> asyncpg.Record | None:
    """Atomically claim ONE due unit → mark it 'running' with a fresh lease. Claimable = queued OR a running row whose
    lease lapsed (crashed worker → auto-reclaim). Ordered by (priority ASC, due_at ASC): incremental (priority 10) beats
    full (100), so a due incremental NEVER waits behind full; within a type the oldest-due goes first. type_filter lets a
    RESERVED-SLOT worker restrict to 'incremental' (so a long full BFS can't starve the 30-min hubs). SKIP LOCKED → N
    workers never collide. Returns the row or None (nothing due → caller backs off). {claim ladder from the design}."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            UPDATE work_queue SET
                status = 'running',
                lease_owner = $1,
                lease_until = now() + ($2 || ' minutes')::interval,
                attempt = attempt + 1,
                updated_at = now()
            WHERE id = (
                SELECT id FROM work_queue
                WHERE (status = 'queued' OR (status = 'running' AND lease_until < now()))
                  AND due_at <= now()
                  AND ($3::text IS NULL OR type = $3)          -- reserved-slot filter (NULL = claim any type)
                ORDER BY priority ASC, due_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, company_id, url, type, attempt;
            """,
            worker_id, str(_LEASE_MIN), type_filter,
        )


async def complete_work(pool: asyncpg.Pool, wid, unit_type: str, event_count: int = 0,
                        duration_s: float | None = None, render_pages: int | None = None,
                        vlm_calls: int | None = None) -> None:
    """Finished a scan → SELF RE-ARM: flip status back to 'queued' and push due_at to the next cycle (full +7d, incremental
    +30min), clear the lease, AND record this scan's resource usage (duration_s / last_render_pages / last_vlm_calls) for
    the finish-time EWMA + solver. The row cycles queued→running→queued forever = a recurring queue, no terminal 'done'.
    This is what makes 'every company weekly / every hub every cycle' automatic without a central scheduler. {self re-arm;
    USER 2026-07-26 "estimation of finish time of each"} [CONFIDENCE: CONFIRMED — per-unit cost = finish-time raw material].

    RESETS `attempt` — without this the counter is a one-way ratchet. claim_work does `attempt = attempt + 1` on EVERY
    claim, including the routine ones and including a lease-expiry re-claim after a SIGKILL, but nothing ever decremented
    it. In a RECURRING queue that means `attempt` measures "how many times has this unit ever been picked up", not
    "how many times has it failed in a row" — which is what fail_work's cap actually tests. An incremental hub on a
    30-minute rotation is claimed 48×/day, so it crosses the cap of 4 within ~2 hours, after which its next transient
    hiccup (one nav timeout, one blip) marks it 'failed' permanently. Zeroing on success restores the intended
    "consecutive failures" meaning. {QUEUE.PY claim_work "ATTEMPT = ATTEMPT + 1,"; fail_work "IF ROW AND
    ROW["ATTEMPT"] >= MAX_ATTEMPTS"} {MEASURED 2026-07-28 live work_queue: "INCREMENTAL AVG_ATTEMPT 1.3, MAX 5, 56 UNITS
    ALREADY AT OR PAST THE CAP OF 4" while status='failed' was still 0 — the ratchet was climbing but had not yet fired}
    [CONFIDENCE: CONFIRMED 100% — the counter's climb was read off the live table; pairs with the pacer's failed-row
     reaper, since without a reaper anything that does trip the cap is unreachable forever]."""
    async with pool.acquire() as conn:
        if unit_type == "full":                              # full stays weekly (its cadence isn't paced by the solver yet)
            interval = _FULL_INTERVAL_S
        else:                                                # incremental re-arm interval = the solver's current T* (pacer
            ts = await conn.fetchval("SELECT t_star_s FROM scheduler_state WHERE id=1")   # writes it); fall back to 30min
            interval = int(ts) if ts and ts > 0 else _INC_INTERVAL_S   # if the pacer hasn't solved yet (cold start)
        await conn.execute(
            """
            UPDATE work_queue SET
                status = 'queued', lease_owner = NULL, lease_until = NULL,
                attempt = 0,
                due_at = now() + ($2 || ' seconds')::interval,
                last_scanned_at = now(), last_event_count = $3,
                duration_s = $4, last_render_pages = $5, last_vlm_calls = $6, updated_at = now()
            WHERE id = $1;
            """,
            wid, str(interval), event_count, duration_s, render_pages, vlm_calls,
        )


async def fail_work(pool: asyncpg.Pool, wid, unit_type: str, max_attempts: int = 4) -> None:
    """A scan errored. Under max_attempts → re-arm SOON (short backoff) for a retry; at the cap → mark 'failed' (fail-loud,
    a reconcile/monitor surfaces it) so a permanently-broken url doesn't spin forever. {fail-loud}."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT attempt FROM work_queue WHERE id = $1", wid)
        if row and row["attempt"] >= max_attempts:
            await conn.execute("UPDATE work_queue SET status='failed', lease_owner=NULL, lease_until=NULL, updated_at=now() WHERE id=$1", wid)
        else:
            await conn.execute(
                "UPDATE work_queue SET status='queued', lease_owner=NULL, lease_until=NULL, "
                "due_at = now() + interval '5 minutes', updated_at=now() WHERE id=$1", wid)


async def enqueue(pool: asyncpg.Pool, rows: list[dict]) -> int:
    """Idempotent DYNAMIC-ADD: UPSERT units on UNIQUE(type,url). rows = [{company_id, url, type, priority, vlm_weight,
    due_at?}]. Re-adding an existing (type,url) is a no-op on the identity (keeps its live status/due_at) — so seeding,
    a new company, or a full-run discovering a new hub all just call this. full priority=100/weight=5, incremental=10/1.
    due_at defaults to now() (or a caller-supplied stagger, e.g. full spread across the week). Returns rows attempted."""
    if not rows:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO work_queue (company_id, url, type, priority, vlm_weight, due_at)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()))
            ON CONFLICT (type, url) DO NOTHING;
            """,
            [(r.get("company_id"), r["url"], r["type"],
              r.get("priority", 10 if r["type"] == "incremental" else 100),
              r.get("vlm_weight", 1 if r["type"] == "incremental" else 5),
              r.get("due_at")) for r in rows],
        )
    return len(rows)
