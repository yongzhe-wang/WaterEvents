// GET /api/companies — the company RAIL data: every company that has events, with its ticker label + event count,
// most-events first. Reads the waterevents.event_companies VIEW (a single GROUP BY aggregate) instead of paging the
// whole events table, so the rail renders INSTANTLY regardless of how many events exist. {USER 2026-07-24 "web app takes
// too long to load, do segment loading" — the rail no longer waits on the full events fetch}.
import { sbAll } from "../lib/_db.js";

// Ticker is the label; fall back to the IR host only when a company has no ticker.
function hostOf(u) { try { return new URL(u).host.replace(/^www\./, ""); } catch { return u || "—"; } }

export default async function handler(_req, res) {
  try {
    const rows = await sbAll("event_companies?select=id,ticker,ir_url,event_count&order=event_count.desc");
    res.json((rows || []).map((r) => ({
      id: r.id,
      label: r.ticker || hostOf(r.ir_url),   // ticker first (L, ET, BOKF…), host only if no ticker
      event_count: r.event_count || 0,
    })));
  } catch (_e) {
    res.status(500).json({ error: "failed to load companies" });   // never leak the raw PostgREST error
  }
}
