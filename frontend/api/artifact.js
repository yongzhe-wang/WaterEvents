// GET /api/artifact?event_id=<uuid>&kind=<kind>
// Single-artifact CONTENT for the debug modal. Returns the full rows for one artifact type of one
// event, capped at 500 rows so one pathological event cannot stall the UI.
// kind is one of: blocks | files | transcript | audio | urls | basic
// {USER 2026-08-05 "debug infra — pop up of e.g. the pdf, md, basic info, pptx, audio"}
// [CONFIDENCE: CONFIRMED 100% — explicit contract in task spec this session]
import { sb } from "../lib/_db.js";

// UUID v4 regex — same guard as artifacts.js; event_id comes from the client and is interpolated
// into a PostgREST path, so we must validate it before use.
// {OWASP "A03:2021 – Injection"} [CONFIDENCE: CONFIRMED 95% — defensive, PostgREST would 400 but
//  we validate earlier to avoid handing any parsed response to unvalidated input].
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// ROW_CAP: maximum rows returned per modal fetch. WHY it is far less pressing than it was: event_documents holds
// ONE row per source url, and an event has a handful of urls — where the old event_content_blocks held one row per
// paragraph and a single event reached 15,923 of them {psql 2026-08-05 "MD | 15786 | 47"}. The cap stays as a backstop
// against a pathological event, not as a routine limit.
// (historical note: the largest table was event_content_blocks)
// has 23 121 total rows but is filtered by event_id — a single event with >500 blocks would be
// pathological. The cap prevents a single event from stalling the modal with a huge payload while
// still covering every realistic case. {SCHEMA ground-truth in task spec: event_content_blocks 23121 rows}.
// [CONFIDENCE: INFERRED 90% — no single-event row count measured; 500 is a conservative safe cap].
const ROW_CAP = 500;

// VALID_KINDS: the exhaustive set of kind values the client may request.
// WHY an allowlist (not just switch-case): an unknown kind could be a typo or a client bug; returning
// 400 early gives the client a clear signal rather than returning an empty 200 that looks like "no data".
// {Task spec "Unknown kind -> 400 {error:'unknown kind'}"}.
// [CONFIDENCE: CONFIRMED 100% — explicit contract requirement].
const VALID_KINDS = new Set(["blocks", "files", "transcript", "audio", "urls", "basic"]);

export default async function handler(req, res) {
  // Validate event_id — required, must be a valid uuid.
  const id = ((req.query && req.query.event_id) || "").trim();
  if (!id) { res.status(400).json({ error: "event_id required" }); return; }
  if (!UUID_RE.test(id)) { res.status(400).json({ error: "invalid event_id" }); return; }

  // Validate kind — must be one of the allowlisted values.
  const kind = ((req.query && req.query.kind) || "").trim().toLowerCase();
  if (!VALID_KINDS.has(kind)) { res.status(400).json({ error: "unknown kind" }); return; }

  try {
    let rows;

    if (kind === "blocks" || kind === "files") {
      // BOTH kinds now come from event_documents — the table that replaced the per-block rows and the per-file rows
      // with ONE (md, blocks) pair per source url. 'blocks' means the html pages, 'files' the office documents; that
      // is the only difference, and it is a filter on `kind`, not a different table.
      // {MIGRATION 20260805151246 "EVENT_DOCUMENTS — ONE ROW PER (EVENT, SOURCE URL), HOLDING A PAIR: THE PROSE AS
      //  MARKDOWN, AND THE STRUCTURES THE MARKDOWN'S PLACEHOLDERS POINT AT"}
      // [CONFIDENCE: CONFIRMED 100% — schema read back from psql after the migration applied.]
      //
      // The kind NAMES are kept as-is on the wire even though "blocks" no longer means a content block. Renaming them
      // would break the buttons the dashboard already renders for no gain — the label the user sees is decided in the
      // UI, not here.
      const filter = kind === "blocks" ? "kind=eq.html" : "kind=neq.html";
      // md and blocks are BOTH selected: they are a pair and the modal renders them interleaved, replacing each
      // [[TABLE:n]] placeholder with the matching structured table. Fetching one without the other would render a
      // document full of visible markers.
      rows = await sb(
        `event_documents?select=url,kind,md,blocks,n_pages,n_chars,n_blocks&${filter}` +
        `&event_id=eq.${encodeURIComponent(id)}&order=created_at.asc&limit=${ROW_CAP}`);

    } else if (kind === "transcript") {
      // event_transcript_segments — whisper output, ordered by ord (reading / time order).
      // start_s and end_s may be NULL (verified: ground-truth event 90fe56a7 has start_s NULL).
      // WHY include start_s: the modal renders "[mm:ss] <speaker>: <text>" — NULL handled client-side.
      // {SCHEMA: event_transcript_segments(id,event_id,ord,speaker,start_s,end_s,text,source_url,seg_hash,created_at)}
      // [CONFIDENCE: CONFIRMED — schema from psql ground-truth in task spec; NULL start_s confirmed by
      //  live data on event 90fe56a7-25e0-4f8c-a60b-5d9bc3ec108e].
      rows = await sb(
        `event_transcript_segments?select=ord,speaker,start_s,end_s,text,source_url` +
        `&event_id=eq.${encodeURIComponent(id)}&order=ord.asc&limit=${ROW_CAP}`
      );

    } else if (kind === "audio") {
      // event_audio — 0 rows currently but schema exists; select all displayable fields.
      // {SCHEMA: event_audio(id,event_id,url,local_path,duration_s,created_at)}
      // [CONFIDENCE: CONFIRMED — schema from psql ground-truth in task spec].
      rows = await sb(
        `event_audio?select=url,local_path,duration_s,created_at` +
        `&event_id=eq.${encodeURIComponent(id)}&limit=${ROW_CAP}`
      );

    } else if (kind === "urls") {
      // event_media_urls — the per-url processing ledger, most valuable debug artifact because it
      // is the ONLY place per-url failures are visible. Order by created_at asc (chronological).
      // WHY include canon_key: it deduplicates urls that normalise to the same canonical form,
      // helpful for diagnosing duplicate-processing bugs.
      // {SCHEMA: event_media_urls(id,event_id,url,canon_key,kind,status,created_at)}
      // [CONFIDENCE: CONFIRMED — schema from psql ground-truth in task spec].
      rows = await sb(
        `event_media_urls?select=url,kind,status,created_at` +
        `&event_id=eq.${encodeURIComponent(id)}&order=created_at.asc&limit=${ROW_CAP}`
      );

    } else {
      // kind === "basic" — return events.basic_info as a single pseudo-row {md: <text>}.
      // We select ONLY basic_info here (intentionally heavy: the user CHOSE to open this modal,
      // unlike the inventory endpoint which must avoid transferring it every 30s).
      // {SCHEMA: events(... basic_info text ...)}
      // [CONFIDENCE: CONFIRMED — schema from psql ground-truth in task spec].
      const ev = (await sb(
        `events?select=basic_info&id=eq.${encodeURIComponent(id)}&limit=1`
      ))[0];
      // Wrap in the same {kind, event_id, n, rows} envelope as all other kinds for uniform client handling.
      const md = (ev && ev.basic_info) || null;
      rows = md ? [{ md }] : [];
    }

    res.json({ kind, event_id: id, n: rows.length, rows });

  } catch (_e) {
    // Never leak the raw PostgREST error to the client.
    // {nestjs-conventions "never throw raw Supabase/DB errors — they leak internal schema details"}.
    res.status(500).json({ error: "failed to load artifact" });
  }
}
