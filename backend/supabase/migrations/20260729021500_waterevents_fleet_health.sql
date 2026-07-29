-- fleet_health — ONE definition of "is this system working", consumed by everything that asks.
--
-- 用一句话讲完: 给 scan_log 补一列 extract_errors(抽取硬失败数,之前只存在于进程日志里、数据库看不见), 然后建一个
-- fleet_health() 函数把「产出」而不是「活动」作为健康判据 —— watchdog / dashboard / canary 三个消费者都读它, 所以
-- 它们不可能再各自算出互相矛盾的结论。
--
-- WHY this exists, concretely. On 2026-07-28 the pod's vLLM died for 11h52m. Every layer below detected it: extract.py
-- separated a transport `_error` from "0 events", engine.py counted each one and printed ~18,000 loud banners. But the
-- only thing that reached the DATABASE was scan_log rows saying "a scan happened, it found 0 events" — which is exactly
-- what a healthy hash-gate skip also writes. With that column missing, NO query could tell the two apart, so every
-- consumer built its own guess out of the columns that did exist, and all of them guessed "healthy":
--   • the watchdog asked "is scan_log still moving" — it was, so it restarted nothing and logged healthy every 5 min
--   • the dashboard asked "was each hub scanned within 24h" — it was, so it reported 0 hubs not refreshed
--   • work_queue.failed stayed 0 because worker.py called complete_work() on every one of those empty scans
-- Three different questions, three different code paths, all answering a question that was not "did we produce anything".
-- {W1.LOG 2026-07-29 "1628× ⛔ EXTRACT FAILED ... APIConnectionError: Connection error. — page's events LOST"}
-- {DB 2026-07-29 "2,312 UNITS CALLED THE VLM AND GOT NOTHING; 3,858 WERE LEGITIMATE HASH-GATE SKIPS"}
-- [CONFIDENCE: CONFIRMED 100% — both figures counted live off work_queue.last_vlm_calls during the outage; the
--  legitimate-vs-damaged split is only computable BECAUSE last_vlm_calls exists, which is the same argument for adding
--  extract_errors to scan_log.]
--
-- Upstream: storage/events.py log_scan writes the new column once per finished unit.
-- Downstream: deploy/watchdog.sh, frontend/api/today.js and the canary all call fleet_health() instead of inventing
-- their own predicate.

-- ── 1. THE MISSING COLUMN ────────────────────────────────────────────────────────────────────────────────────────
-- extract_errors = pages whose events were LOST to a hard VLM/transport failure. NOT the same as `events = 0`:
--   events=0, extract_errors=0  → hash-gate skip or a genuinely empty page   → healthy, expected, the common case
--   events=0, extract_errors>0  → we rendered, called the VLM, got nothing   → the system is broken right now
-- Default 0 so every historical row reads as "no known failures" rather than NULL — pre-outage rows genuinely had none
-- recorded, and a NULL would force every consumer to write coalesce() forever.
ALTER TABLE waterevents.scan_log ADD COLUMN IF NOT EXISTS extract_errors int NOT NULL DEFAULT 0;

-- Partial index: every health query filters on "recent AND failing", and failures are rare in steady state, so indexing
-- only the failing rows keeps this tiny while making the hot predicate an index scan instead of a window scan.
CREATE INDEX IF NOT EXISTS scan_log_extract_errors_idx
    ON waterevents.scan_log (ts DESC) WHERE extract_errors > 0;

-- ── 2. THE SINGLE HEALTH DEFINITION ──────────────────────────────────────────────────────────────────────────────
-- DROP the earlier single-argument signature first. `CREATE OR REPLACE FUNCTION` matches on the FULL signature, so
-- adding a parameter creates a SECOND overload instead of replacing the first — and then `fleet_health(15)` fails with
-- "could not choose a best candidate function", i.e. the health oracle itself becomes uncallable. Caught by calling it
-- immediately after applying; left here as an idempotent guard so a replay from any prior state converges to one
-- function. [CONFIDENCE: CONFIRMED 100% — the ambiguity error was reproduced against the live database.]
DROP FUNCTION IF EXISTS waterevents.fleet_health(int);

