// GET /api/events?company_id=&limit=&offset= — ONE PAGE of events, newest first. Server-side paginated + optionally
// filtered to a single company, so the frontend loads a segment at a time instead of the whole table. Returns company_id
// (the frontend maps it to the ticker label via /api/companies) — no more fetching every company row per request.
// {USER 2026-07-24 "web app takes too long to load, do segment loading"}.
import { sb } from "../lib/_db.js";

// The event's primary click-through url = the first entry of its media_urls[] (the detail page; PDFs/mp3 follow).
function primaryUrl(mediaUrls) {
  const a = Array.isArray(mediaUrls) ? mediaUrls : [];
  return a.find((u) => typeof u === "string" && u.startsWith("http")) || null;
}

export default async function handler(req, res) {
  try {
    const qp = req.query || {};
    const limit = Math.min(parseInt(qp.limit || "300", 10) || 300, 1000);   // page size (PostgREST caps at 1000)
    const offset = Math.max(parseInt(qp.offset || "0", 10) || 0, 0);
    let path = "events?select=id,company_id,title,event_date,event_type,media_urls,status&order=created_at.desc";
    if (qp.company_id) path += `&company_id=eq.${encodeURIComponent(qp.company_id)}`;   // one company's segment
    path += `&limit=${limit}&offset=${offset}`;
    const events = await sb(path);
    const rows = (events || []).map((e) => ({
      id: e.id,
      company_id: e.company_id,           // frontend maps → ticker label via /api/companies (no per-request company fetch)
      date: e.event_date || "",           // kept as text (YYYY / YYYY-MM / YYYY-Q1 / YYYY-MM-DD) — the model's granularity
      type: e.event_type || "",
      title: e.title || "",
      url: primaryUrl(e.media_urls),
      media_count: Array.isArray(e.media_urls) ? e.media_urls.length : 0,
      status: e.status || "discovered",   // discovered → enriched
    }));
    res.json(rows);
  } catch (_e) {
    res.status(500).json({ error: "failed to load events" });   // never leak the raw PostgREST error (nestjs-conventions)
  }
}
