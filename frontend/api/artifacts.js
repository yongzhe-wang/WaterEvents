// GET /api/artifacts?event_ids=<uuid>,<uuid>,...
// Batch INVENTORY endpoint: for each event id, returns a summary of every stage-2 artifact type
// so the dashboard can decide WHICH artifact buttons to render per row — WITHOUT transferring any
// heavy payload (md/markdown/text columns). Called every 30s alongside /api/today.
// Contract: { "inventory": { "<event_id>": { blocks, files, segments, audio, urls, basic_info } } }
// Events with zero artifacts may be OMITTED (client treats missing-key as empty).
// {USER 2026-08-05 "debug infra — buttons for pdf, md, basic info, pptx, audio etc"}
// [CONFIDENCE: CONFIRMED 100% — user screenshot + task brief in this session]
import { sb } from "../lib/_db.js";

// UUID v4 regex — used to strip non-uuid entries from the id list before building PostgREST paths.
// WHY: the id list comes from the client and is interpolated into a REST url path; an injected '),
// or empty string would break the PostgREST "in.(…)" filter and potentially leak data.
// {OWASP "A03:2021 – Injection"; PostgREST docs "in.(v1,v2,...)" syntax}.
// [CONFIDENCE: CONFIRMED 95% — defensive measure; PostgREST would 400 on malformed but we validate
//  earlier to avoid giving any response to injected input].
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// MAX_IDS caps the batch so the PostgREST url stays under ~8 KB (nginx/Vercel default header+url
// limit is 8–16 KB; 200 × 37-char uuids = 7 400 chars + filter prefix ≈ safe at chunk size 100).
// For the /api/today feed which tops out at 200 rows this cap is never hit in practice.
// [CONFIDENCE: INFERRED 85% — no measured Vercel limit found; 8 KB is the widely-cited nginx default].
const MAX_IDS = 200;

// CHUNK_SIZE: PostgREST "in.(…)" with 200 uuids ≈ 7 400 chars in the path segment; adding select +
// order params brings the full url near 8 KB. Chunk at 100 so each sub-request sits under 4 KB.
// WHY chunk at all: URL length is a hard ceiling; chunking also parallelises sub-queries across
// ≤2 batches so total latency equals the slowest round-trip, not their sum.
// {MEASURED media.js 2026-07-28 "SBALL(EVENTS ... NO LIMIT) -> ROWS=146758 IN 130.5S"} proves
// that over-fetching rows is the real danger — we avoid it by projecting ONLY columns needed to count.
// [CONFIDENCE: INFERRED 80% — no single measured incident for URL ceiling; conservative margin].
const CHUNK_SIZE = 100;

// buildInFilter — join uuids into PostgREST "in.(a,b,c)" syntax.
// WHY encodeURIComponent not needed here: uuids are safe ASCII hex + hyphens; the parentheses and
// commas in "in.(...)" are part of PostgREST's own query syntax and must NOT be percent-encoded
// (PostgREST parses them before URL decoding). Test confirmed: curl with raw "in.(uuid,uuid)"
// returns 200, while "in.%28uuid%2Cuuid%29" returns 400 PGRST100.
// {PostgREST docs v12 "Filtering > in" — value format is "in.(v1,v2,...)"}
// [CONFIDENCE: CONFIRMED 90% — pattern used in existing codebase filter calls; PostgREST behaviour
//  verified by the smoke-test curl runs in this session].
function buildInFilter(ids) {
  return `in.(${ids.join(",")})`;
}

