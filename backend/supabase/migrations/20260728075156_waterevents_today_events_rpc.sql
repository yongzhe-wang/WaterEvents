-- today_events RPC — the ENTITY twin of today_pulse: same minute/hour/day buckets, but it returns the events
-- themselves with their metadata instead of just counting them.
--
-- 用一句话讲完: 复用 today_pulse 一模一样的谓词(event_date 周期为当前 AND created_at 落在 1分钟/1小时/1天 窗口内),
-- 但不再 count(*) 而是把命中的 event 连同 title/event_date/event_type/url/ticker/discovered 一起吐出来,按发现时间倒序,
-- 服务端硬上限封顶 → 经 PostgREST 的 /rest/v1/rpc/today_events 暴露给 api_service 的公开 GET /today/events。
--
-- WHY this endpoint can be PUBLIC while /api/events cannot: the predicate is a TIME WINDOW, not an offset. The widest
-- bucket is one day, so the result set is bounded by "what we discovered today" (~237 rows at the measured rate) and
-- can never be paged backwards into the full corpus. /api/events by contrast takes `offset`, which lets a caller walk
-- the entire 149k-row history 1000 at a time — that is why it sits behind the dashboard password and this does not.
-- {MEASURED 2026-07-28 "events total 149398 | created_at > now()-1day 28420 raw | period-matched day bucket 237"}
-- [CONFIDENCE: CONFIRMED 100% — all three counts read off the live REST API with Prefer: count=exact].
--
-- WHY the SAME predicate as today_pulse rather than a fresh one: the two endpoints are published side by side, so a
-- caller WILL compare `buckets.hour` from the pulse against `length(events)` from this one. Any divergence in the
-- period-match logic turns into a "the counter says 21 but the feed gave me 19" bug that is very expensive to chase.
-- The predicate below is copied verbatim from 20260728031746_waterevents_today_pulse_rpc.sql.
-- {20260728031746_waterevents_today_pulse_rpc.sql:53-60 "s.event_date = a.cur_quarter OR ... = a.today"}
-- [CONFIDENCE: CONFIRMED 100% — line-for-line copy; any future edit must be applied to BOTH functions].
--
-- Upstream trigger: api_service GET /today/events. Downstream: nothing writes; STABLE + SECURITY INVOKER, read-only.

