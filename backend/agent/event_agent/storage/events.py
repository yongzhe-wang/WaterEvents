"""event_agent.storage.events — the EVENT-level Postgres layer: batch-flush a scan's events idempotently, persist the
source pages (+content_hash for the incremental hash-gate), log per-scan resource usage, and run the stage-2 enrichment
claim/complete cycle.

用一句话讲完: scan_unit 跑完 → 把 events 用 `INSERT ... SELECT unnest(...) ON CONFLICT DO UPDATE(合并 media)` 批量幂等
落 events 表 → 把来源页 content(+sha256) 落 pages 表供下轮 hash-gate 比对 → scan_log 记一行资源消耗喂 solver;之后
media_agent 从同一张 events 表 claim `discovered` 事件做 enrichment。**这层只管"结果怎么安全地落、怎么被 claim",不含
任何 event 抽取逻辑**(那在 crawl/extract.py)—— 换 DB 不动 crawl。

WORK CLAIMING LIVES IN storage/queue.py, NOT HERE. The old company-level lease machinery (claim_company / renew_lease /
mark_company / fail_company / reconcile, which owned companies.status + companies.event_count) was deleted 2026-07-27:
the unified scheduler claims from `work_queue` and records per-unit results via queue.complete_work, so nothing had
called those functions since the scheduler landed. They left companies.event_count frozen at 162 while the events table
held 124,811 rows — a dead counter that silently contradicted reality and misled diagnosis.
{MEASURED 2026-07-27 "SUM(COMPANIES.EVENT_COUNT) = 162 VS COUNT(*) FROM EVENTS = 124811"}
{GREP 2026-07-27 "ONLY CALLER OF CLAIM_COMPANY/MARK_COMPANY WAS TESTS/EVENT/VERIFY/VERIFY_WORKER.PY, ITSELF BROKEN
 (`FROM . IMPORT DB` WITH NO DB.PY IN THAT PACKAGE)"}
[CONFIDENCE: CONFIRMED 100% — zero production callers; the frontend rail reads the event_companies VIEW, which is a real
 count(e.id) join, so removing the counter changes no user-visible number].

WHY asyncpg + Supavisor transaction pooler: 2000+ 公司 × N worker 会打爆 Postgres 直连;transaction-mode pooler 把
连接 multiplex 收敛。transaction mode 不支持 server-side prepared statements → 必须 statement_cache_size=0。
{RESEARCH wv2d0n0v3 "Supavisor transaction mode (6543) ... asyncpg 设 statement_cache_size=0"}
[CONFIDENCE: CONFIRMED 100% — prepared-statement caching is incompatible with a transaction-mode pooler that rotates
backends per transaction].
"""
from __future__ import annotations

import hashlib
import json
import os

import asyncpg

# _event_key is the ONLY key builder this module uses: (title+date) identity, falling back to the primary url's canon
# for a title-less event. The url-only `_dedup_key` that used to live here was deleted 2026-07-28 — it had zero callers
# and described a scheme the data has not used since the title+date key landed.
# {GIT GREP 2026-07-28 "_dedup_key → only its own def plus one stale migration comment; the live path is _event_key"}
from .urls import _event_key

# The Supavisor transaction-mode pooler DSN (port 6543), from env so no secret is hard-coded. The worker NEVER opens a
# session-mode direct connection at 2000-company scale. {RESEARCH "全部走 Supavisor transaction-mode pooler ... 防连接耗尽"}.
_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")

