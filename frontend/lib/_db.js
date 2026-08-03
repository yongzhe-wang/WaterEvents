// Shared Supabase REST helpers for the Vercel serverless functions.
// WHY: the dashboard now reads LIVE from Supabase (project water_events_ir) instead of
// a static snapshot, so the deployed site is dynamic for anyone. The publishable
// key is public by design (RLS is off → read-only public access to these tables).
// {USER 2026-06-06 "i want this dynamic, and working for vercel and other people"}
// Files prefixed with "_" are NOT treated as routes by Vercel — just a shared lib.
// From env (publishable key is public-by-design; the literal is only the local-dev default) so a project/key
// change is a config edit, not a code edit. {AUDIT 2026-06-22 "no hardcode"}.
// Defaults point to the LIVE project (vtwdantvlurtvymhhorr) post-migration; prod overrides via env.
// {MIGRATION 2026-06-24 duioztbhvufoikracwgf → ezuvmolyfgsadkehjnef} — anon key is public-by-design.
// {MIGRATION 2026-08-03 ezuvmolyfgsadkehjnef (personal project, us-east-1) → vtwdantvlurtvymhhorr
//  (water_events_ir under the focusAlpha org, us-west-2)} — moving the DB off a personal project onto the company org.
// [CONFIDENCE: CONFIRMED 100% — pg_dump/pg_restore reconciled 14/15 tables exactly (scan_log drifted during the
//  cutover window), indexes 34/34 and constraints 115/115 matched, and the old project went to zero writes].
const SUPABASE_URL = process.env.SUPABASE_REST_URL || "https://vtwdantvlurtvymhhorr.supabase.co/rest/v1";
const SUPABASE_KEY = process.env.SUPABASE_PUBLISHABLE_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ0d2RhbnR2bHVydHZ5bWhob3JyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU3MzMzOTMsImV4cCI6MjEwMTMwOTM5M30.-ls5eNkxKHJSE_YBtfV_Zhi-qT2QKDPbYMHkhJ3JUOU";

// Accept-Profile pins PostgREST to the `waterevents` schema (discovery + enrichment tables live there, NOT public —
// a bare read would hit public / a stale ir-pipeline table). {MIGRATION 20260723 "create schema waterevents"}.
const HEADERS = { apikey: SUPABASE_KEY, Authorization: `Bearer ${SUPABASE_KEY}`, "Accept-Profile": "waterevents" };

// sbPublic() — kept as a thin alias of sb(). WHY it exists at all: pipeline_status / pipeline_health USED to live only in
// the legacy `public` schema, so reading them under the waterevents-pinned HEADERS 404'd (PGRST205 "could not find the
// table 'waterevents.pipeline_status'") → health panel + tracked-ticker scope silently empty in prod. Both tables have now
// been MOVED into `waterevents` and the whole legacy `public` schema was dropped, so one profile serves everything; the
// alias stays so callers don't churn. {SCAN whn86f4f7 E1 + 2026-07-26 legacy-public drop}
// [CONFIDENCE: CONFIRMED — 404 reproduced by live curl pre-move; tables verified present in waterevents post-move].
export async function sbPublic(path) {
  return sb(path);                                             // single canonical schema now — waterevents
}

