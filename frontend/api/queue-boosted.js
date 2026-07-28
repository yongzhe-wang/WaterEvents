// GET /api/queue/boosted — what is boosted right now and what has it actually produced.
//
// This is the half that makes the feature a DEBUGGING tool rather than just a queue poker: for every boosted unit
// it reports the events the company has now against what its last scan returned, so "did jumping the queue plus the
// new expansion gate actually find more?" is answerable at a glance. Read-only, but still behind the same token so
// the queue's shape isn't public.
import { pool, denied, methodNotAllowed } from "../lib/_queue_admin.js";

export default async function handler(req, res) {
  const bad = methodNotAllowed(req, "GET") || denied(req);
  if (bad) return res.status(bad.code).json({ error: bad.error });
  const p = pool();
  try {
    const { rows } = await p.query(`
      SELECT c.ticker, w.priority, w.status, w.url, w.last_scanned_at, w.last_event_count,
             (SELECT count(*) FROM waterevents.events e WHERE e.company_id = w.company_id)::int AS events_now
      FROM waterevents.work_queue w JOIN waterevents.companies c ON c.id = w.company_id
      WHERE w.type='full' AND w.priority < 100
      ORDER BY (w.last_scanned_at IS NULL), w.last_scanned_at DESC NULLS LAST
      LIMIT 500`);
    const scanned = rows.filter((r) => r.last_scanned_at).length;
    return res.status(200).json({
      total: rows.length, scanned, pending: rows.length - scanned,
      units: rows.map((r) => ({
        ticker: r.ticker, priority: r.priority, status: r.status, url: r.url,
        events_now: r.events_now, last_scan_events: r.last_event_count, last_scanned_at: r.last_scanned_at,
      })),
    });
  } catch (e) {
    return res.status(500).json({ error: String(e.message || e) });
  }
}
