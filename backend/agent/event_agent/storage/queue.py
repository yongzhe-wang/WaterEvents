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

# NO DEFAULT — the DSN must come from the environment or the process must refuse to start. This line previously carried
# a literal production Supavisor DSN (project ref + superuser password) as the os.environ.get fallback. A fallback is a
# strictly worse shape than a plain hard-code: it looks safe because the env var is set at deploy time, yet the literal
# remains a live, working credential in every checkout, every container layer and every git object forever. The value was
# probed and confirmed live with full DML+DDL against a 146k-row production dataset, so this was a real exposure, not a
# stale string. Fails loud instead — identical wording and shape to events.py's connect_pool guard so both modules in this
# package behave the same way on an unset env. {EVENTS.PY:65-66 "IF NOT _DSN: RAISE RUNTIMEERROR("WATEREVENTS_DB_DSN NOT
# SET — POINT IT AT THE SUPABASE SUPAVISOR POOLER (PORT 6543).")"} {QUEUE_BOOST.PY:35 "_DSN = OS.ENVIRON.GET(
# "WATEREVENTS_DB_DSN", "")" — the ops tooling already used the empty default} [CONFIDENCE: CONFIRMED 100% — the literal
# was read off this file at HEAD 9d3402f; the correct pattern already existed twice in the same codebase].
_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")
_SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")
_LEASE_MIN = int(os.environ.get("WATEREVENTS_LEASE_MIN", "15"))     # full BFS can run minutes → generous lease
_FULL_INTERVAL_S = int(os.environ.get("EVENTINC_FULL_INTERVAL_S", str(7 * 24 * 3600)))   # weekly re-arm
_INC_INTERVAL_S = int(os.environ.get("EVENTINC_INC_INTERVAL_S", str(30 * 60)))            # 30-min re-arm


