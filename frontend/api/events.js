// GET /api/events — the flat Events list: every discovered event, newest first, with its company IR host and its
// media count. Reads the waterevents.events + .companies tables live from Supabase (Accept-Profile in lib/_db.js).
// Shape follows the WaterEvents json/table design: {date, type, title, company, url, media_count, status}.
// {USER 2026-07-23 "two pages, one event one media, both list view, simplest ... follow our current json and table design"}.
import { sbAll } from "../lib/_db.js";

// The event's primary click-through url = the first entry of its media_urls[] (the detail page; PDFs/mp3 follow).
function primaryUrl(mediaUrls) {
  const a = Array.isArray(mediaUrls) ? mediaUrls : [];
  return a.find((u) => typeof u === "string" && u.startsWith("http")) || null;
}

// Company display = the IR host (investors.coca-colacompany.com → coca-colacompany.com is too lossy; keep the host).
function hostOf(u) {
  try { return new URL(u).host.replace(/^www\./, ""); } catch { return u || "—"; }
}

export default async function handler(_req, res) {
  try {
    const [events, companies] = await Promise.all([
      // events table (waterevents schema): discovery + enrichment columns. Newest first.
      sbAll("events?select=id,company_id,title,event_date,event_type,media_urls,status,created_at&order=created_at.desc"),
      sbAll("companies?select=id,ir_url"),
    ]);
    const host = Object.fromEntries((companies || []).map((c) => [c.id, hostOf(c.ir_url)]));
    const rows = (events || []).map((e) => ({
      id: e.id,
      date: e.event_date || "",           // kept as text (YYYY / YYYY-MM / YYYY-Q1 / YYYY-MM-DD) — the model's granularity
      type: e.event_type || "",
      title: e.title || "",
      company: host[e.company_id] || "—",
      url: primaryUrl(e.media_urls),
      media_count: Array.isArray(e.media_urls) ? e.media_urls.length : 0,
      status: e.status || "discovered",   // discovered → enriched
    }));
    res.json(rows);
  } catch (_e) {
    res.status(500).json({ error: "failed to load events" });   // never leak the raw PostgREST error (nestjs-conventions)
  }
}