// getTrackedTickers() — the media-data scope SHARED by the two company_agent surfaces: the Company Agent
// Table rail (/api/companies `tracked` flag → EventsView) AND the Company Agent progress bars (/api/status
// coverage). Both surfaces MUST show the SAME companies, so both await THIS one function — a single source of
// truth by construction. A company is "tracked" iff it has media DATA: ≥1 company_agent-written basic_info
// (company_event_stats.basic_count>0) OR ≥1 media artifact (company_artifact_stats.artifact_count>0).
// WHY dynamic — replaces the old HARDCODED curated-19 list: the user deliberately expanded media past the
// original 19 to ~97 companies and wants BOTH surfaces to show every company that actually has media data. So
// the scope now DERIVES from the DB and auto-grows as media expands — no more hand-editing a ticker list when
// a company gets its first artifact. An explicit env override (TRACKED_TICKERS="AAPL,MSFT,...") still wins,
// for pinning a specific set. {USER 2026-07-01 "the fix should be adding these [~97] to tracked, and also make
// sure they show the progress bar"; picked the "~97 has-media-data" scope over 114-attempted / 194-crawled}
// {USER 2026-06-30 "media include basic info as long as have 1 basic info"}
// [CONFIDENCE: CONFIRMED 100% — user explicitly chose the ~97 has-media-data option via this session's prompt].
export async function getTrackedTickers() {
  // Explicit pin still wins — a comma-separated env list overrides the dynamic default (escape hatch).
  const override = (process.env.TRACKED_TICKERS || "").trim();
  if (override) return new Set(override.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean));
  // DYNAMIC default (2026-07-18): the CURRENT RUN's companies = every ticker in the pipeline_status table. The
  // pipeline orchestrator/workers upsert a ticker here when its first stage starts, so the web app shows ONLY the
  // companies in-flight/done for THIS run — an EMPTY pipeline_status table → NO companies (the fresh-start clean slate
  // the user asked for). Supersedes the old "has media data" (company_event_stats/artifact_stats) scope that showed
  // the whole stale 379k-event backlog. {USER 2026-07-18 "clear the web app showing no companies ... mark each ticker
  // if finished so we dont rerun"} [CONFIDENCE: CONFIRMED — pipeline_status is the single run-tracker].
  const rows = await sbPublic("pipeline_status?select=ticker");   // public schema (legacy monitoring table, ~100 rows < 1000 cap)
  const set = new Set();
  for (const r of rows) if (r.ticker) set.add(String(r.ticker).toUpperCase());
  return set;
}

// GET rows from a PostgREST path (e.g. "events?ticker=eq.NVDA&select=*&order=id.desc").
export async function sb(path) {
  const r = await fetch(`${SUPABASE_URL}/${path}`, { headers: HEADERS });
  if (!r.ok) {
    // Log the full PostgREST body server-side (table/column/constraint detail helps us debug) but NEVER
    // put it in the thrown Error — that message can be serialized into the client response.
    // {nestjs-conventions "never throw raw Supabase/DB errors — they leak internal schema details"}.
    console.error(`supabase ${r.status} on ${path}: ${await r.text()}`);
    throw new Error(`supabase request failed (${r.status})`);
  }
  return r.json();
}

// Fetch ALL rows for a PostgREST path, paging past the server's 1000-row response cap.
// WHY: PostgREST returns at most 1000 rows per request, so any endpoint that AGGREGATES a table
// (e.g. token_usage now has 6000+ rows) was silently summing only the first 1000 → frozen totals.
// We page with the Range header (0-999, 1000-1999, …) until a short page signals the end.
// {USER 2026-06-08 "this page need update too" — token-usage totals were stuck at 1000}
export async function sbAll(path) {
  const PAGE = 1000;
  let from = 0;
  const all = [];
  for (;;) {
    const r = await fetch(`${SUPABASE_URL}/${path}`, {
      headers: { ...HEADERS, "Range-Unit": "items", Range: `${from}-${from + PAGE - 1}` },
    });
    if (!r.ok) {
    // Log the full PostgREST body server-side (table/column/constraint detail helps us debug) but NEVER
    // put it in the thrown Error — that message can be serialized into the client response.
    // {nestjs-conventions "never throw raw Supabase/DB errors — they leak internal schema details"}.
    console.error(`supabase ${r.status} on ${path}: ${await r.text()}`);
    throw new Error(`supabase request failed (${r.status})`);
  }
    const rows = await r.json();
    all.push(...rows);
    if (rows.length < PAGE) break;   // short page = last page
    from += PAGE;
  }
  return all;
}
