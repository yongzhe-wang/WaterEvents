-- today_pulse RPC — the Today page's live counter, exposed through PostgREST rather than the Supavisor pooler.
--
-- 用一句话讲完: 一个只读函数,数「event_date 落在当前周期」AND「created_at 落在最近 1 分钟/1 小时/1 天」的 events,
-- 三个粒度一次扫描算完;经 PostgREST 的 /rest/v1/rpc/today_pulse 暴露给 api_service。
--
-- WHY a function + PostgREST instead of api_service opening its own Postgres connection:
-- the crawl fleet reaches Postgres through Supavisor (transaction mode :6543) and a PUBLIC endpoint that shares that
-- pool can starve the crawler just by being hammered. PostgREST runs its OWN connection pool under the `authenticator`
-- role, entirely separate from Supavisor, so routing the endpoint here means api_service holds ZERO Supavisor
-- connections — the fleet's budget cannot be touched no matter how hard the endpoint is hit. It also adds NO new
-- database credential, which matters in this repo specifically.
-- {MEASURED 2026-07-28 pg_stat_activity "postgres/Supavisor 7 idle + 1 active (the fleet) vs authenticator/PostgREST
--  14.5 5 idle — two independent pools"} {USER 2026-07-28 "we need to separate the supavisor"}
-- [CONFIDENCE: CONFIRMED 100% — the two pools were read off pg_stat_activity as distinct usename/application_name sets].
--
-- Upstream trigger: api_service GET /today/pulse. Downstream: nothing writes; STABLE + SECURITY INVOKER, read-only.

CREATE OR REPLACE FUNCTION waterevents.today_pulse(tz text DEFAULT 'UTC')
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = waterevents, pg_catalog
AS $$
WITH anchor AS (
    -- "now" expressed in the caller's timezone, then reduced to the period labels event_date actually uses.
    SELECT (now() AT TIME ZONE tz)                                          AS local_ts,
           (now() AT TIME ZONE tz)::date                                    AS today,
           to_char(now() AT TIME ZONE tz, 'YYYY-MM')                        AS cur_month,
           to_char(now() AT TIME ZONE tz, 'YYYY')                           AS cur_year,
           to_char(now() AT TIME ZONE tz, 'YYYY') || '-Q' ||
             to_char(EXTRACT(quarter FROM now() AT TIME ZONE tz), 'FM9')    AS cur_quarter,
           -- LAST quarter counts as current: in July a company is reporting its Q2 results, so 2026-Q2 is the
           -- live filing period even though the calendar says Q3. {USER 2026-07-28 "the last quarter for e.g. 2q"}.
           to_char((now() AT TIME ZONE tz) - interval '3 months', 'YYYY') || '-Q' ||
             to_char(EXTRACT(quarter FROM (now() AT TIME ZONE tz) - interval '3 months'), 'FM9') AS last_quarter
),
scoped AS (
    -- Narrow to the widest window we report (1 day) FIRST so the created_at index does the work; every bucket below
    -- is a FILTER over this one pass rather than three separate scans.
    SELECT e.event_date, e.created_at FROM waterevents.events e
    WHERE e.created_at > now() - interval '1 day'
),
flagged AS (
    SELECT s.created_at,
           -- PERIOD MATCH — event_date is TEXT and only 77.5% of it is a full YYYY-MM-DD; the rest is quarter/month/
           -- year/fiscal granularity. Rather than discard that 22.5%, a coarse label counts as "now" when the period
           -- it names CONTAINS today. pg_input_is_valid guards the full-date branch: 10 rows match the YYYY-MM-DD
           -- shape but are not real dates ('2010-05-00', '2022-02-29' — 2022 was not a leap year) and a bare ::date
           -- cast on them aborts the whole query, which would surface as a 500 on a public endpoint.
           -- {MEASURED 2026-07-28 "YYYY-MM-DD 113535 (77.5%) | YYYY-Qn 24341 | YYYY-MM 2514 | YYYY 2184 | 10 invalid"}
           -- [CONFIDENCE: CONFIRMED 100% — both bad values reproduced by aborting a SELECT during this investigation].
           (    s.event_date = a.cur_quarter
             OR s.event_date = a.last_quarter
             OR s.event_date = a.cur_month
             OR s.event_date = a.cur_year
             OR (s.event_date ~ '^\d{4}-(FY|H[12])$' AND left(s.event_date, 4) = a.cur_year)
             OR (s.event_date ~ '^\d{4}-\d{2}-\d{2}$'
                 AND pg_input_is_valid(s.event_date, 'date')
                 AND s.event_date::date = a.today)
           ) AS period_now
    FROM scoped s CROSS JOIN anchor a
)
-- INTERSECTION, not union: an event counts only when its date period is current AND we discovered it inside the
-- window. {USER 2026-07-28 "No not union but interaction"}. Note the minute bucket is legitimately 0 most of the
-- time — over the 24h sampled, only 132 of 1440 minutes (9.2%) had any qualifying row, while 21 of 24 hours did.
-- [CONFIDENCE: CONFIRMED 100% — measured directly; a zero here is sparse data, not a broken query].
SELECT jsonb_build_object(
    'now',     to_char(a.local_ts, 'YYYY-MM-DD"T"HH24:MI:SS'),
    'tz',      tz,
    'anchors', jsonb_build_object('today', a.today, 'month', a.cur_month, 'quarter', a.cur_quarter,
                                  'last_quarter', a.last_quarter, 'year', a.cur_year),
    'buckets', jsonb_build_object(
        'minute', count(*) FILTER (WHERE f.period_now AND f.created_at > now() - interval '1 minute'),
        'hour',   count(*) FILTER (WHERE f.period_now AND f.created_at > now() - interval '1 hour'),
        'day',    count(*) FILTER (WHERE f.period_now AND f.created_at > now() - interval '1 day')),
    -- the two ingredients, so a caller can always tell WHICH side moved a bucket instead of trusting one number
    'fetched', jsonb_build_object(
        'minute', count(*) FILTER (WHERE f.created_at > now() - interval '1 minute'),
        'hour',   count(*) FILTER (WHERE f.created_at > now() - interval '1 hour'),
        'day',    count(*)),
    'period_matched_today', count(*) FILTER (WHERE f.period_now)
)
FROM flagged f CROSS JOIN anchor a
GROUP BY a.local_ts, a.today, a.cur_month, a.cur_quarter, a.last_quarter, a.cur_year;
$$;

-- anon is the PUBLIC role PostgREST maps an unauthenticated request to. EXECUTE only — the function is STABLE and
-- SECURITY INVOKER, so it can read exactly what anon can already read (anon already holds SELECT on waterevents.events)
-- and nothing more. No new privilege is granted by this migration.
GRANT EXECUTE ON FUNCTION waterevents.today_pulse(text) TO anon, authenticated, service_role;

COMMENT ON FUNCTION waterevents.today_pulse(text) IS
  'Today page pulse: events whose event_date period is current AND discovered within the last minute/hour/day. Read-only, served via PostgREST so it never consumes the crawl fleet''s Supavisor connections.';