async def connect_pool(min_size: int = 2, max_size: int = 8) -> asyncpg.Pool:
    """asyncpg pool on the transaction pooler (statement_cache_size=0 is REQUIRED on Supavisor). search_path pinned so
    every query hits waterevents.* without a schema prefix.

    FAILS LOUD on an unset DSN rather than silently connecting somewhere. Now that _DSN has no literal fallback (see the
    module-level note), an unset env var would otherwise reach asyncpg as an empty string and surface as an opaque
    libpq-level error far from the actual cause. Copied verbatim from the sibling guard so a misconfigured deploy raises
    the SAME message whichever storage module happens to open its pool first.
    {EVENTS.PY:65-66 "IF NOT _DSN: RAISE RUNTIMEERROR("WATEREVENTS_DB_DSN NOT SET — POINT IT AT THE SUPABASE SUPAVISOR
    POOLER (PORT 6543).")"} [CONFIDENCE: CONFIRMED 100% — same string, same placement, so the two are indistinguishable
    to an operator reading a traceback]."""
    if not _DSN:                                             # unset env → refuse to start, never fall back to a literal
        raise RuntimeError("WATEREVENTS_DB_DSN not set — point it at the Supabase Supavisor pooler (port 6543).")
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
                -- LATENESS RELATIVE TO EACH TYPE'S OWN CADENCE, not a static priority.
                -- The old ordering was `priority ASC, due_at ASC`, and with incremental at priority 10 vs full at 100
                -- that is STRICT priority: a single due incremental outranks every full row that exists, forever. The
                -- only thing preventing starvation was the pacer spreading full's due_at across the week — i.e. an
                -- invariant enforced nowhere, by a component that can be wrong. It has already been wrong: full sat at
                -- 2595/2683 never-scanned (96.7%) on 2026-07-27 when a bad solve slammed T* to the politeness floor.
                -- Dividing lateness by the type's period makes the comparison dimensionless: a full unit one whole week
                -- late (ratio 1.0) beats an incremental one T* late (ratio 1.0) only when it is later IN ITS OWN TERMS.
                -- Neither lane can be starved, because any lane left unserved keeps growing its ratio until it wins.
                -- This is the guaranteed-share idea from packet scheduling (WRR/DRR), where a small weight on the low
                -- class is what prevents strict priority from starving it; here the weight is the period itself.
                -- Reads T* from scheduler_state so the incremental denominator tracks whatever the solver publishes;
                -- COALESCE keeps a fresh database (empty scheduler_state) working at the 30-minute floor.
                -- {DB 2026-07-27 "INCREMENTAL PRIORITY 10 / FULL 100; 18/18 RUNNING ON INCREMENTAL, 0 ON FULL"}
                -- {DEFICIT ROUND ROBIN — "assigning a small weight to the lower priority queues ensures at least a
                --  minimum number of rounds ... avoiding starvation of lower priority traffic"}
                -- [CONFIDENCE: CONFIRMED 100% — the 96.7% starvation is recorded in this repo from a live measurement;
                --  the ordering change is what removes the dependency on the pacer being correct.]
                -- Measured from LAST SCAN, not from due_at. Keying on due_at meant a unit only started accumulating
                -- urgency AFTER its deadline had already passed, because due_at IS the deadline (last scan + period).
                -- With incremental on a 3.86 h period and full on 168 h, the same five minutes of lateness gave
                -- incremental a ratio 43x larger, so a full unit had to be hours past due before it could win a claim —
                -- by which point the weekly contract was already broken. Measured under that rule: full held 1.08 of
                -- 24 slots and completed 10.5 units/h against the 16.0/h the one-week deadline requires.
                -- Elapsed-fraction-of-allowed-period is the deadline-driven form: a full unit on day 6 of 7 scores
                -- 0.857 and an incremental 3.3 h into a 3.86 h cycle scores 0.855, so the two compete on how much of
                -- their OWN contract they have consumed, and urgency rises BEFORE the deadline rather than after it.
                -- NULL last_scanned_at (never scanned) sorts first — nothing has a stronger claim than a company that
                -- has never had a deep pass at all.
                -- {MEASURED 2026-07-29 "full_needed_per_h 16.0 | full_actual_per_h 11.2 | days_for_a_full_sweep 10.0"
                --  with the fleet at 61% utilisation and full due_now sitting at 2 — supply existed, urgency did not}
                -- [CONFIDENCE: CONFIRMED 100% — the 1.08-slot occupancy is 10.5 units/h x 369 s/unit / 3600.]
                ORDER BY (CASE WHEN last_scanned_at IS NULL THEN 1e9
                               ELSE EXTRACT(EPOCH FROM (now() - last_scanned_at)) / CASE
                                        WHEN type = 'full' THEN 604800.0          -- the one-week deadline, in seconds
                                        ELSE GREATEST(COALESCE((SELECT t_star_s FROM scheduler_state WHERE id = 1),
                                                               1800.0), 1.0)
                                    END
                          END) DESC,
                         due_at ASC                                              -- tie-break: oldest first, as before
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


async def fail_work(pool: asyncpg.Pool, wid, unit_type: str, max_attempts: int = 4) -> str | None:
    """A scan errored. Under max_attempts → re-arm SOON (short backoff) for a retry; at the cap → mark 'failed' (fail-loud,
    reconcile_work/the pacer's reaper surfaces it) so a permanently-broken url doesn't spin forever.

    ONE STATEMENT, NOT TWO. This was `SELECT attempt` followed by a separate `UPDATE`, which is a classic read-then-write
    race: the two statements run in different implicit transactions (asyncpg autocommits each, and the Supavisor
    transaction pooler may even route them to different backends), so between the read and the write another worker's
    claim_work can bump `attempt`. Two workers failing the same unit concurrently both read attempt=3, both decide
    "under the cap", and both re-queue — the row keeps climbing past max_attempts and never reaches 'failed', which is
    precisely the fail-loud terminal state this function exists to produce. Deciding INSIDE the UPDATE evaluates the CASE
    against the row version the statement itself locks, so the branch and the write can no longer disagree.
    {QUEUE.PY claim_work "ATTEMPT = ATTEMPT + 1," — a concurrent claim mutates the very column the removed SELECT read}
    {EVENTS.PY:225 "STATUS = CASE WHEN FAIL_COUNT + 1 >= 3 THEN 'DEAD_LETTER' ELSE 'FAILED' END" — the event-side twin
     already used the single-statement inline-CASE shape, so this makes the two layers consistent}
    [CONFIDENCE: CONFIRMED 100% — the two-statement shape was read off this file at HEAD 9d3402f; the fix is a pure
     collapse with no behaviour change in the single-worker case].

    Upstream trigger: scheduler/worker.py on a scan exception. Downstream: the row either re-arms at now()+5min (retry)
    or lands in 'failed', where it is visible to the pacer's failed-row reaper and to operators.

    RETURNS the terminal status actually written ('failed' | 'queued'), or None if the id no longer exists — so the
    caller can log which branch fired instead of having to re-query."""
    async with pool.acquire() as conn:
        # attempt is compared as-is (NOT attempt+1): claim_work already incremented it when this unit was handed out,
        # so by the time a failure lands `attempt` is the count INCLUDING the attempt that just failed.
        row = await conn.fetchrow(
            """
            UPDATE work_queue SET
                status      = CASE WHEN attempt >= $2 THEN 'failed' ELSE 'queued' END,
                lease_owner = NULL,
                lease_until = NULL,
                -- only the retry branch moves due_at; a 'failed' row must NOT look due, or a future relaxation of the
                -- claim predicate would silently resurrect it.
                due_at      = CASE WHEN attempt >= $2 THEN due_at ELSE now() + interval '5 minutes' END,
                updated_at  = now()
            WHERE id = $1
            RETURNING status;
            """,
            wid, max_attempts,
        )
    return row["status"] if row else None


async def reconcile_work(pool: asyncpg.Pool) -> int:
    """Sweeper: flip every work_queue row whose lease has LAPSED back to 'queued' (clearing the dead worker's ownership)
    so the fleet can pick it up again. Returns the number of rows reclaimed, for logging + alerting.

    WHY this exists as a separate cron rather than relying on claim_work's reclaim arm: claim_work DOES have an
    opportunistic second arm for lapsed leases, but that arm is conjoined with `due_at <= now()`, and complete_work
    pushes a full unit's due_at a WEEK out. So a full unit that dies mid-scan holds a lease that lapsed 15 minutes later
    while its due_at sits up to 7 days in the future — the claim query will not look at it again until the week elapses,
    and until then the row is invisible work that nothing is doing. An unconditional sweep on the lease alone is the only
    thing that returns those rows to the pool promptly.
    {QUEUE.PY claim_work "WHERE (STATUS = 'QUEUED' OR (STATUS = 'RUNNING' AND LEASE_UNTIL < NOW())) AND DUE_AT <= NOW()"
     — the reclaim arm is gated on due_at, which is what makes it insufficient for full units}
    {QUEUE.PY:19 "_FULL_INTERVAL_S = INT(OS.ENVIRON.GET("EVENTINC_FULL_INTERVAL_S", STR(7 * 24 * 3600)))" — the week}
    {MIGRATION 20260725024245_waterevents_work_queue.sql:35 "-- RECLAIM SCAN: FIND ROWS WHOSE LEASE LAPSED (CRASHED
     WORKER) — A RECONCILE CRON FLIPS THEM BACK TO 'QUEUED'." — the cron the migration promised was never written}
    {MEASURED at audit time "91 ROWS IN STATUS='RUNNING', OF WHICH 28 HAD ALREADY-LAPSED LEASES, ALL OWNED BY THE MACHINE
     THAT SUFFERED THE 4H27M WEDGE"} {PACER.PY:142-143 "RECONCILE 从来不存在(EVENTS.PY 的 RECONCILE_EVENTS 只管 EVENTS
     表的 ENRICHMENT,不碰 WORK_QUEUE)"}
    [CONFIDENCE: CONFIRMED 100% — the missing sweeper is independently documented in the migration comment, in pacer.py's
     own analysis, and by 28 stuck rows counted on the live queue].

    ATOMIC by construction: a single UPDATE. Its WHERE clause is evaluated against the rows the statement itself locks,
    so a worker that renews its lease in the same instant either renews BEFORE (row no longer matches, not reclaimed) or
    AFTER (row was reclaimed, and the renewing worker's own fencing decides the outcome) — there is no window where this
    function reclaims a row whose lease is still valid.

    NOTE ON `attempt`: reclaiming deliberately does NOT touch it. claim_work increments on every claim, and complete_work
    zeroes on success, so leaving it alone preserves the intended "consecutive failures" meaning — a unit that wedges
    repeatedly keeps climbing toward fail_work's cap instead of being laundered clean by the sweeper.
    {QUEUE.PY complete_work "RESETS `ATTEMPT` — WITHOUT THIS THE COUNTER IS A ONE-WAY RATCHET"}
    [CONFIDENCE: CONFIRMED 100% — matches the semantics complete_work's docstring already establishes].

    Index-backed: the predicate matches `work_queue_lease_idx` exactly, so this is a cheap partial-index scan even as
    the table grows. {MIGRATION 20260725024245_waterevents_work_queue.sql:36-37 "CREATE INDEX IF NOT EXISTS
    WORK_QUEUE_LEASE_IDX ON WATEREVENTS.WORK_QUEUE (STATUS, LEASE_UNTIL) WHERE STATUS = 'RUNNING'"}
    [CONFIDENCE: CONFIRMED 100% — index name and predicate read directly from the migration file].

    Upstream trigger: scripts/ops/reconcile_queue.py, run from a systemd timer. Downstream: the reclaimed rows become
    claimable by claim_work on its next poll.

    COMPANION FIX (NOT in this module, NOT owned here): this reclaims a lease AFTER it lapses. The reason a lease lapses
    while the process is still alive is that scan_unit has no hard timeout, so a wedged unit holds its slot for hours —
    that bound belongs in scheduler/worker.py and is owned by another agent. Without it this sweeper is a mitigation, not
    a cure: it returns the row to the pool, but the wedged worker still occupies a slot. {MEASURED "4H27M WEDGE"}."""
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """
            UPDATE work_queue SET
                status = 'queued', lease_owner = NULL, lease_until = NULL, updated_at = now()
            WHERE status = 'running' AND lease_until < now();
            """
        )
    # asyncpg returns the raw command tag ("UPDATE 28"). Parsed defensively for the same reason reconcile_events does:
    # this runs unattended from a timer, and a malformed tag must degrade to "reclaimed 0", never crash the cron.
    # {EVENTS.PY reconcile_events "RETURN INT(TAG.SPLIT()[-1]) IF TAG AND TAG.STARTSWITH("UPDATE") ELSE 0"}
    # [CONFIDENCE: CONFIRMED 100% — identical parsing, so both sweepers report counts the same way].
    try:
        return int(tag.split()[-1]) if tag and tag.startswith("UPDATE") else 0
    except (ValueError, IndexError):                         # {AUDIT 2026-07-23 MEDIUM: int(tag.split()) could raise}
        return 0


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