FLUSH_BATCH = int(os.environ.get("WATEREVENTS_FLUSH_BATCH", "25"))      # events per batch INSERT {DESIGN "events 攒 25 行"}
ENRICH_BATCH = int(os.environ.get("WATEREVENTS_ENRICH_BATCH", "16"))    # events an enrichment worker claims per round
# Soft lease held on a claimed EVENT while the enrichment worker renders its detail page; a crashed worker's rows become
# re-claimable once it lapses (see claim_events' `status='rendering' AND lease_until < now()` arm). Reads the same
# WATEREVENTS_LEASE_MIN env var (same default 30) that the deleted company-level lease used, so this is behaviour-
# preserving — only the NAME narrowed to the one path that still exists after the 2026-07-27 legacy removal.
# [CONFIDENCE: CONFIRMED 100% — same env key + same default; claim_events is the sole remaining reader].
ENRICH_LEASE_MIN = int(os.environ.get("WATEREVENTS_LEASE_MIN", "30"))

# SEC filings are EXCLUDED from this pipeline — not because they are low value, but because scraping them here is the
# wrong mechanism for them. EDGAR publishes the same documents through a complete, structured, bulk API
# (data.sec.gov submissions + full-text search), so a 10-K reached by rendering one IR page at a time is a fragile,
# partial copy of something that can be fetched systematically and in full. Whatever this pipeline extracts from a
# filings page is strictly worse than what the official route yields.
# {USER 2026-08-06 "we can sysmeticlaly process those url ther is no need for us to do it here"}
# [CONFIDENCE: CONFIRMED 100% — direct user directive.]
#
# The pattern is the SAME one the hub seeder already applies, kept as ONE constant so the two places cannot drift:
# {SEED.PY "AND source_url !~* '/(sec-filings|edgar|financials/(sec|quarterly|annual|financial-results)|regulatory)'"}
# {USER 2026-07-25 "big filling hub is def not the ones we should monitor"} — the earlier, narrower version of the
# same call, applied then to monitoring and now to enrichment as well.
# Scale of what this removes from stage-2's queue:
# {REST 2026-08-06 count=exact "events?source_url=ilike.*sec-filings*" -> 40530 of 282498} = 14.3% of all events,
# and {REST "event_documents?url=ilike.*sec-filings*" -> 1052 of 4853} = 21.7% of everything extracted so far.
# [CONFIDENCE: CONFIRMED 100% — both counts read from the live REST endpoint.]
SEC_URL_EXCLUDE = r"/(sec-filings|edgar|financials/(sec|quarterly|annual|financial-results)|regulatory)"


# WaterEvents lives in its OWN schema so it starts from scratch WITHOUT touching the decommissioned ir-pipeline's
# cluttered `public` (60+ tables incl. shared api_keys/api_jobs + dozens of *_arch_* snapshots). Old data stays
# archived-in-place; WaterEvents gets a pristine namespace. Default 'waterevents' to MATCH the migration, which does
# `create schema waterevents; set search_path=waterevents` — so a fresh `db push` (and a local throwaway Postgres that
# runs the same migration) both land the tables in waterevents, and the pool must read that same schema by default or it
# false-greens on an empty `public`. {AUDIT 2026-07-23 HIGH: default 'public' mismatched the waterevents-only migration}
# [CONFIDENCE: CONFIRMED 100% — schema in the migration and schema in the pool MUST be the one-and-same name].
_SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")


async def connect_pool(min_size: int = 1, max_size: int = 4) -> asyncpg.Pool:
    """Open the asyncpg pool against the Supavisor transaction pooler. statement_cache_size=0 is MANDATORY (transaction
    mode rotates the backend per tx, so a cached prepared statement points at the wrong session → 'prepared statement
    does not exist'). search_path pins every connection to WaterEvents' schema so unqualified table names resolve there."""
    if not _DSN:
        raise RuntimeError("WATEREVENTS_DB_DSN not set — point it at the Supabase Supavisor pooler (port 6543).")
    return await asyncpg.create_pool(_DSN, min_size=min_size, max_size=max_size, statement_cache_size=0,
                                     server_settings={"search_path": _SCHEMA})