-- min_vlm_calls = how many model invocations must have happened before "zero events" is treated as proof of breakage
-- rather than a slow minute. Measured basis: the outage ran 380-1015 VLM calls per HOUR (~95-250 per 15-min window),
-- so 10 trips within the first window while being far above the handful a genuinely quiet stretch produces.
CREATE OR REPLACE FUNCTION waterevents.fleet_health(window_min int DEFAULT 15, min_vlm_calls int DEFAULT 10)
RETURNS TABLE (
    verdict            text,      -- 'ok' | 'degraded' | 'down'
    reason             text,      -- human-readable WHY, so a caller never has to re-derive it
    scan_age_min       numeric,   -- minutes since the newest scan_log row  (ACTIVITY — the old, insufficient signal)
    event_age_min      numeric,   -- minutes since the newest event         (PRODUCTION — the signal that mattered)
    scans_w            bigint,    -- scans inside the window
    events_w           bigint,    -- events written inside the window
    extract_errors_w   bigint,    -- extraction hard-failures inside the window
    vlm_calls_w        bigint,    -- VLM invocations inside the window (attempts, including failed ones)
    orphan_leases      bigint,    -- running rows whose lease has lapsed (nothing is actually working them)
    units_failed       bigint     -- work_queue rows parked in 'failed'
)
LANGUAGE sql STABLE AS $$
    WITH w AS (SELECT now() - make_interval(mins => window_min) AS since),
    s AS (
        SELECT count(*)                        AS scans_w,
               coalesce(sum(extract_errors),0) AS extract_errors_w,
               coalesce(sum(vlm_calls),0)      AS vlm_calls_w
        FROM waterevents.scan_log, w WHERE ts >= w.since
    ),
    e AS (SELECT count(*) AS events_w FROM waterevents.events, w WHERE created_at >= w.since),
    a AS (
        SELECT round(extract(epoch FROM now() - max(ts))/60.0, 1) AS scan_age_min
        FROM waterevents.scan_log
    ),
    b AS (
        SELECT round(extract(epoch FROM now() - max(created_at))/60.0, 1) AS event_age_min
        FROM waterevents.events
    ),
    q AS (
        SELECT count(*) FILTER (WHERE status = 'running' AND lease_until < now()) AS orphan_leases,
               count(*) FILTER (WHERE status = 'failed')                          AS units_failed
        FROM waterevents.work_queue
    )
    SELECT
        -- VERDICT — ordered most-severe first, and the FIRST clause is the failure mode that went undetected for
        -- 11h52m: work is flowing (scans_w > 0) and the VLM is being called, yet nothing comes back. Any predicate
        -- built on activity alone reports healthy here, which is precisely what happened.
        -- PRIMARY PREDICATE: the VLM was invoked and produced NOTHING.
        -- This deliberately does NOT depend on extract_errors, and that independence is the point. extract_errors only
        -- exists once the worker code that records it is deployed, so a health oracle keyed on it alone is blind
        -- exactly when a deploy is stale or a NEW failure mode appears that nobody instrumented. vlm_calls and events
        -- have both existed since the table was created, so this predicate is true of the 2026-07-28 outage in the
        -- historical data, with no backfill.
        -- WHY it cannot false-positive on a quiet period: a hash-gate skip increments vlm_skipped, NOT vlm_calls. A
        -- genuinely idle stretch therefore has vlm_calls = 0 and never trips this. Only "we actually asked the model,
        -- repeatedly, and got nothing back" does.
        -- {DB 2026-07-29 hourly replay — 11 consecutive hours of vlm_calls 380..1015 against events 0, bracketed by
        --  healthy hours 13:00 (694 calls / 2671 events) and 02:00 (141 calls / 423 events)}
        -- [CONFIDENCE: CONFIRMED 100% — the separation is total across the whole 16h replay; no healthy hour has
        --  vlm_calls > 0 with events = 0, and no outage hour lacks it.]
        CASE
            WHEN s.vlm_calls_w >= min_vlm_calls AND e.events_w = 0 THEN 'down'
            WHEN a.scan_age_min IS NULL OR a.scan_age_min > window_min * 2 THEN 'down'
            WHEN s.extract_errors_w > 0 OR q.orphan_leases > 0 OR q.units_failed > 0 THEN 'degraded'
            ELSE 'ok'
        END,
        CASE
            WHEN s.vlm_calls_w >= min_vlm_calls AND e.events_w = 0
                THEN format('extraction is producing nothing: %s VLM calls, %s extract errors, 0 events in %s min (%s scans still running)',
                            s.vlm_calls_w, s.extract_errors_w, window_min, s.scans_w)
            WHEN a.scan_age_min IS NULL OR a.scan_age_min > window_min * 2
                THEN format('no scan_log activity for %s min', coalesce(a.scan_age_min::text, 'ever'))
            WHEN s.extract_errors_w > 0
                THEN format('%s extraction failures in %s min but events are still landing', s.extract_errors_w, window_min)
            WHEN q.orphan_leases > 0 THEN format('%s lapsed leases awaiting the reaper', q.orphan_leases)
            WHEN q.units_failed > 0  THEN format('%s units parked in failed', q.units_failed)
            ELSE format('%s events from %s scans in %s min', e.events_w, s.scans_w, window_min)
        END,
        a.scan_age_min, b.event_age_min, s.scans_w, e.events_w, s.extract_errors_w, s.vlm_calls_w,
        q.orphan_leases, q.units_failed
    FROM s, e, a, b, q;
$$;

-- Readable by the dashboard's anon key: it is aggregate health, contains no row-level data, and the whole point is that
-- one definition serves every consumer — a function only the backend could call would push the dashboard straight back
-- to inventing its own predicate, which is the failure this migration exists to end.
GRANT EXECUTE ON FUNCTION waterevents.fleet_health(int, int) TO anon, authenticated;

-- ── 3. WHY NO ALERTING LIVES HERE ────────────────────────────────────────────────────────────────────────────────
-- This function only ANSWERS. It deliberately does not restart, page, or write — a health oracle that also acts cannot
-- be queried safely from a dashboard, and every consumer would then need to know whether calling it had side effects.
