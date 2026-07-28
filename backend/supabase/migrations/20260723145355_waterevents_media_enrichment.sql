-- WaterEvents MEDIA-ENRICHMENT schema (stage-2) — normalizes the media_agent output that was previously flattened into
-- a single events.basic_info JSON blob. One table per slice of the media_agent Chart model (chart.py Chart.to_dict):
-- ordered content blocks, transcript segments, parsed office files, audio artifacts, and the per-event url ledger — so
-- downstream can query "every table in this event", "the transcript", "all PDFs" without unpacking a text blob.
-- {USER 2026-07-23 "建独立规范化 media schema"} [CONFIDENCE: CONFIRMED 100% — direct user directive].
-- Applied via the versioned CLI pipeline (supabase db push / psql -f "$DSN"), NOT MCP apply_migration.
-- {ROOT CLAUDE.md rule 20 "migrations go through the canonical version-controlled pipeline"}.

-- gen_random_uuid() lives in pgcrypto on older PG; no-op if present. Created in public BEFORE the search_path switch so
-- the `default gen_random_uuid()` columns resolve it.
create extension if not exists pgcrypto;

-- Pin to WaterEvents' own schema (mirror of the discovery migration): db push runs with NO search_path override, and the
-- worker pool connects with search_path=waterevents (db.py _SCHEMA), so the tables MUST land there, not in public.
-- {DISCOVERY MIGRATION "create schema if not exists waterevents; set search_path = waterevents, public"}.
create schema if not exists waterevents;
set search_path = waterevents, public;

-- ─────────────────────────────────────────────────────────────────────────────
-- event_content_blocks — the basic_info ORDERED blocks (md / list / table) from chart.append_basic_info. One row per
-- block; `ord` preserves reading order across sources; content_hash is the cross-source dedup key (a table shown on the
-- html page AND inside its linked PDF collapses to ONE row). {CHART.PY:65 "basic_info: ordered blocks {type:md|table|
-- list}"; :113 "content hash → cross-source table/paragraph dedup"} [CONFIDENCE: CONFIRMED 100% — chart.py is the model].
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists event_content_blocks (
    id           uuid primary key default gen_random_uuid(),
    event_id     uuid not null references events(id) on delete cascade,   -- an event's blocks die with the event
    ord          int  not null default 0,                 -- position within the event's basic_info (reading order)
    block_type   text not null check (block_type in ('md','list','table')),
    md           text,                                     -- md / list blocks: the markdown text (table blocks leave NULL)
    caption      text,                                     -- table blocks: optional caption
    headers      jsonb,                                    -- table blocks: ["col", ...]
    rows         jsonb,                                    -- table blocks: [["cell", ...], ...]
    content_hash text not null,                            -- chart._hash(block) — append-time dedup key
    source_url   text,                                     -- which page / file this block was extracted from
    created_at   timestamptz not null default now(),
    unique (event_id, content_hash)                        -- the same block from two sources is stored once
);
create index if not exists ecb_event_idx on event_content_blocks (event_id, ord);

-- ─────────────────────────────────────────────────────────────────────────────
-- event_transcript_segments — speaker-annotated dialogue, from WhisperX audio OR an inline html transcript (which routes
-- HERE, never into basic_info). Timestamped audio segments dedup by (speaker,start,text); a timestamp-less inline
-- segment (start NULL) NEVER dedups — a speaker who says "Thank you." twice must not lose one. {CHART.PY:121
-- append_transcript; :131 "Dedup ONLY timestamped segments"} [CONFIDENCE: CONFIRMED 100% — the edge-case is in chart.py].
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists event_transcript_segments (
    id          uuid primary key default gen_random_uuid(),
    event_id    uuid not null references events(id) on delete cascade,
    ord         int  not null default 0,                  -- segment order within the event's transcript
    speaker     text not null default 'SPEAKER_00',       -- v1 placeholder until diarization/name-mapping {USER "first use speaker_00"}
    start_s     numeric,                                   -- seconds into the audio; NULL for a timestamp-less inline (html) segment
    end_s       numeric,
    "text"      text not null,                             -- the spoken words (quoted: text is a type name in some tooling)
    source_url  text,                                      -- the audio / page these segments came from
    seg_hash    text,                                      -- (speaker,start,text) hash — dedup key for timestamped segments
    created_at  timestamptz not null default now()
);
create index if not exists ets_event_idx on event_transcript_segments (event_id, ord);

-- ─────────────────────────────────────────────────────────────────────────────
-- event_media_files — parsed OFFICE documents (pdf / pptx / docx / xlsx), the officeall.DocResult shape: `markdown`
-- (Docling clean full text with tables inline = readable view) + `tables` (structured [{columns,rows}] = JSON view) +
-- n_pages. {CHART.PY:150 append_file "kind ∈ {pdf,pptx,docx,xlsx}", "markdown + tables"} [CONFIDENCE: CONFIRMED 100%].
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists event_media_files (
    id           uuid primary key default gen_random_uuid(),
    event_id     uuid not null references events(id) on delete cascade,
    url          text not null,                            -- the source document url
    kind         text not null check (kind in ('pdf','pptx','docx','xlsx')),
    markdown     text,                                     -- Docling markdown (prose + inline tables)
    tables       jsonb not null default '[]'::jsonb,       -- structured [{columns,rows}] for the JSON view
    n_pages      int  not null default 0,
    content_hash text not null,                            -- dedup a doc reached via two urls
    created_at   timestamptz not null default now(),
    unique (event_id, content_hash)
);
create index if not exists emf_event_idx on event_media_files (event_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- event_audio — audio artifacts (the mp3 downloaded + transcribed). The transcript segments derived from it live in
-- event_transcript_segments (linked by source_url). {CHART.PY:146 append_audio "{url,local_path,duration_s}"}.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists event_audio (
    id          uuid primary key default gen_random_uuid(),
    event_id    uuid not null references events(id) on delete cascade,
    url         text not null,
    local_path  text,                                      -- where the mp3 was staged (transient; may be empty)
    duration_s  numeric,
    created_at  timestamptz not null default now(),
    unique (event_id, url)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- event_media_urls — the per-event URL LEDGER (chart.urls): every url the close-loop knows about, its kind (html/pdf/…/
-- other) and processing status. canon_key is chart._canon (tracking-param-stripped) so a utm-tagged dupe collapses to
-- one row. {CHART.PY:71 "urls: canonical → {url,kind,status}"; ROUTER.PY:18-19 KIND_* html/pdf/pptx/docx/xlsx/audio/
-- video/other} [CONFIDENCE: CONFIRMED 100% — the 8 KIND_* strings are the router's classify() output domain].
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists event_media_urls (
    id         uuid primary key default gen_random_uuid(),
    event_id   uuid not null references events(id) on delete cascade,
    url        text not null,                             -- the first-seen url form (canon_key is the dedup key)
    canon_key  text not null,                             -- chart._canon(url)
    kind       text not null check (kind in ('html','pdf','pptx','docx','xlsx','audio','video','other')),
    status     text not null default 'pending' check (status in ('pending','done','failed','skipped')),
    created_at timestamptz not null default now(),
    unique (event_id, canon_key)                          -- one row per distinct resource in this event
);
create index if not exists emu_event_idx on event_media_urls (event_id, status);
