-- fleet_health: stop 'degraded' from latching on forever, and add the render lane to what it can see.
--
-- 用一句话讲完: verdict 里的 units_failed 是累计值, 只要历史上失败过一个单元就永远 degraded —— 改成"窗口内新增的
-- failed 数", 于是它能亮也能灭; 顺便让它读得到 scan_log.failed_render, 之前它只看得见 VLM 那条腿。
--
-- WHAT WAS WRONG. The verdict CASE had three degraded predicates and two of them self-clear:
--   extract_errors_w  — windowed, falls back to 0 when the window passes
--   orphan_leases     — the reaper clears these every 2 minutes
--   units_failed      — count(*) FILTER (WHERE status = 'failed') over the WHOLE table, all time
-- The third never decreases. A 'failed' row stays 'failed' until somebody requeues it, so the first unit ever to hit
-- the attempt cap pinned the verdict to 'degraded' permanently. Read live today: verdict='degraded', reason='50 units
-- parked in failed', while the same row reported 768 scans, 794 events, 0 extract errors and a 0.1-minute scan age.
-- Every other number said the fleet was healthy, which it was.
--
-- A signal that cannot return to 'ok' is not a signal. It is a check-engine light that was on when you bought the car,
-- and its cost is that the one verdict anybody would actually page on becomes noise.
--
-- WHAT IT IS NOW. The verdict asks whether units are failing NOW — rows that entered 'failed' inside the window — and
-- the all-time total stays in units_failed as a reported number, because the backlog is worth seeing, just not worth
-- calling an incident. A burst of failures still trips degraded; a long-settled backlog does not.
--
-- {DB 2026-08-01 "SELECT * FROM fleet_health(30,5)" -> degraded | 50 units parked in failed | scan_age 0.1 |
--  scans_w 768 | events_w 430 | extract_errors_w 0 | orphan_leases 0}
-- {USER 2026-07-31 "why do we need a degraded" — the question this answers}
-- [CONFIDENCE: CONFIRMED 100% — the latch was read off production; the other two predicates were checked for the same
--  flaw and both are windowed or reaped.]
--
-- ALSO: failed_render joins the picture. Until migration 20260801140500 the column did not exist, so this function
-- could only see extraction failures; a fleet whose renders were all failing showed extract_errors_w = 0 (a page that
-- never loads reaches the VLM zero times) and read healthy. It is a degraded-class signal, windowed like the rest.
--
-- Upstream trigger: deploy/watchdog.sh (every 5 min via .timer). Downstream: the watchdog's restart decision.

-- DROP + CREATE inside ONE transaction. CREATE OR REPLACE cannot add the two new OUT parameters — Postgres refuses
-- with "cannot change return type of existing function" — so the function has to be dropped first. Wrapping both in a
-- transaction makes the window where fleet_health does not exist atomic: deploy/watchdog.sh calls this every 5 minutes
-- via its timer, and a call landing between a bare DROP and CREATE would error and be read as the fleet being
-- unreachable. Inside BEGIN/COMMIT the concurrent caller either sees the old function or the new one, never neither.
-- [CONFIDENCE: CONFIRMED 100% — the CREATE OR REPLACE failure was hit on the first apply attempt against production.]
BEGIN;

DROP FUNCTION IF EXISTS waterevents.fleet_health(integer, integer);

CREATE FUNCTION waterevents.fleet_health(window_min integer DEFAULT 30, min_vlm_calls integer DEFAULT 5)
RETURNS TABLE (
    verdict            text,
    reason             text,
    scan_age_min       numeric,
    event_age_min      numeric,
    scans_w            bigint,
    events_w           bigint,
    extract_errors_w   bigint,
    vlm_calls_w        bigint,
    orphan_leases      bigint,
    units_failed       bigint,
    units_failed_w     bigint,   -- NEW: rows that entered 'failed' inside the window — the un-latched signal
    failed_render_w    bigint    -- NEW: render-lane failures in the window
)
LANGUAGE sql STABLE AS $$
    WITH s AS (
        SELECT count(*)                       AS scans_w,
               coalesce(sum(vlm_calls), 0)    AS vlm_calls_w,
               coalesce(sum(extract_errors),0) AS extract_errors_w,
               coalesce(sum(failed_render), 0) AS failed_render_w
        FROM waterevents.scan_log
        WHERE ts > now() - make_interval(mins => window_min)
    ),
    e AS (
        SELECT count(*) AS events_w FROM waterevents.events
        WHERE created_at > now() - make_interval(mins => window_min)
    ),
    a AS (
        SELECT round(extract(epoch FROM now() - max(ts))/60.0, 1) AS scan_age_min FROM waterevents.scan_log
    ),
    b AS (
        SELECT round(extract(epoch FROM now() - max(created_at))/60.0, 1) AS event_age_min FROM waterevents.events
    ),
    q AS (
        SELECT count(*) FILTER (WHERE status = 'running' AND lease_until < now()) AS orphan_leases,
               count(*) FILTER (WHERE status = 'failed')                          AS units_failed,
               -- updated_at is written by fail_work on the transition, so this is "failed recently", not "failed ever".
               count(*) FILTER (WHERE status = 'failed'
                                  AND updated_at > now() - make_interval(mins => window_min))
                                                                                  AS units_failed_w
        FROM waterevents.work_queue
    )
    SELECT
        -- Order is most-severe first. The two 'down' clauses are unchanged: work flowing with the VLM answering
        -- nothing, and no scan_log activity at all. Those are the outage shapes.
        CASE
            WHEN s.vlm_calls_w >= min_vlm_calls AND e.events_w = 0 THEN 'down'
            WHEN a.scan_age_min IS NULL OR a.scan_age_min > window_min * 2 THEN 'down'
            -- Every degraded predicate is now windowed, so every one of them can clear on its own.
            WHEN s.extract_errors_w > 0 OR q.orphan_leases > 0 OR q.units_failed_w > 0 OR s.failed_render_w > 0
                THEN 'degraded'
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
            WHEN q.units_failed_w > 0
                THEN format('%s units newly parked in failed in %s min (%s total)', q.units_failed_w, window_min, q.units_failed)
            WHEN s.failed_render_w > 0
                THEN format('%s render failures in %s min but events are still landing', s.failed_render_w, window_min)
            ELSE format('%s events from %s scans in %s min (%s units in failed, not recent)',
                        e.events_w, s.scans_w, window_min, q.units_failed)
        END,
        a.scan_age_min, b.event_age_min, s.scans_w, e.events_w, s.extract_errors_w, s.vlm_calls_w,
        q.orphan_leases, q.units_failed, q.units_failed_w, s.failed_render_w
    FROM s, e, a, b, q;
$$;

COMMENT ON FUNCTION waterevents.fleet_health(integer, integer) IS
  'Single liveness oracle for the crawl fleet. Every degraded predicate is windowed so the verdict can return to ok; '
  'the all-time failed backlog is reported in units_failed but does not by itself set a verdict, because a settled '
  'backlog is a queue to work through, not an incident to page on.';

COMMIT;
