// GET /api/usage — the day's resource-usage history for the chart: hourly buckets of render pages/hr + VLM calls/hr,
// aggregated from waterevents.scan_log over the last 24h. Powers the click-to-chart modal on the Today page's CPU/VLM
// cards. {USER 2026-07-27 "click to show a line chart for both usage at the day level"}.
import { sbAll } from "../lib/_db.js";

export default async function handler(_req, res) {
  try {
    const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();   // last 24h
    const rows = await sbAll(`scan_log?select=ts,render_pages,vlm_calls&ts=gt.${since}&order=ts.asc`);
    // fold each scan into its hour bucket → per-hour totals (= that hour's render pages + VLM calls, i.e. the usage rate)
    const buckets = new Map();
    for (const r of rows || []) {
      const h = new Date(r.ts); h.setMinutes(0, 0, 0);
      const k = h.toISOString();
      const b = buckets.get(k) || { ts: k, pages: 0, calls: 0 };
      b.pages += r.render_pages || 0;
      b.calls += r.vlm_calls || 0;
      buckets.set(k, b);
    }
    res.json({ rows: [...buckets.values()] });
  } catch (_e) {
    res.status(500).json({ error: "failed to load usage", rows: [] });   // never leak the raw PostgREST error
  }
}
