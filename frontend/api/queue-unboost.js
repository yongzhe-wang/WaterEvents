// POST /api/queue/unboost — return boosted full units to the default priority. Auth: X-Queue-Token.
// The band IS the marker (type='full' AND priority < 100), so undo needs no bookkeeping table.
import { pool, denied, methodNotAllowed, FULL_DEFAULT_PRIORITY } from "../lib/_queue_admin.js";

export default async function handler(req, res) {
  const bad = methodNotAllowed(req, "POST") || denied(req);
  if (bad) return res.status(bad.code).json({ error: bad.error });

  const b = req.body || {};
  const tickers = Array.isArray(b.tickers) ? b.tickers.map((t) => String(t).trim().toUpperCase()).filter(Boolean) : null;
  if (!b.all && !tickers?.length) return res.status(400).json({ error: "pass {all:true} or {tickers:[...]}" });
  const dryRun = b.dry_run === true;                       // unboost is the SAFE direction → defaults to applying

  const p = pool();
  const where = tickers?.length
    ? { sql: `w.type='full' AND w.priority < $1 AND w.company_id IN (SELECT id FROM waterevents.companies WHERE ticker = ANY($2::text[]))`,
        args: [FULL_DEFAULT_PRIORITY, tickers] }
    : { sql: `w.type='full' AND w.priority < $1`, args: [FULL_DEFAULT_PRIORITY] };
  try {
    const n = await p.query(`SELECT count(*)::int c FROM waterevents.work_queue w WHERE ${where.sql}`, where.args);
    if (dryRun) return res.status(200).json({ would_reset: n.rows[0].c, dry_run: true, updated: 0 });
    const upd = await p.query(
      `UPDATE waterevents.work_queue w SET priority=$1, updated_at=now() WHERE ${where.sql}`,
      [FULL_DEFAULT_PRIORITY, ...where.args.slice(1)]);
    return res.status(200).json({ matched: n.rows[0].c, updated: upd.rowCount, priority: FULL_DEFAULT_PRIORITY });
  } catch (e) {
    return res.status(500).json({ error: String(e.message || e) });
  }
}
