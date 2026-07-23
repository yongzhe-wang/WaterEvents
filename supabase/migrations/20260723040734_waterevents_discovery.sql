-- WaterEvents discovery schema — the `companies` discovery queue + the `events` seam table.
-- WHY this migration exists: the event_agent discovery worker (agent/event_agent/worker.py) needs a durable
-- work-queue (companies it claims one-at-a-time via SKIP LOCKED) and a durable sink (events it batch-flushes with
-- commit-then-flip + idempotent ON CONFLICT). {DESIGN wlkrnxklp "events 表 = THE SEAM (产出表 + stage-2 work queue)"}
-- [CONFIDENCE: CONFIRMED 100% — the two-stage prod design chose fully-decoupled 2-fleet with the events table as the seam].
-- Applied via the versioned CLI pipeline: supabase migration new -> edit -> supabase db push (NOT MCP apply_migration).
-- {ROOT CLAUDE.md rule 20 "migrations go through the canonical version-controlled pipeline ... supabase db push"}.

-- gen_random_uuid() lives in pgcrypto on older PG; no-op if already present.
create extension if not exists pgcrypto;

-- ─────────────────────────────────────────────────────────────────────────────
-- companies — the DISCOVERY queue. One row per company to crawl. A worker claims exactly one (SKIP LOCKED), runs the
-- whole in-memory BFS, then flips the row to `discovered`. The lease columns make a crashed worker's company reclaimable.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists companies (
    id                   uuid primary key default gen_random_uuid(),
    ir_url               text not null,                       -- the Investor-Relations seed URL the BFS starts from
    -- lifecycle: queued -> discovering -> discovered | discovered_partial | failed. `discovered_partial` = the crawl
    -- finished but with dropped pages (fail-loud incomplete) so the caller knows the event list is NOT the whole truth.
    -- {CRAWL.PY "status = 'ok' if (failed_render == 0 and failed_extract == 0) else 'incomplete'"}.
    status               text not null default 'queued'
                         check (status in ('queued','discovering','discovered','discovered_partial','failed')),
    lease_owner          text,                                -- which worker holds this row (worker_id); NULL when free
    lease_until          timestamptz,                         -- soft lease; a heartbeat renews it every ~60s during the crawl
    lease_hard_deadline  timestamptz,                         -- absolute cap (claim + Nh): even a live heartbeat can't hold past this
    attempt              int  not null default 0,             -- times this company has been claimed (retry accounting)
    run_id               text,                                -- the batch-run this company was processed under
    event_count          int  not null default 0,            -- events written for this company (completion-barrier input)
    pages                int,                                 -- crawl_company result: pages visited
    failed_render        int,                                 -- crawl_company result: pages that failed to render (fail-loud)
    failed_extract       int,                                 -- crawl_company result: pages where the LLM hard-failed (fail-loud)
    trace_dir            text,                                -- where this company's per-page audit trail was written
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

-- claim index — the hot query is "give me a queued OR lease-expired company". Partial index keeps it tiny as the table
-- fills with finished (discovered) rows that the claim never scans.
create index if not exists companies_claimable_idx
    on companies (status, lease_until)
    where status in ('queued','discovering');

-- ─────────────────────────────────────────────────────────────────────────────
-- events — THE SEAM: discovery's OUTPUT table AND the stage-2 (enrichment) work-queue. Discovery writes rows with
-- status='discovered' + basic_info NULL. The enrichment fleet (a DIFFERENT session/agent) later claims discovered rows
-- and fills basic_info. The claim_token/lease/fail_count/next_retry_at columns below are the ENRICHMENT lifecycle —
-- included here nullable for forward-compat so the enrichment session doesn't have to ALTER a hot table; discovery
-- never touches them. {DESIGN wlkrnxklp "events (THE SEAM — 产出表 + stage-2 work queue)"}.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists events (
    id                uuid primary key default gen_random_uuid(),
    company_id        uuid not null references companies(id) on delete cascade,
    run_id            text,
    -- dedup_key = the STABLE identity of an event across re-crawls. It must NOT change as media_urls grows (a crash +
    -- re-crawl re-discovers the same event, sometimes with more media), so it is derived from the event's PRIMARY url
    -- (its detail page = urls[0], canonicalised) NOT from the whole url set. The UNIQUE(company_id, dedup_key) below +
    -- ON CONFLICT DO UPDATE (merge media) is what makes a re-crawl idempotent instead of duplicating events.
    -- {DB.PY "_dedup_key = _canon(urls[0])"} [CONFIDENCE: CONFIRMED 100% — primary-url identity survives media growth].
    dedup_key         text not null,
    title             text,                                  -- discovery metadata (enrichment may confirm/complete it)
    event_date        text,                                  -- kept as text: the model records whatever granularity the page shows (YYYY / YYYY-MM / YYYY-Q1 / YYYY-MM-DD)
    event_type        text,                                  -- earnings|press_release|presentation|filing|webcast|conference|shareholder_meeting|dividend|other
    media_urls        jsonb not null default '[]'::jsonb,    -- the event's urls[] list: detail page + PDF/slides/mp3/webcast/transcript/filing
    -- ── enrichment columns (nullable; discovery leaves them untouched) ──
    basic_info        text,                                  -- enrichment output: generative structured-markdown full page content (NULL until enriched)
    content_hash      text,                                  -- enrichment content SimHash (near-dup dedup)
    status            text not null default 'discovered'
                      check (status in ('discovered','rendering','enriched','failed','dead_letter')),
    fail_reason       text,                                  -- enrichment failure taxonomy (render_timeout / vlm_5xx / vlm_schema_invalid / output_truncated / unrenderable_asset)
    fail_count        int  not null default 0,
    next_retry_at     timestamptz,                           -- enrichment backoff+jitter; claim orders by this so retried rows sink
    claim_token       uuid,                                  -- enrichment fencing token (defeats lease-expiry split-brain)
    lease_until       timestamptz,
    created_at        timestamptz not null default now(),
    enriched_at       timestamptz,
    -- idempotency: a re-crawl of the same company re-discovers the same events → same (company_id, dedup_key) → ON
    -- CONFLICT merges media instead of inserting a duplicate. This constraint is the backbone of crash-safe discovery.
    unique (company_id, dedup_key)
);

-- enrichment-claim index (used by the OTHER fleet, not discovery) — partial so it stays small as rows reach enriched.
create index if not exists events_enrich_claimable_idx
    on events (status, next_retry_at)
    where status in ('discovered','rendering','failed');
