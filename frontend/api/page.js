// GET /api/page?event_id=<uuid> — the ORIGINAL rendered page content the LLM extracted this event FROM. Looks up the
// event's source_url + company_id (waterevents.events), then the stored page text (waterevents.pages, keyed by
// company_id+url). Shape: {url, content, n_chars}. Lets the dashboard show "what the model actually read" per event.
// {USER 2026-07-24 "each event have a button that shows the original page content the llm extract from"}.
import { sb } from "../lib/_db.js";

export default async function handler(req, res) {
  const id = (req.query && req.query.event_id) || "";
  if (!id) { res.status(400).json({ error: "event_id required" }); return; }
  try {
    // 1) the event → which page it came from (source_url) + which company (company_id)
    const ev = (await sb(`events?select=source_url,company_id&id=eq.${encodeURIComponent(id)}&limit=1`))[0];
    if (!ev || !ev.source_url) { res.json({ url: null, content: null, n_chars: 0 }); return; }
    // 2) that page's stored rendered text (unique per company_id+url)
    const pg = (await sb(
      `pages?select=url,content,n_chars&company_id=eq.${ev.company_id}` +
      `&url=eq.${encodeURIComponent(ev.source_url)}&limit=1`))[0];
    res.json(pg || { url: ev.source_url, content: null, n_chars: 0 });
  } catch (_e) {
    res.status(500).json({ error: "failed to load page" });   // never leak the raw PostgREST error (nestjs-conventions)
  }
}