async def flush_events(pool: asyncpg.Pool, company_id, run_id: str, events: list[dict]) -> int:
    """Idempotently persist a company's events in batches. Uses `INSERT ... SELECT unnest(...)` (one typed array per
    column — the 5x-faster wide-batch shape) with `ON CONFLICT (company_id, dedup_key) DO UPDATE` that MERGES media_urls
    (union, dedup) rather than DO NOTHING — so an event re-discovered from a second route (or a re-crawl) gains its extra
    media instead of being dropped. {RESEARCH "INSERT...SELECT unnest() ... 5.02x"; ADVERSARIAL "DO UPDATE SET MEDIA_URLS
    = ... || EXCLUDED.MEDIA_URLS 不丢 route 补的 media"}. Returns rows attempted."""
    written = 0
    for i in range(0, len(events), FLUSH_BATCH):
        batch = events[i:i + FLUSH_BATCH]
        keys, titles, dates, types, medias, srcs = [], [], [], [], [], []
        seen_in_batch = set()                                # ON CONFLICT can't catch dups WITHIN one INSERT → dedup here
        for e in batch:
            k = _event_key(e.get("title"), e.get("date"), e.get("urls") or [])   # (title+date) identity, url fallback for title-less
            if not k or k in seen_in_batch:                  # an event with no key shouldn't exist (extract drops url-less), skip defensively
                continue
            seen_in_batch.add(k)
            keys.append(k)
            titles.append(e.get("title") or "")
            dates.append(e.get("date") or "")
            types.append(e.get("type") or "")
            medias.append(json.dumps(e.get("urls") or []))   # jsonb array as text, cast below
            srcs.append(e.get("source_url") or "")           # the page this event was extracted from → events.source_url
        if not keys:
            continue
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (company_id, run_id, dedup_key, title, event_date, event_type, media_urls, source_url)
                SELECT $1, $2, k, t, d, ty, m::jsonb, s
                FROM unnest($3::text[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[]) AS x(k, t, d, ty, m, s)
                ON CONFLICT (company_id, dedup_key) DO UPDATE SET
                    media_urls = (
                        SELECT coalesce(jsonb_agg(DISTINCT u), '[]'::jsonb)      -- union existing + new, dedup
                        FROM jsonb_array_elements(events.media_urls || excluded.media_urls) AS u
                    ),
                    source_url = COALESCE(NULLIF(events.source_url, ''), NULLIF(excluded.source_url, ''));   -- keep first non-empty
                """,
                company_id, run_id, keys, titles, dates, types, medias, srcs,
            )
        written += len(keys)
    return written


async def save_pages(pool: asyncpg.Pool, company_id, run_id: str, pages: list[dict]) -> int:
    """Persist the SOURCE PAGES (url + reading-order content) each event was extracted from → the `pages` table the
    frontend's "Source page" view reads. WHY: events.source_url points AT a page, but the page CONTENT lives here; without
    it the UI shows "no source page stored". Idempotent per (company_id, url): re-crawl UPDATEs the content (freshest wins).
    {USER 2026-07-24 "no source page" — the schema had pages+source_url but nothing wrote them}. Returns rows written."""
    if not pages:
        return 0
    urls = [p.get("url") or "" for p in pages]
    contents = [p.get("content") or "" for p in pages]
    nchars = [len(c) for c in contents]
    # content_hash = sha256 of the SAME reading-order text the hash-gate hashes (scan._make_gate hashes page_text =
    # render.inline||text; save_pages stores content = the same string) → the stored hash and the gate's freshly-computed
    # hash are byte-identical, so an unchanged hub matches and its VLM call is skipped. {SCAN.PY _make_gate hashes page_text;
    # CRAWL.PY _page.content = render.inline||text — same source string} [CONFIDENCE: CONFIRMED — one string, one hash algo].
    hashes = [hashlib.sha256((c or "").encode("utf-8")).hexdigest() for c in contents]
    async with pool.acquire() as conn:                       # ON CONFLICT needs a unique index on (company_id, url) — see note
        await conn.execute(
            """
            INSERT INTO pages (company_id, run_id, url, content, n_chars, content_hash)
            SELECT $1, $2, u, c, n, h
            FROM unnest($3::text[], $4::text[], $5::int[], $6::text[]) AS x(u, c, n, h)
            WHERE u <> ''
            ON CONFLICT (company_id, url) DO UPDATE SET
                content = excluded.content, n_chars = excluded.n_chars, content_hash = excluded.content_hash;
            """,
            company_id, run_id, urls, contents, nchars, hashes,
        )
    return len(urls)


async def log_scan(pool: asyncpg.Pool, unit_type: str, url: str, stats: dict) -> None:
    """Append ONE finished scan's resource usage to scan_log → the windowed source for C_R (pages/hr), C_V (calls/hr) and
    hit_rate (vlm_calls / (vlm_calls+vlm_skipped)) that the packing solver reads to compute the incremental period T*.
    WHY an append row (not a work_queue last-value column): a THROUGHPUT is Σ over a time window ÷ window-hours; a
    last-value column keeps only the newest scan and can't be summed across a window. {USER 2026-07-26 "calculate render
    and vlm usage for parallel"; PLAN packing solver reads C_R/C_V/hit_rate live} [CONFIDENCE: CONFIRMED — rate needs history]."""
    async with pool.acquire() as conn:
        await conn.execute(
            # extract_errors is what lets a QUERY tell "this scan found nothing" apart from "this scan could not
            # look". Without it every consumer sees the same row for a healthy hash-gate skip and for a dead VLM, and
            # each invents its own guess — which is how three separate health checks all reported healthy through an
            # 11h52m total outage. {MIGRATION 20260729021500 fleet_health "events=0, extract_errors>0 → the system is
            # broken right now"} [CONFIDENCE: CONFIRMED 100% — the indistinguishability was the measured root cause].
            # failed_render carries the SAME argument one lane over. The engine counts it and worker.py's fail/complete
            # predicate consumes it (`lost = extract_errors + failed_render`), but it was dropped on the way here — so
            # a render failure reached the table as render_pages=1, vlm_calls=0, vlm_skipped=0, events=0,
            # extract_errors=0 and had to be deduced from three zeros. That is the exact shape all 40 permanently
            # 'failed' work_queue rows share, every one of them a url that has produced real events before.
            # {DB 2026-08-01 scan_log for the 40 failed units -> "1 | 0 | 0 | 0 | 0" on every row}
            # [CONFIDENCE: CONFIRMED 100% — read off production for all 40.]
            "INSERT INTO scan_log (unit_type, url, render_pages, vlm_calls, vlm_skipped, events, extract_errors, "
            "failed_render) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            unit_type, url, int(stats.get("render_pages") or 0), int(stats.get("vlm_calls") or 0),
            int(stats.get("vlm_skipped") or 0), int(stats.get("events") or 0),
            int(stats.get("extract_errors") or 0), int(stats.get("failed_render") or 0),
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENRICHMENT (stage-2, EVENT-level) — the media_agent worker claims `discovered` events from the SAME events table
# (the seam), drills each event's detail page, fills basic_info, flips to `enriched`. Event-level claim (not company)
# so a 10k-event company fans across many enrichment workers. Fencing via claim_token; fail-loud via fail_reason.
# {DESIGN wlkrnxklp "enrichment 是 EVENT 级 flat map 从表里 claim"}.
# ─────────────────────────────────────────────────────────────────────────────
async def claim_events(pool: asyncpg.Pool, limit: int = ENRICH_BATCH) -> list[asyncpg.Record]:
    """Claim a BATCH of enrichable events (discovered, OR partial-with-work-owed, OR rendering-lease-expired, OR
    failed-and-retry-due) → flip to `rendering` with a FRESH per-row claim_token + lease. SKIP LOCKED → N workers never
    fight over a row. Ordered by next_retry_at so backed-off failures sink below fresh rows (anti-starvation).

    `pending_kinds` rides along in the RETURNING because it is what tells the worker WHICH pass to run: empty means the
    full pass, non-empty means dispatch only those kinds and leave the html/VLM half alone. A claimed row is `rendering`
    either way — the array, not the status, carries the distinction, which is what lets one lease and one reclaim path
    serve both. {MIGRATION 20260810051500 "WHY ONE STATE + ONE ARRAY, AND NOT A PAIR OF STATES"}."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            UPDATE events SET status='rendering', claim_token=gen_random_uuid(),
                lease_until=now() + ($1 || ' minutes')::interval
            WHERE id IN (
                SELECT id FROM events
                WHERE (status='discovered'
                       -- 'partial' = enriched, but some lane was closed when it ran and still owes an attempt. It is
                       -- claimable on exactly the same terms as a fresh event; what differs is the work, not the
                       -- eligibility. {MIGRATION 20260810051500}
                       OR status='partial'
                       OR (status='rendering' AND lease_until < now())           -- reclaim a crashed enrichment worker
                       OR (status='failed' AND (next_retry_at IS NULL OR next_retry_at < now())))
                  -- SEC filings never enter stage-2: EDGAR serves them completely and structurally, so rendering an
                  -- IR filings page to reconstruct them is the wrong mechanism. Filtering at CLAIM rather than at
                  -- dispatch means the fleet never spends a render slot on one. See SEC_URL_EXCLUDE.
                  AND source_url !~* $3
                  -- AN EVENT WITH NO URL HAS NO ENRICHMENT WORK, so it must never occupy a render slot. This is the
                  -- claim-side half of the change that stopped writing the LISTING page in as an event's url: those
                  -- rows now carry `[]` honestly instead of `[<hub>]`, and that honesty only pays off if the claim
                  -- predicate reads it. Without this line the same rows get claimed, find nothing to do, and are
                  -- written back as failures — paying the full claim/lease/heartbeat cost to learn what the column
                  -- already said.
                  -- {psql 2026-08-07 "jsonb_array_length(media_urls)=1 AND media_urls->>0 = source_url → 6878"} is the
                  --  population that fallback created, each one costing a render that returned a navigation menu.
                  -- {USER 2026-08-07 "if there is no medai url in stage 2 then we just discad the event in stage 1"}
                  -- The ROW is kept: its title and date came off a text-only IR calendar and are real data; what it
                  -- lacks is anything for stage-2 to fetch. Dropping it from the QUEUE and dropping it from the
                  -- DATABASE are different decisions, and only the first is needed to stop the waste.
                  -- [CONFIDENCE: CONFIRMED 100% — count read from production; one such nav-menu body was read in full.]
                  AND jsonb_array_length(media_urls) > 0
                  -- AND the same rule for an event whose ONLY url IS the listing page. The crawler no longer records
                  -- one, but 110 such rows predate that change, and stopping their CREATION did nothing about their
                  -- CLAIMING: each time one is picked up, stage-2 renders the hub again and stores the navigation menu
                  -- again. An audit run after the first backfill found 167 fresh hub-sourced documents that had
                  -- appeared in exactly that way, still arriving:
                  -- {psql 2026-08-08 "d.url = e.source_url → 168 份, 其中 167 份产生于回填之后"}
                  -- {psql 2026-08-08 "stepstonegroupinc.gcs-web.com/events/event-details | 533 字"} — a listing page,
                  --  stored as an event's body.
                  -- Enforced HERE rather than by cleaning the 110 rows, because a data fix closes the population that
                  -- exists today while a predicate closes the shape forever — and the audit also found 2 such rows
                  -- created after the crawler fix, so the shape is demonstrably not closed.
                  -- [CONFIDENCE: CONFIRMED 100% — both counts and the sample document came from the live database.]
                  AND NOT (jsonb_array_length(media_urls) = 1 AND media_urls->>0 = source_url)
                -- enrich_priority first: a chosen batch (a test set, a customer's backlog) is pushed to the front
                -- without disturbing anything else. Every row defaults to 0, so with no batch enqueued this orders
                -- exactly as it did before. {MIGRATION 20260806025158 "ENRICH_PRIORITY"}
                --
                -- THEN queue_pos, which replaces a tiebreaker that was not breaking anything. Every `discovered` row
                -- has next_retry_at NULL, so all 168k of them tied at the front and the winner was whatever the
                -- planner returned — an unordered heap wearing an ORDER BY. Two measured failures came out of that
                -- single fact, in opposite directions:
                --   {DB 2026-08-10 — 216 rows returned to 'discovered' sat untouched across five samples ten minutes
                --    apart, while the fleet ran at full rate: a re-queued row is never reached}
                --   {DB 2026-08-10 — the escape hatch, enrich_priority=8 on 313 rows, put 39 in-flight events on ONE
                --    host and took throughput 884 → 144 docs/h: the only tool for un-starving manufactures clumping}
                -- queue_pos deals the queue by host — round k holds one row per host with at least k rows — so the
                -- head is 3,107 distinct hosts and the biggest host's density there is 1/3107. See migration
                -- 20260810062000 and pacer.py::_respace_events for the arithmetic.
                -- [CONFIDENCE: CONFIRMED 100% — both incidents measured on this database hours apart.]
                --
                -- `next_retry_at IS NOT NULL` is kept AHEAD of queue_pos to preserve exactly what NULLS FIRST bought:
                -- a never-failed row still outranks a backed-off retry. Dropping it would let a retrying row with a
                -- low position outrank fresh work, which is the anti-starvation property inverted.
                -- NULLS LAST on queue_pos: a row the pacer has not positioned yet sinks below every positioned row
                -- rather than jumping the queue, so a pacer outage delays new work instead of flooding the head.
                ORDER BY enrich_priority DESC, (next_retry_at IS NOT NULL), queue_pos ASC NULLS LAST
                FOR UPDATE SKIP LOCKED LIMIT $2
            )
            RETURNING id, claim_token, title, event_date, event_type, media_urls, pending_kinds;
            """,
            str(ENRICH_LEASE_MIN), limit, SEC_URL_EXCLUDE,
        )


async def renew_leases(pool: asyncpg.Pool, pairs: list[tuple]) -> set:
    """Renew a WHOLE BATCH of claims in ONE statement on ONE connection. Returns the set of event_ids still owned;
    anything the caller passed in that is missing from the result has provably lost its claim.

    WHY batched instead of one call per event: the per-event version of this deadlocked itself against the connection
    pool. A worker claims ENRICH_BATCH=16 events and runs them concurrently, so 16 heartbeats woke together and each
    asked for its own connection out of a pool built as {EVENTS.PY:65 "ASYNC DEF CONNECT_POOL(MIN_SIZE: INT = 1, MAX_SIZE: INT = 4)"}.
    Four succeeded, twelve timed out, and the log shows it with the pool size printed straight through as the group size:
    {MEDIA@2 2026-08-05 "08:42:58 ⚠️ EVENT 27EEDDDD LEASE RENEWAL ERRORED (TIMEOUTERROR: )" — FOUR SUCH LINES PER TICK,
     AT 08:39:58 / 08:42:58 / 08:43:58 / 08:44:58, MATCHING MAX_SIZE=4 EXACTLY}
    The renewals that timed out never reached the database, the leases lapsed, and the reaper reclaimed the rows out
    from under workers that were still processing them — the same discard this heartbeat exists to prevent, reintroduced
    by the heartbeat's own connection appetite. Four of six workers spent 40 minutes in that state and committed nothing.
    [CONFIDENCE: CONFIRMED — the four-per-tick grouping IS the pool size; media@2/@3/@5 logged 104-108 lines and zero ✅
     over the window while media@1/@4/@6 committed 13/14/11.]

    One query for the batch means one connection per WORKER rather than one per EVENT, so the cost no longer scales
    with ENRICH_BATCH and cannot exhaust the pool no matter how large a batch grows.
    """
    if not pairs:
        return set()
    ids = [p[0] for p in pairs]
    toks = [p[1] for p in pairs]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            # Match on the (id, claim_token) PAIR, not on id alone — an event reclaimed by the reaper carries a new
            # token, so pairing is what keeps a stale worker from renewing a lease it no longer owns.
            """
            UPDATE events SET lease_until = now() + ($1 || ' minutes')::interval
            WHERE (id, claim_token) IN (SELECT * FROM unnest($2::uuid[], $3::uuid[]))
              AND status='rendering'
            RETURNING id;
            """,
            str(ENRICH_LEASE_MIN), ids, toks,
        )
        return {r["id"] for r in rows}


