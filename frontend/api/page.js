// GET /api/page?event_id=<uuid> — the ORIGINAL rendered page content the LLM extracted this event FROM, PLUS the raw
// VLM prompt (system persona) it was extracted UNDER. Looks up the event's source_url + company_id (waterevents.events),
// then the stored page text (waterevents.pages, keyed by company_id+url). Shape: {url, content, n_chars, system_prompt}.
// Lets the dashboard show BOTH "what we asked the model" (prompt) and "what the model actually read" (page) per event.
// {USER 2026-07-24 "button shows the original page content"; USER 2026-07-26 "see the raw prompt + page"}.
import { sb } from "../lib/_db.js";

// The EXTRACTION system prompt the VLM runs under — a VERBATIM copy of agent/event_agent/prompts.py SYSTEM_EVENTS (the
// source of truth; keep in sync if that changes). Shown read-only so you can verify the persona + rules the model saw.
// The full user turn = this system prompt + the page content below (with links inline-tagged as [anchor](Lnn)).
const SYSTEM_EVENTS = `You are an INVESTOR reading a company's Investor-Relations page. The page is shown to you in reading order with every link inline as [anchor text](Lnn) right where it appears (Lnn is a short reference id — L1, L2, …). You are scanning the page for the EVENTS you, as an investor, actually care about, and writing them down as JSON.

Think like an investor: what has happened or is scheduled that matters to a shareholder? — earnings calls and results, dividends and buybacks, SEC/regulatory filings (10-K, 10-Q, 8-K, proxy), investor presentations and slide decks, webcasts, conference and fireside-chat appearances, and annual or special shareholder meetings, plus material press releases. You do NOT write down the site's navigation menu, product or marketing pages, careers, or social links — those are not events (another step handles navigation).

For EACH event you spot, record: title (verbatim headline), date (exact granularity shown), type (earnings / press_release / presentation / filing / webcast / conference / shareholder_meeting / dividend / other), urls (the Lnn ids of links belonging to THIS event), and evidence (a 3-12 word snippet COPIED VERBATIM proving the event is real).

EXTRACT, DON'T GENERATE — every event MUST carry verbatim evidence copied from the page; a bare nav link with no dated disclosure has no evidence → output nothing for it. Output STRICT JSON only: {"events": [{"title":"","date":"","type":"","urls":["L3"],"evidence":""}]}. If no events, return {"events": []}.`;

export default async function handler(req, res) {
  const id = (req.query && req.query.event_id) || "";
  if (!id) { res.status(400).json({ error: "event_id required" }); return; }
  try {
    // 1) the event → which page it came from (source_url) + which company (company_id)
    const ev = (await sb(`events?select=source_url,company_id&id=eq.${encodeURIComponent(id)}&limit=1`))[0];
    if (!ev || !ev.source_url) { res.json({ url: null, content: null, n_chars: 0, system_prompt: SYSTEM_EVENTS }); return; }
    // 2) that page's stored rendered text (unique per company_id+url)
    const pg = (await sb(
      `pages?select=url,content,n_chars&company_id=eq.${ev.company_id}` +
      `&url=eq.${encodeURIComponent(ev.source_url)}&limit=1`))[0];
    // always include the extraction system_prompt so the UI can show "what we asked" beside "what the model read"
    res.json({ ...(pg || { url: ev.source_url, content: null, n_chars: 0 }), system_prompt: SYSTEM_EVENTS });
  } catch (_e) {
    res.status(500).json({ error: "failed to load page" });   // never leak the raw PostgREST error (nestjs-conventions)
  }
}