// chunkArray — split an array into sub-arrays of at most `size` elements.
// WHY: used to stay under URL length ceiling per request (see CHUNK_SIZE comment above).
// [CONFIDENCE: CONFIRMED 100% — pure utility, no external dependency].
function chunkArray(arr, size) {
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

// fetchForIds — run sb(pathFn(chunk)) for each chunk and flatten results.
// WHY a helper: all 6 table queries use the exact same chunk-and-flatten pattern; extracting it
// avoids 6 copies of the loop. {nestjs-conventions "Promise.all with .map() for independent async"}.
// [CONFIDENCE: CONFIRMED 100% — DRY refactor, no external contract].
async function fetchForIds(ids, pathFn) {
  const chunks = chunkArray(ids, CHUNK_SIZE);
  // Run all chunks in parallel — for 200 ids that is at most 2 concurrent requests per table,
  // within PostgREST connection limits.
  const results = await Promise.all(chunks.map((chunk) => sb(pathFn(chunk))));
  return results.flat();
}

export default async function handler(req, res) {
  // Parse and validate the event_ids list — reject non-uuid entries rather than interpolating them.
  const raw = ((req.query && req.query.event_ids) || "").trim();
  if (!raw) { res.status(400).json({ error: "event_ids required" }); return; }

  // Split on comma, filter blanks, validate uuid format, cap at MAX_IDS.
  const ids = raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => UUID_RE.test(s))   // drop anything that is not a uuid — injection guard
    .slice(0, MAX_IDS);

  if (ids.length === 0) { res.status(400).json({ error: "no valid event_ids" }); return; }

  try {
    // ── PARALLEL fetch across all 6 artifact tables ──────────────────────────────────────────
    // All 6 queries fire simultaneously (outer Promise.all); within each query, chunks also fire in
    // parallel (inner Promise.all in fetchForIds). This gives maximum concurrency while each
    // sub-request stays well under the URL length ceiling.
    //
    // COLUMN PROJECTION DISCIPLINE: select ONLY the columns needed to count + group.
    // NEVER select md / markdown / text — the heavy columns.
    // {MEASURED media.js 2026-07-28 "SBALL(EVENTS ... NO LIMIT) -> ROWS=146758 IN 130.5S"}
    // proves that over-fetching is the real danger. Our projections:
    //   blocks:    event_id only           → ~37 bytes/row
    //   files:     event_id, kind, n_pages → ~55 bytes/row  (n_pages is a small int, 96 rows total)
    //   segments:  event_id only           → ~37 bytes/row
    //   audio:     event_id only           → ~37 bytes/row
    //   urls:      event_id, status        → ~50 bytes/row  (688 rows total)
    //   basic:     events.id only          → ~37 bytes/row  (presence signal, not the content)
    // [CONFIDENCE: CONFIRMED — projection list derived from schema ground-truth in task spec].

    // ── ONE row per event, aggregated IN POSTGRES ──────────────────────────────────────────────
    // The counts come from the waterevents.event_artifact_counts VIEW rather than from counting
    // transferred rows. This is a correctness fix, not an optimisation: the previous shape asked for
    // the raw rows under `limit=500`, and that limit caps the RESPONSE, not the per-event count. A
    // single event in production already carries 15,923 content blocks —
    // {MEASURED 2026-08-05 psql "SELECT * FROM WATEREVENTS.EVENT_ARTIFACT_COUNTS ORDER BY BLOCKS DESC
    //  LIMIT 4" -> "1A4F990A-8D72-47E9-B77C-2EAC2F988E15 | 15923 | 0 | | 0 | 0 | 5 | 1 | 3 | 1"}
    // — so that one event alone overruns a 500-row window by 32x, and every event after it in the
    // same chunk would have reported zero artifacts and rendered no buttons at all. Silently.
    // [CONFIDENCE: CONFIRMED 100% — the 15,923 figure is a live read of the view on production.]
    //
    // The view returns AT MOST one row per event, so a CHUNK_SIZE=100 request returns <=100 rows —
    // an order of magnitude under PostgREST's 1000-row response cap. The bound now depends on how
    // many events were ASKED about (which this handler controls via MAX_IDS) instead of on how much
    // output those events happened to produce (which nothing controls). No `limit` clause is needed
    // because the shape itself is what makes truncation impossible.
    const [countRows, basicRows] = await Promise.all([
      // Every count + the per-kind file breakdown in one projection. file_kinds is jsonb of the form
      // {"pdf": 2, "xlsx": 1}, built by the view so the client never regroups rows itself.
      fetchForIds(ids, (c) =>
        `event_artifact_counts?select=event_id,blocks,files,file_kinds,segments,audio,`
        + `urls_done,urls_failed,urls_skipped&event_id=${buildInFilter(c)}`),

      // events.basic_info presence: filter WHERE basic_info IS NOT NULL, select ONLY the id column.
      // WHY NOT select basic_info itself: at 200 events x avg ~2 KB = 400 KB every 30s = ~800 MB/day
      // of transfer for a column used as a boolean.
      // This query was ALREADY safe under a row cap and stays as it was: the filter returns at most
      // one row per event asked about, so a 100-id chunk can never exceed 100 rows.
      // [CONFIDENCE: CONFIRMED 100% — one row per matching event id, by primary key.]
      fetchForIds(ids, (c) => `events?select=id&id=${buildInFilter(c)}&basic_info=not.is.null`),
    ]);

    // ── Tally results into a map keyed by event_id ───────────────────────────────────────────
    const inventory = {};

    // Helper: get-or-create a blank entry for an event.
    // WHY: both loops below write into the same entry; this centralises the zero-state so neither
    // loop needs to guard against a missing key. [CONFIDENCE: CONFIRMED 100% — pure utility].
    function entry(eid) {
      if (!inventory[eid]) {
        inventory[eid] = {
          blocks: 0,
          files: [],            // [{kind, n_pages}] — one entry per media file, per the contract
          segments: 0,
          audio: 0,
          urls: { done: 0, failed: 0, skipped: 0 },
          basic_info: false,
        };
      }
      return inventory[eid];
    }

    for (const r of countRows) {
      const e = entry(r.event_id);
      e.blocks   = r.blocks   ?? 0;
      e.segments = r.segments ?? 0;
      e.audio    = r.audio    ?? 0;
      e.urls = {
        done:    r.urls_done    ?? 0,
        failed:  r.urls_failed  ?? 0,
        skipped: r.urls_skipped ?? 0,
      };
      // Re-expand {"pdf": 2} into two {kind:"pdf"} entries so the payload keeps the array shape the
      // contract specifies and the button renderer's accumulate-by-kind loop works unchanged.
      // n_pages is null here by construction: the view aggregates across a kind, so a per-FILE page
      // count no longer exists at this level. Nothing in the button row reads it — it is carried only
      // to keep the documented field present rather than silently dropped.
      for (const [kind, n] of Object.entries(r.file_kinds || {})) {
        for (let i = 0; i < (n || 0); i++) e.files.push({ kind, n_pages: null });
      }
    }

    // Mark basic_info presence — basicRows contains ONLY events where basic_info IS NOT NULL.
    // The events table PK is "id" (not "event_id"), so we read r.id here.
    for (const r of basicRows) {
      // Create or update the entry — the event may have basic_info but zero other artifacts.
      entry(r.id).basic_info = true;
    }

    res.json({ inventory });
  } catch (_e) {
    // Never leak the raw PostgREST error to the client.
    // {nestjs-conventions "never throw raw Supabase/DB errors — they leak internal schema details"}.
    res.status(500).json({ error: "failed to load artifacts inventory" });
  }
}