async def mark_enriched(pool: asyncpg.Pool, event_id, claim_token, basic_info: str, urls: list[str]) -> bool:
    """Flip an event to `enriched` with its generative basic_info + merged urls. FENCED on claim_token: if the lease
    expired and another worker re-claimed (new token), this UPDATE matches nothing → the stale result never clobbers.
    media_urls is MERGED (union, dedup) NOT replaced — a discovery re-crawl may have ON-CONFLICT-added urls AFTER this
    worker read the event; a plain `=$4::jsonb` would overwrite + LOSE them. {AUDIT 2026-07-23 HIGH: mark_enriched
    replaced instead of merged}. Returns True if it landed (we still owned it)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE events SET status='enriched', enriched_at=now(), claim_token=NULL, basic_info=$3,
                media_urls = (
                    SELECT coalesce(jsonb_agg(DISTINCT u), '[]'::jsonb)          -- union current(may have grown) + new, dedup
                    FROM jsonb_array_elements(events.media_urls || $4::jsonb) AS u
                )
            WHERE id=$1 AND claim_token=$2 RETURNING id;
            """,
            event_id, claim_token, basic_info, json.dumps(urls or []),
        )
        return row is not None


async def delete_event(pool: asyncpg.Pool, event_id, claim_token) -> bool:
    """Delete an event that turned out NOT to be an event. Returns True if the row was ours and is gone.

    WHY delete rather than park it in a status: the row is not a failed event, it is a MISTAKE — stage-1 read a listing
    page and minted an event from it. Keeping it would leave a permanent phantom in every count, and every later pass
    would re-examine it to reach the same conclusion. The url itself is not wasted: the caller promotes it to the
    incremental hub queue first, so the page keeps being watched — as the listing it always was.
    {USER 2026-08-06 "we should just add it to the incermetnal hubs after dedup and delete the event here"}
    [CONFIDENCE: CONFIRMED 100% — direct user directive.]

    The delete is COMPLETE, not partial: all six child tables cascade, so no artifact of the phantom survives.
    {psql 2026-08-06 — event_documents / event_media_urls / event_transcript_segments / event_audio and the two arch
     tables all report "CASCADE 级联删" on events; none is RESTRICT or NO ACTION}
    [CONFIDENCE: CONFIRMED 100% — read from pg_constraint on the production database before this was written.]

    Fenced on claim_token like every other completion path — a worker that lost its lease must not delete a row the new
    owner is working on.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow("DELETE FROM events WHERE id=$1 AND claim_token=$2 RETURNING id;",
                                  event_id, claim_token)
        return row is not None


async def defer_event(pool: asyncpg.Pool, event_id, claim_token, reason: str) -> None:
    """Park an event we CHOSE not to process. Not a failure: fail_count is untouched and no retry is scheduled.

    WHY this is not fail_event with a nicer reason string: fail_event bumps fail_count and dead-letters on the third
    strike {EVENTS.PY "STATUS = CASE WHEN FAIL_COUNT + 1 >= 3 THEN 'DEAD_LETTER' ELSE 'FAILED' END"}, and dead_letter
    is absent from claim_events' predicate — so an event deferred three times would be stranded, invisible to the very
    requeue that is supposed to rescue it once its lane is enabled. Forty rows landed in that state within 45 seconds
    of the kind gate going live {psql 2026-08-06 "FAILED | DEFERRED:KIND-DISABLED | 40"} before this existed.
    [CONFIDENCE: CONFIRMED 100% — the dead_letter branch and the claim predicate are both in this file.]

    `deferred` is deliberately NOT claimable. Parking these back in `discovered` would not work: the claim predicate
    checks next_retry_at only for `failed`, so a `discovered` row is claimed unconditionally and would loop instantly.
    Re-enabling a lane is one UPDATE flipping `deferred` back to `discovered`.

    Fenced on claim_token, like every other completion path — a worker that lost its lease must not park a row the
    new owner is already working.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE events SET status='deferred', fail_reason=$3, claim_token=NULL,
                              lease_until=NULL, next_retry_at=NULL
            WHERE id=$1 AND claim_token=$2;
            """,
            event_id, claim_token, (reason or "")[:200],
        )


async def clear_pending_kinds(pool: asyncpg.Pool, event_id, claim_token) -> bool:
    """Owed work on a lane this event has no url for → drop the debt and put the row back to 'enriched'. Fenced.

    WHY this exists as its own exit. Enrolment into `partial` is a bulk UPDATE driven by the url ledger, and a ledger
    row can name a kind the event's CURRENT media_urls no longer contains — a url was rewritten by the viewer unwrap,
    or the row predates a router reclassification. Without this exit the worker claims such a row, filters `media` to
    empty, and returns having changed nothing, so the row is claimed again on the very next round: a free-running loop
    that occupies a worker slot forever and produces nothing. It is the same shape as an empty retry with no backoff.
    Dropping the debt rather than failing the event is the honest disposition — the event is enriched and fine; there
    is simply nothing on that lane to fetch. {MIGRATION 20260810051500}"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE events SET status='enriched', pending_kinds='{}', claim_token=NULL, lease_until=NULL
               WHERE id=$1 AND claim_token=$2 RETURNING id;""",
            event_id, claim_token,
        )
    return row is not None


async def fail_event(pool: asyncpg.Pool, event_id, claim_token, reason: str) -> None:
    """Enrichment FAILED on this event → fail-loud: record fail_reason, bump fail_count, and either requeue with
    exponential backoff+jitter (fail_count<3) or send to dead_letter (≥3 — a persistently-failing event is human-review,
    never silently 'enriched'). Content errors (schema_invalid / output_truncated / unrenderable) SHOULD pass a terminal
    reason so the caller can dead_letter immediately; here we let the count decide. Fenced on claim_token."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE events SET
                status = CASE WHEN fail_count + 1 >= 3 THEN 'dead_letter' ELSE 'failed' END,
                fail_count = fail_count + 1, fail_reason = $3,
                next_retry_at = now() + (interval '30 seconds' * power(2, fail_count)) + (random() * interval '10 seconds'),
                claim_token = NULL
            WHERE id=$1 AND claim_token=$2;
            """,
            event_id, claim_token, (reason or "")[:200],
        )


async def reconcile_events(pool: asyncpg.Pool) -> int:
    """Sweeper: reclaim events stuck in `rendering` past their lease (crashed enrichment worker) → back to `discovered`
    so the next claim retries. Returns rows reclaimed. {ADVERSARIAL "兜底扫非终态过期 lease 行"}."""
    async with pool.acquire() as conn:
        tag = await conn.execute(
            "UPDATE events SET status='discovered', claim_token=NULL "
            "WHERE status='rendering' AND lease_until < now();"
        )
        try:                                             # a malformed command tag must return 0, never crash the cron
            return int(tag.split()[-1]) if tag and tag.startswith("UPDATE") else 0
        except (ValueError, IndexError):                 # {AUDIT 2026-07-23 MEDIUM: int(tag.split()) could raise}
            return 0