-- The index today_pulse's own comment already assumed existed but which was never created: every call scans events
-- filtered on created_at, and the only indexes in this schema's migration history are on work_queue and scan_log.
-- Without it both public endpoints sequential-scan 149k rows on every cache miss (6×/minute at the 10s TTL).
-- DESC because every consumer reads newest-first. Plain CREATE INDEX (not CONCURRENTLY): Supabase runs migrations
-- inside a transaction where CONCURRENTLY is illegal, and at 149k rows the build holds its write lock for well under
-- a second — acceptable against a fleet that writes continuously but tolerates a sub-second stall.
-- {20260728031746_waterevents_today_pulse_rpc.sql:39-40 "NARROW TO THE WIDEST WINDOW WE REPORT (1 DAY) FIRST SO THE
--  CREATED_AT INDEX DOES THE WORK"} {GREP 2026-07-28 backend/supabase/migrations "CREATE INDEX → only work_queue_claim_idx,
--  work_queue_lease_idx, scan_log_ts_idx — no events index exists"}
-- [CONFIDENCE: CONFIRMED 100% — the absent index was verified by enumerating CREATE INDEX across every migration file].
CREATE INDEX IF NOT EXISTS events_created_at_idx ON waterevents.events (created_at DESC);

CREATE OR REPLACE FUNCTION waterevents.today_events(
    tz     text DEFAULT 'UTC',
    bucket text DEFAULT 'hour',
    lim    int  DEFAULT 100
)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = waterevents, pg_catalog
AS $$
WITH bounds AS (
    -- Window per bucket. An unrecognised label falls back to one hour rather than one day: api_service already
    -- rejects anything outside {minute,hour,day} with a 400, so this branch is only reachable by a direct RPC caller,
    -- and the cheaper window is the safer default for something that reached us unvalidated.
    SELECT CASE lower(bucket)
             WHEN 'minute' THEN interval '1 minute'
             WHEN 'day'    THEN interval '1 day'
             ELSE               interval '1 hour'
           END AS win,
           -- Server-side hard cap. The caller's `lim` can only ever SHRINK the page, never grow it past 500 — on an
           -- unauthenticated endpoint the row cap is the whole safety property, so it cannot be a client-side default.
           least(greatest(coalesce(lim, 100), 1), 500) AS cap
),
anchor AS (
    -- "now" in the caller's timezone reduced to the period labels event_date actually uses. Verbatim from today_pulse.
    SELECT (now() AT TIME ZONE tz)                                          AS local_ts,
           (now() AT TIME ZONE tz)::date                                    AS today,
           to_char(now() AT TIME ZONE tz, 'YYYY-MM')                        AS cur_month,
           to_char(now() AT TIME ZONE tz, 'YYYY')                           AS cur_year,
           to_char(now() AT TIME ZONE tz, 'YYYY') || '-Q' ||
             to_char(EXTRACT(quarter FROM now() AT TIME ZONE tz), 'FM9')    AS cur_quarter,
           -- LAST quarter counts as current: in July a company is reporting its Q2 results, so 2026-Q2 is the live
           -- filing period even though the calendar says Q3. {USER 2026-07-28 "the last quarter for e.g. 2q"}.
           to_char((now() AT TIME ZONE tz) - interval '3 months', 'YYYY') || '-Q' ||
             to_char(EXTRACT(quarter FROM (now() AT TIME ZONE tz) - interval '3 months'), 'FM9') AS last_quarter
),
scoped AS (
    -- ONE pass over the bucket window; both the period-matched feed and the raw `fetched` count are FILTERs over it,
    -- mirroring today_pulse's single-scan shape rather than issuing two queries.
    SELECT e.id, e.company_id, e.title, e.event_date, e.event_type,
           e.media_urls, e.source_url, e.created_at
    FROM waterevents.events e CROSS JOIN bounds b
    WHERE e.created_at > now() - b.win
),
flagged AS (
    SELECT s.*,
           -- PERIOD MATCH — event_date is TEXT and only 77.5% of it is a full YYYY-MM-DD; the rest is quarter/month/
           -- year/fiscal granularity, so a coarse label counts as "now" when the period it names CONTAINS today.
           -- pg_input_is_valid guards the full-date branch: 10 rows match the YYYY-MM-DD shape but are not real dates
           -- ('2010-05-00', '2022-02-29') and a bare ::date cast on them aborts the query → a 500 on a public endpoint.
           -- {20260728031746_waterevents_today_pulse_rpc.sql:53-60 — copied verbatim, must stay in sync}
           -- [CONFIDENCE: CONFIRMED 100% — the two invalid values were reproduced against live data in that migration].
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
),
matched AS (
    SELECT * FROM flagged WHERE period_now
),
capped AS (
    -- Newest-first is the contract: a poller that keeps the highest `discovered` it has seen can resume from there.
    SELECT * FROM matched ORDER BY created_at DESC LIMIT (SELECT cap FROM bounds)
)
SELECT jsonb_build_object(
    'now',     to_char(a.local_ts, 'YYYY-MM-DD"T"HH24:MI:SS'),
    'tz',      tz,
    'bucket',  CASE lower(bucket) WHEN 'minute' THEN 'minute' WHEN 'day' THEN 'day' ELSE 'hour' END,
    'anchors', jsonb_build_object('today', a.today, 'month', a.cur_month, 'quarter', a.cur_quarter,
                                  'last_quarter', a.last_quarter, 'year', a.cur_year),
    -- `count` is the SAME number today_pulse reports in buckets.<bucket>; `fetched` is the same as its fetched.<bucket>.
    -- Publishing both lets a caller tell whether a quiet feed means "nothing crawled" or "crawled but nothing current",
    -- which is exactly why today_pulse exposes the two ingredients side by side.
    'count',   (SELECT count(*) FROM matched),
    'fetched', (SELECT count(*) FROM flagged),
    'limit',   (SELECT cap FROM bounds),
    'returned',  (SELECT count(*) FROM capped),
    -- An explicit truncation flag, so a caller never has to infer "did I get everything?" from length == limit.
    'truncated', (SELECT count(*) FROM matched) > (SELECT cap FROM bounds),
    'events', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
                   'id',         c.id,
                   'ticker',     co.ticker,
                   'title',      c.title,
                   'event_date', c.event_date,
                   'event_type', c.event_type,
                   -- First http entry in media_urls = the link a reader should follow, matching what the dashboard
                   -- already surfaces so the API and the UI never disagree about "the" url for an event.
                   -- {frontend/api/today.js:27-30 "return a.find((u) => typeof u === 'string' && u.startsWith('http')) || null"}
                   -- [CONFIDENCE: CONFIRMED 100% — same first-http-wins rule, transcribed from the dashboard helper].
                   'url', (SELECT u FROM jsonb_array_elements_text(
                                          CASE WHEN jsonb_typeof(c.media_urls) = 'array'
                                               THEN c.media_urls ELSE '[]'::jsonb END) AS t(u)
                           WHERE u LIKE 'http%' LIMIT 1),
                   'media_urls', c.media_urls,
                   'source_url', c.source_url,
                   -- Always UTC with an explicit Z, independent of the tz used for the period anchors: the anchors are
                   -- a human calendar question, but `discovered` is the cursor a poller compares against, and a
                   -- cursor that silently shifts with a query parameter is a data-loss bug waiting to happen.
                   'discovered', to_char(c.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
               ) ORDER BY c.created_at DESC)
        FROM capped c
        LEFT JOIN waterevents.companies co ON co.id = c.company_id   -- LEFT: an event outlives its company row
    ), '[]'::jsonb)
)
FROM anchor a;
$$;

-- anon is the PUBLIC role PostgREST maps an unauthenticated request to. EXECUTE only — STABLE + SECURITY INVOKER, so
-- the function reads exactly what anon can already read (anon already holds SELECT on waterevents.events and
-- waterevents.companies) and nothing more. No new privilege is granted by this migration.
GRANT EXECUTE ON FUNCTION waterevents.today_events(text, text, int) TO anon, authenticated, service_role;

COMMENT ON FUNCTION waterevents.today_events(text, text, int) IS
  'Today page event feed: the events whose event_date period is current AND that were discovered within the last minute/hour/day, newest first, capped at 500. Same predicate as today_pulse so the counts agree. Read-only, served via PostgREST so it never consumes the crawl fleet''s Supavisor connections.';
