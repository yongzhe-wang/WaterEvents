-- event_documents — ONE ROW PER (event, source url), holding a PAIR: the prose as markdown, and the structures the
-- markdown's placeholders point at. Replaces event_content_blocks (many rows per source) + event_media_files (one row
-- per office document) with a single shape both extraction paths produce.
--
-- 用一句话讲完: 一个 url 存一行 —— `md` 是非结构化正文,表格位置只留 `[[TABLE:n]]` 占位符;`blocks` 是这些占位符指向的
-- 结构化数据。读的时候把占位符换回表就是完整原文;做 chunk / embedding 的时候 md 是干净散文,不会被一张几百行的表淹掉。
-- {USER 2026-08-05 "WE ONLY KEEP ONE MD + PLACEHODLER FOR GRAPHS AND TABELS USING JSON, SO ONE PAIR STURUTE RAND
--  UNDSTRUCTRUED FOR ANY URL LEVEL"}
-- [CONFIDENCE: CONFIRMED 100% — direct user directive.]
--
-- WHY BLOCKS ARE GONE — three findings, all measured on this database:
--
-- 1. Nothing downstream ever consumed a block AS a block. A full-repo grep for readers of event_content_blocks returns
--    only a dashboard COUNT, the artifact modal's display, and comments. There is no vector store and no retrieval:
--    the "embedding/chunk" grep hits are all false positives — {QWEN_LLM/CLIENT.PY:178 "CHUNK"} is HTTP streaming and
--    {WATERCRAWL/CAPTURE.PY:35 "CHUNKLIST"} is an HLS manifest term.
--
-- 2. The one mechanism still running — per-block content-hash dedup — was destroying content. One page rendered 102
--    date-time lines and stored only 94:
--      {RENDER /render_detail 2026-08-05 "渲染后 日期时间 出现 102 次"} vs {psql "日期时间块 | 94"}
--    The 8 dropped lines were textually identical but belonged to DIFFERENT companies meeting at the same time —
--      {RENDER TEXT "7| ENABLE INJECTIONS, INC." / "9| AUGUST 05, 2026 | 09:00 AM ET"
--                   "11| KINGSTONE COMPANIES, INC." / "13| AUGUST 05, 2026 | 09:00 AM ET"}
--    so dedup collapsed two distinct facts into one. The unique constraint below moves dedup to the level where it is
--    safe: the SOURCE. Two sources saying the same thing stay two rows, because which one said it is the information.
--
-- 3. Block granularity was unbounded in the wrong direction. One event carried 15,923 blocks averaging 47 bytes
--    {psql "MD | 15786 | 47"} — fragments too small to embed, to feed a model, or to read. A document has a natural
--    size; a "block" produced by a paragraph heuristic does not.
--
-- NO BACKFILL. The media pipeline was stopped for this change and its output is being purged, so the old rows are not
-- migrated — they are output of the shape this replaces. The two old tables are RENAMED rather than dropped: 14 MB is
-- a trivial price for a reversible change, and a DROP here would be irreversible for no gain.
--
-- 上游触发: db_media.mark_enriched_media (stage-2 worker). 下游连接: /api/artifact, /api/artifacts, the artifact modal.

-- DROP-then-CREATE, not `create table if not exists`. A killed earlier attempt had already created this table with a
-- DIFFERENT column set (`tables jsonb`, no `n_blocks`), and `if not exists` silently ACCEPTS a mismatched pre-existing
-- shape — it guards against re-creation, not against being wrong. Verified empty before dropping:
-- {psql 2026-08-05 "SELECT COUNT(*) AS 行数 FROM WATEREVENTS.EVENT_DOCUMENTS" -> "0"}
-- [CONFIDENCE: CONFIRMED 100% — row count checked immediately before the drop; no data existed to lose.]
-- Drop the dependent view EXPLICITLY rather than using CASCADE. CASCADE would also remove anything else that grew a
-- dependency on this table without naming it, and a migration should state what it destroys. The view is recreated at
-- the bottom of this file.
-- {psql "ERROR: CANNOT DROP TABLE WATEREVENTS.EVENT_DOCUMENTS BECAUSE OTHER OBJECTS DEPEND ON IT / DETAIL: VIEW
--  WATEREVENTS.EVENT_ARTIFACT_COUNTS DEPENDS ON TABLE WATEREVENTS.EVENT_DOCUMENTS"}
-- [CONFIDENCE: CONFIRMED 100% — the dependency was reported by Postgres on the first attempt.]
drop view if exists waterevents.event_artifact_counts;
drop table if exists waterevents.event_documents;
create table waterevents.event_documents (
    id           uuid primary key default gen_random_uuid(),
    event_id     uuid not null references waterevents.events(id) on delete cascade,   -- documents die with the event
    url          text not null,                          -- THE SOURCE: an html detail page, or a pdf/pptx/docx/xlsx asset
    kind         text not null,                          -- 'html' | 'pdf' | 'pptx' | 'docx' | 'xlsx'
    -- UNSTRUCTURED. Prose in reading order. Where a table or figure stood, this carries a placeholder line —
    -- `[[TABLE:1]]` / `[[FIGURE:1]]`, alone on its line — and nothing else for that element.
    md           text not null,
    -- STRUCTURED. One object per placeholder, in the SAME ORDER the placeholders appear in md, so the pairing is
    -- positional rather than a lookup that can silently miss:
    --   {"id":1,"type":"table","headers":["Q1","Q2"],"rows":[["1","2"]],"caption":""}
    --   {"id":1,"type":"figure","caption":"","alt":"","src":""}
    -- FIGURES: the extractors do not produce figure objects yet. The type is accepted here and rendered by the UI so
    -- adding them later needs no migration, but nothing emits one today — stated plainly rather than implied.
    blocks       jsonb not null default '[]'::jsonb,
    n_pages      int,                                     -- office documents only; NULL for html
    n_chars      int  not null default 0,                 -- length(md), stored so a count never transfers the body
    n_blocks     int  not null default 0,                 -- jsonb_array_length(blocks), same reason
    content_hash text,                                    -- canonical(url) + md head — idempotent re-enrich key
    created_at   timestamptz not null default now(),
    -- ONE document per source, enforced by the schema rather than by convention. This IS the dedup rule now.
    unique (event_id, url)
);

-- The unique constraint already indexes (event_id, url); this covers the event_id-only lookup the API does when it
-- fetches every document for one event.
create index if not exists event_documents_event_idx on waterevents.event_documents (event_id);

grant select on waterevents.event_documents to anon, authenticated, service_role;

-- ── ARCHIVE THE OLD SHAPE ───────────────────────────────────────────────────────────────────────────────────────────
-- RENAME, never DROP. The two tables hold 14 MB of output from the shape being replaced; keeping them costs nothing
-- and makes this migration reversible, while a DROP would make a mistake here permanent.
-- {psql 2026-08-05 "EVENT_CONTENT_BLOCKS | 14 MB | 24692" · "EVENT_MEDIA_FILES | 688 KB | 96"}
-- [CONFIDENCE: CONFIRMED 100% — sizes from pg_total_relation_size on this database.]
alter table if exists waterevents.event_content_blocks rename to event_content_blocks_arch_20260805;
alter table if exists waterevents.event_media_files    rename to event_media_files_arch_20260805;

-- ── THE COUNTS VIEW, REPOINTED ──────────────────────────────────────────────────────────────────────────────────────
-- Its consumers read these exact column names and they must not change: event_id, blocks, files, file_kinds, segments,
-- audio, urls, urls_done, urls_failed, urls_skipped. {API/ARTIFACTS.JS "SELECT=EVENT_ID,BLOCKS,FILES,FILE_KINDS,
-- SEGMENTS,AUDIO,URLS_DONE,URLS_FAILED,URLS_SKIPPED"} [CONFIDENCE: CONFIRMED 100% — read off the handler.]
-- The NAMES survive; what they COUNT changes, because a "block" is now a document:
--   blocks -> html documents,  files -> office documents,  file_kinds -> {"pdf":2,"xlsx":1} over the office kinds.
create or replace view waterevents.event_artifact_counts as
with
docs as (
  select event_id,
         count(*) filter (where kind = 'html')::int  as n_html,
         count(*) filter (where kind <> 'html')::int as n_files
  from waterevents.event_documents group by event_id
),
kinds as (
  -- Per-kind breakdown for the UI, which draws ONE button per document kind rather than one for "files".
  select event_id, jsonb_object_agg(kind, n) as file_kinds
  from (select event_id, kind, count(*)::int as n
        from waterevents.event_documents where kind <> 'html' group by event_id, kind) t
  group by event_id
),
segs as (select event_id, count(*)::int as n from waterevents.event_transcript_segments group by event_id),
aud  as (select event_id, count(*)::int as n from waterevents.event_audio group by event_id),
urls as (
  -- The url ledger tallied BY OUTCOME. This is the only place a per-url failure is recorded, so the failed count is
  -- what tells the dashboard an event is worth opening.
  -- {psql 2026-08-05 over 688 ledger rows "HTML DONE 450 | PDF DONE 80 | HTML FAILED 60 | PDF FAILED 38 |
  --  VIDEO SKIPPED 30 | XLSX FAILED 28 | OTHER SKIPPED 5 | VIDEO FAILED 1 | XLSX DONE 1"} — xlsx at 28 failed against
  -- 1 done is a broken extractor path naming itself, and without this column it is invisible from the UI.
  -- [CONFIDENCE: CONFIRMED 100% — tallied from the live table.]
  select event_id,
         count(*)::int                                   as n,
         count(*) filter (where status = 'done')::int    as n_done,
         count(*) filter (where status = 'failed')::int  as n_failed,
         count(*) filter (where status = 'skipped')::int as n_skipped
  from waterevents.event_media_urls group by event_id
)
select
  coalesce(d.event_id, s.event_id, a.event_id, u.event_id) as event_id,
  coalesce(d.n_html, 0)    as blocks,
  coalesce(d.n_files, 0)   as files,
  k.file_kinds             as file_kinds,
  coalesce(s.n, 0)         as segments,
  coalesce(a.n, 0)         as audio,
  coalesce(u.n, 0)         as urls,
  coalesce(u.n_done, 0)    as urls_done,
  coalesce(u.n_failed, 0)  as urls_failed,
  coalesce(u.n_skipped, 0) as urls_skipped
from docs d
full outer join segs s on s.event_id = d.event_id
full outer join aud  a on a.event_id = coalesce(d.event_id, s.event_id)
full outer join urls u on u.event_id = coalesce(d.event_id, s.event_id, a.event_id)
left join kinds k on k.event_id = coalesce(d.event_id, s.event_id, a.event_id, u.event_id);

grant select on waterevents.event_artifact_counts to anon, authenticated, service_role;
