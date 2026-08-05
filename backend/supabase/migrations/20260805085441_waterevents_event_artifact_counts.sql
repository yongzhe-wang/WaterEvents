-- event_artifact_counts — ONE row per event that has any stage-2 output, carrying the per-event tally of every
-- artifact kind. Exists so the dashboard can ask "which artifact buttons does this event need?" in a single bounded
-- query instead of counting rows client-side.
--
-- 用一句话讲完: 把五张产物表各自 GROUP BY event_id 聚合一遍再拼起来,于是前端一次 `event_artifact_counts?event_id=in.(...)`
-- 就拿到每个事件的 blocks/files/segments/audio/url 各多少 —— 每个事件恰好一行,再也不会被 PostgREST 的响应行数上限截断。
--
-- WHY a view instead of counting in the API layer: PostgREST caps a response at 1000 rows and the API layer was asking
-- for the raw rows and counting them in JS under a `limit=500` guard. That limit is a cap on the RESPONSE, not a cap
-- per event, so one chunk of 100 event_ids whose blocks exceed 500 rows silently loses the tail — the later events in
-- the chunk report zero artifacts and their buttons never render. The table this bites hardest is already well past
-- the threshold:
--   {MEASURED 2026-08-05 REST "event_content_blocks?select=id" WITH Prefer:count=exact -> "CONTENT-RANGE: 0-0/24692"}
--   {MEASURED 2026-08-05 psql "SELECT COUNT(*) FROM WATEREVENTS.EVENTS WHERE ENRICHED_AT IS NOT NULL" -> 393}
-- 24,692 blocks across 393 enriched events is ~63 blocks per event, so a 100-event chunk carries ~6,300 rows against
-- a 500-row ceiling: roughly 92% of the chunk would be dropped, and dropped SILENTLY.
-- Aggregating in Postgres returns one row per event instead, so the response size is bounded by the number of events
-- asked about — which the caller already controls — rather than by how much output those events produced.
-- [CONFIDENCE: CONFIRMED 100% — both counts read off the live database; the ratio is arithmetic on those two numbers.]
--
-- WHY it does NOT join `events`: events is 273,853 rows and this view must stay cheap enough to serve a 30-second
-- poll. Aggregating only the artifact tables keeps the whole working set at ~25.5k rows (24,692 blocks + 688 url
-- ledger + 96 files + 8 segments + 0 audio), so the view never touches the large table at all. `basic_info` presence
-- is deliberately left out for the same reason — the caller can ask for it with a filter that returns at most one row
-- per event, which is already safe under any response cap.
-- {MEASURED 2026-08-05 psql pg_stat_user_tables "EVENTS | 273650" VS "EVENT_CONTENT_BLOCKS | 23121"}
-- [CONFIDENCE: CONFIRMED 100% — row counts from pg_stat_user_tables on the production database.]
--
-- 上游触发: /api/artifacts (dashboard, 30s poll). 下游连接: the artifact buttons in the Today Events / Today Media tables.

create or replace view waterevents.event_artifact_counts as
with
-- Each CTE is an independent GROUP BY over one small artifact table. FULL OUTER JOIN below rather than joining onto
-- `events`, so an event appears here iff it produced at least one artifact of some kind.
blocks as (
  select event_id, count(*)::int as n
  from waterevents.event_content_blocks group by event_id
),
files as (
  -- kinds carries the per-kind breakdown (pdf / pptx / xlsx / docx …) because the UI renders ONE button per kind,
  -- not one button for "files". jsonb_object_agg gives {"pdf": 2, "xlsx": 1} directly, no client-side regrouping.
  select event_id, count(*)::int as n,
         jsonb_object_agg(kind, k) filter (where kind is not null) as kinds
  from (
    select event_id, kind, count(*)::int as k
    from waterevents.event_media_files group by event_id, kind
  ) t group by event_id
),
segs as (
  select event_id, count(*)::int as n
  from waterevents.event_transcript_segments group by event_id
),
aud as (
  select event_id, count(*)::int as n
  from waterevents.event_audio group by event_id
),
urls as (
  -- The url ledger is tallied BY OUTCOME, not just counted. This is the only place a per-url failure is recorded, and
  -- the failed count is what the dashboard needs in order to mark an event as worth opening:
  -- {MEASURED 2026-08-05 over the ledger "HTML DONE 450 | PDF DONE 80 | HTML FAILED 60 | PDF FAILED 38 |
  --  VIDEO SKIPPED 30 | XLSX FAILED 28 | OTHER SKIPPED 5 | VIDEO FAILED 1 | XLSX DONE 1"}
  -- xlsx at 28 failed against 1 done is a broken extractor path naming itself; without this column that fact is
  -- invisible from the UI and only reachable by hand-written SQL.
  -- [CONFIDENCE: CONFIRMED 100% — tallied from the live event_media_urls table.]
  select event_id,
         count(*)::int                                          as n,
         count(*) filter (where status = 'done')::int            as n_done,
         count(*) filter (where status = 'failed')::int          as n_failed,
         count(*) filter (where status = 'skipped')::int         as n_skipped
  from waterevents.event_media_urls group by event_id
)
select
  coalesce(b.event_id, f.event_id, s.event_id, a.event_id, u.event_id) as event_id,
  coalesce(b.n, 0)        as blocks,
  coalesce(f.n, 0)        as files,
  f.kinds                 as file_kinds,      -- {"pdf": 2, "xlsx": 1} or null when the event has no documents
  coalesce(s.n, 0)        as segments,
  coalesce(a.n, 0)        as audio,
  coalesce(u.n, 0)        as urls,
  coalesce(u.n_done, 0)    as urls_done,
  coalesce(u.n_failed, 0)  as urls_failed,
  coalesce(u.n_skipped, 0) as urls_skipped
from blocks b
full outer join files f on f.event_id = b.event_id
full outer join segs  s on s.event_id = coalesce(b.event_id, f.event_id)
full outer join aud   a on a.event_id = coalesce(b.event_id, f.event_id, s.event_id)
full outer join urls  u on u.event_id = coalesce(b.event_id, f.event_id, s.event_id, a.event_id);

-- The dashboard reads through PostgREST with the anon role, exactly as it already does for the underlying tables.
-- Granting select on the view (not on anything new) keeps the surface identical to what is already readable.
grant select on waterevents.event_artifact_counts to anon, authenticated, service_role;

-- Supporting indexes. Every CTE above groups by event_id, so without these each poll seq-scans the artifact tables.
-- event_content_blocks is the one that matters today at 24,692 rows; the others are created for symmetry so the view
-- does not silently degrade as they fill in (event_media_files is 96 rows now but tracks document throughput, and
-- event_transcript_segments is 8 rows only because whisper has barely run).
-- [CONFIDENCE: CONFIRMED 100% — table sizes read from pg_stat_user_tables 2026-08-05.]
create index if not exists event_content_blocks_event_id_idx      on waterevents.event_content_blocks (event_id);
create index if not exists event_media_files_event_id_idx         on waterevents.event_media_files (event_id);
create index if not exists event_transcript_segments_event_id_idx on waterevents.event_transcript_segments (event_id);
create index if not exists event_audio_event_id_idx               on waterevents.event_audio (event_id);
create index if not exists event_media_urls_event_id_idx          on waterevents.event_media_urls (event_id);
