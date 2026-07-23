// GET /api/media — the Media list: every event's MATERIALS (its media_urls[] = detail page + PDF/slides/mp3/webcast/
// transcript) plus its enrichment state (basic_info present? = the media_agent parsed the page content). One row per
// event that HAS media, newest first. Follows the WaterEvents design: media lives as `media_urls` jsonb on the event
// + `basic_info` (enrichment output), NOT a separate table. {USER 2026-07-23 "follow our current json and table design"}.
import { sbAll } from "../lib/_db.js";

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

export default async function handler(_req, res) {
  try {
    const [events, companies] = await Promise.all([
      sbAll("events?select=id,company_id,title,event_date,event_type,media_urls,basic_info,status,enriched_at&order=created_at.desc"),
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
          enriched: !!e.basic_info,          // media_agent parsed the page content into basic_info
          status: e.status || "discovered",  // discovered → enriched
        };
      });
    res.json(rows);
  } catch (_e) {
    res.status(500).json({ error: "failed to load media" });
  }
}
