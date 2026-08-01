-- fleet_health: threshold the two predicates whose NORMAL value is nonzero, so 'degraded' means abnormal again.
--
-- 用一句话讲完: 上一个 migration 把 units_failed 解了闩, 但同时把 failed_render_w > 0 和 orphan_leases > 0 留成了
-- degraded 条件 —— 而这两个量在健康运转下本来就大于 0, 于是常亮灯只是换了个名字。改成按比例/按滞留时长判断。
--
-- MY OWN REGRESSION, CAUGHT BY SAMPLING THE THING I HAD JUST CHANGED. Eight consecutive reads, 25 seconds apart:
--
--   degraded | 3 lapsed leases awaiting the reaper
--   degraded | 3 lapsed leases awaiting the reaper
--   degraded | 1 lapsed leases awaiting the reaper
--   degraded | 10 units newly parked in failed in 30 min (50 total)
--   degraded | 10 render failures in 30 min but events are still landing
--   degraded | 37 render failures in 30 min but events are still landing
--   degraded | 49 render failures in 30 min but events are still landing
--
-- The unlatch worked — the reason moves now, which the old all-time count could never do. But it never reached 'ok',
-- and it never would have, because:
--
--   * A web crawler's baseline render-failure rate is not zero. Measured over the same window: 10 failures against
--     667 scans and 858 pages, about 1.5%. Hosts go down, time out, and wall us; that is the job, not an incident.
--   * Leases lapse continuously and the reaper sweeps every 2 minutes, so at any instant a handful are awaiting it.
--     A lease that lapsed 20 seconds ago is the system working. One that lapsed 10 minutes ago means the reaper is
--     not running, which is worth knowing — and the old predicate could not tell those apart.
--
-- So the previous migration traded one permanently-on light for another. This one asks the question that was meant:
-- is the failure rate ABNORMAL, and are leases actually STUCK.
--
--   failed_render_w  → degrade when failures exceed 25% of pages rendered in the window, with a floor of 20 failures
--                      so a quiet window with 2 pages and 1 failure does not read as a 50% catastrophe.
--   orphan_leases    → count only leases lapsed longer than 3× the reaper's 2-minute period. Below that they are
--                      in-flight work the reaper has not reached yet.
--
-- {MEASURED 2026-08-01 the eight samples above, taken deliberately to check whether the verdict could reach ok}
-- {MEASURED 2026-08-01 "scans 768 | pages 858 | vlm 220 | render_fail 10 | extract_fail 0 | events 794" over 30 min
--  — the healthy baseline, in which failed_render is 10 and not 0}
-- [CONFIDENCE: CONFIRMED 100% — the regression is my own, observed in production within minutes of introducing it.]
--
-- extract_errors_w keeps its `> 0` test on purpose. Its healthy value IS zero: measured 0 across the same window and
-- across 34,778 historical rows. The VLM either answers or it does not, so any nonzero count is genuinely notable.
--
-- Upstream trigger: deploy/watchdog.sh every 5 min. Downstream: the watchdog's restart decision, which debounces on
-- 3 consecutive strikes — a debounce that is worthless if the signal is always on.

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
    orphan_leases      bigint,   -- only leases STUCK past 3× the reaper period; in-flight ones are not counted
    units_failed       bigint,
    units_failed_w     bigint,
    failed_render_w    bigint,
    render_pages_w     bigint    -- NEW: the denominator, so the failure RATE is readable and not just the numerator
)
LANGUAGE sql STABLE AS $$
    WITH s AS (
        SELECT count(*)                        AS scans_w,
               coalesce(sum(vlm_calls), 0)     AS vlm_calls_w,
               coalesce(sum(extract_errors),0) AS extract_errors_w,
               coalesce(sum(failed_render), 0) AS failed_render_w,
               coalesce(sum(render_pages), 0)  AS render_pages_w
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
        -- 6 minutes = 3× the reaper timer's 2-minute period. A lease that lapsed more recently than that is work the
        -- reaper simply has not swept yet, which is the steady state, not a fault.
        SELECT count(*) FILTER (WHERE status = 'running'
                                  AND lease_until < now() - interval '6 minutes')     AS orphan_leases,
               count(*) FILTER (WHERE status = 'failed')                              AS units_failed,
               count(*) FILTER (WHERE status = 'failed'
                                  AND updated_at > now() - make_interval(mins => window_min))
                                                                                      AS units_failed_w
        FROM waterevents.work_queue
    )
    SELECT
        CASE
            WHEN s.vlm_calls_w >= min_vlm_calls AND e.events_w = 0 THEN 'down'
            WHEN a.scan_age_min IS NULL OR a.scan_age_min > window_min * 2 THEN 'down'
            WHEN s.extract_errors_w > 0 THEN 'degraded'
            WHEN q.orphan_leases > 0 THEN 'degraded'
            WHEN q.units_failed_w > 0 THEN 'degraded'
            -- Rate, with an absolute floor so a nearly-idle window cannot produce a scary percentage from 1 failure.
            WHEN s.failed_render_w >= 20 AND s.render_pages_w > 0
                 AND s.failed_render_w::numeric / s.render_pages_w > 0.25 THEN 'degraded'
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
            WHEN q.orphan_leases > 0
                THEN format('%s leases stuck over 6 min — is the reaper running?', q.orphan_leases)
            WHEN q.units_failed_w > 0
                THEN format('%s units newly parked in failed in %s min (%s total)', q.units_failed_w, window_min, q.units_failed)
            WHEN s.failed_render_w >= 20 AND s.render_pages_w > 0
                 AND s.failed_render_w::numeric / s.render_pages_w > 0.25
                THEN format('render failing on %s of %s pages (%s%%) in %s min',
                            s.failed_render_w, s.render_pages_w,
                            round(100.0 * s.failed_render_w / s.render_pages_w), window_min)
            ELSE format('%s events from %s scans in %s min (%s/%s render failures, %s units in failed)',
                        e.events_w, s.scans_w, window_min, s.failed_render_w, s.render_pages_w, q.units_failed)
        END,
        a.scan_age_min, b.event_age_min, s.scans_w, e.events_w, s.extract_errors_w, s.vlm_calls_w,
        q.orphan_leases, q.units_failed, q.units_failed_w, s.failed_render_w, s.render_pages_w
    FROM s, e, a, b, q;
$$;

COMMENT ON FUNCTION waterevents.fleet_health(integer, integer) IS
  'Single liveness oracle for the crawl fleet. Every degraded predicate is windowed AND thresholded so the verdict can '
  'reach ok during normal operation: a crawler''s baseline render-failure rate is ~1.5%, and leases are always briefly '
  'lapsed between reaper sweeps. Nonzero is not the same as abnormal, and a light that is always on is not a signal.';

COMMIT;
