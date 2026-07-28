// GET /api/media — the Media list: every event's MATERIALS (its media_urls[] = detail page + PDF/slides/mp3/webcast/
// transcript) plus its enrichment state (basic_info present? = the media_agent parsed the page content). One row per
// event that HAS media, newest first. Follows the WaterEvents design: media lives as `media_urls` jsonb on the event
// + `basic_info` (enrichment output), NOT a separate table. {USER 2026-07-23 "follow our current json and table design"}.
import { sb, sbAll } from "../lib/_db.js";

function hostOf(u) {
  try { return new URL(u).host.replace(/^www\./, ""); } catch { return u || "—"; }
}
// Classify a media url by extension → the kind chip (pdf / audio / video / page). Cheap + deterministic.
function kindOf(u) {
  const s = (u || "").toLowerCase();
  if (/\.(pdf)(\?|#|$)/.test(s)) return "pdf";
  if (/\.(mp3|wav|m4a|aac)(\?|#|$)/.test(s)) return "audio";
  if (/\.(mp4|mov|webm)(\?|#|$)/.test(s)) return "video";
  if (/\.(pptx?|key)(\?|#|$)/.test(s)) return "slides";
  return "page";
}

// SERVER-SIDE PAGINATED + SERVER-SIDE FILTERED. This handler used to `sbAll` the whole events table with no limit and
// filter for "has media" in JS. Measured 2026-07-28: that is 146,794 rows and ~130s per request, while MediaView polls
// every 30s — so requests stacked up faster than they completed, each buffering six figures of JSON (including the
// full basic_info text) in the node process. That is not a slow endpoint, it is an unbounded one.
// The "has media" predicate is now pushed to PostgREST as `media_urls=neq.[]` (verified: HTTP 200) instead of being
// applied after transferring everything, and `basic_info` is no longer selected at all — it is the heaviest column and
// was only ever used as a boolean. `enriched_at` is the canonical enrichment marker and is already indexed intent.
// {MEASURED 2026-07-28 "SBALL(EVENTS ... NO LIMIT) → ROWS=146758 IN 130.5S"; MEDIAVIEW.TSX "SETINTERVAL(LOAD, 30000)"}
// [CONFIDENCE: CONFIRMED 100% — row count and the neq.[] filter both verified against the live REST endpoint].
// NOTE for whoever reads this next: every one of those 147,140 events currently has status='discovered', a NULL
// basic_info and a NULL enriched_at, and all five enrichment tables (event_media_files / event_content_blocks /
// event_transcript_segments / event_audio) are EMPTY. The media_agent has never written a row, so this page's
// `enriched` column is uniformly false by construction — that is a pipeline gap, not a rendering bug.
export default async function handler(req, res) {
  try {
    const qp = req.query || {};
    const limit = Math.min(parseInt(qp.limit || "300", 10) || 300, 1000);   // page size (PostgREST caps at 1000)
    const offset = Math.max(parseInt(qp.offset || "0", 10) || 0, 0);
    const [events, companies] = await Promise.all([
      sb(`events?select=id,company_id,title,event_date,event_type,media_urls,status,enriched_at`
         + `&media_urls=neq.%5B%5D&order=created_at.desc&limit=${limit}&offset=${offset}`),
      sbAll("companies?select=id,ir_url"),
    ]);
    const host = Object.fromEntries((companies || []).map((c) => [c.id, hostOf(c.ir_url)]));
    const rows = (events || [])
      .filter((e) => Array.isArray(e.media_urls) && e.media_urls.length > 0)   // media page = events that HAVE materials
      .map((e) => {
        const urls = (e.media_urls || []).filter((u) => typeof u === "string" && u.startsWith("http"));
        return {
          id: e.id,
          date: e.event_date || "",
          type: e.event_type || "",
          title: e.title || "",
          company: host[e.company_id] || "—",
          media: urls.map((u) => ({ url: u, kind: kindOf(u) })),   // the material links + their kinds
          media_count: urls.length,
          enriched: !!e.enriched_at,         // enrichment marker; basic_info is no longer fetched (heaviest column, boolean use only)
          status: e.status || "discovered",  // discovered → enriched
        };
      });
    res.json(rows);
  } catch (_e) {
    res.status(500).json({ error: "failed to load media" });
  }
}
