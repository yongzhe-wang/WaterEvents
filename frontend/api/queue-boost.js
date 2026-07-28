// POST /api/queue/boost — move chosen full units to the front of the crawl queue. Auth: X-Queue-Token.
//
// 用一句话讲完: 按 {tickers | max_events | never_full_scanned} 选出 queued 的 full 单元 → 把 priority 降到指定
// 带位(默认 50) → 它们就排到其它 full 前面;dry_run(默认 true)只回报会改哪些,不写库。**本周已经爬过的单元只改
// priority、不动 due_at** —— 插队只改「轮到的顺序」,不能让它在自己的周内被重复抓一次(那是一次白花的 full BFS)。
//
// WHY this endpoint exists: the 2026-07-27 investigation identified 427 companies with fewer than 10 events (122
// with none at all), but every full unit sits at priority 100 and is claimed in arbitrary due_at order, so a
// known-broken company queues behind ~2,300 healthy ones. This turns "scan these next" into one call, with no
// fleet restart and no disruption to in-flight scans.
// {INVESTIGATION 2026-07-27; DB 2026-07-28 "427 queued full units with <10 events, 122 with zero"}
// [CONFIDENCE: CONFIRMED 100% — counts measured on the live queue].
//
// Upstream: an operator (or the Today page control). Downstream: claim_work picks the lowest priority first, so the
// next free worker takes these. Does NOT touch running rows → no in-flight scan is interrupted.
import { pool, denied, methodNotAllowed, selector, FROM, MAX_LIMIT, FULL_INTERVAL_S } from "../lib/_queue_admin.js";

export default async function handler(req, res) {
  const bad = methodNotAllowed(req, "POST") || denied(req);
  if (bad) return res.status(bad.code).json({ error: bad.error });

  const b = req.body || {};
  const priority = Number.isInteger(b.priority) ? b.priority : 50;
  const limit = Math.min(Number.isInteger(b.limit) ? b.limit : 500, MAX_LIMIT);
  const dryRun = b.dry_run !== false;                       // default TRUE — writing must be opted into
  if (priority < 0 || priority >= 100) {
    return res.status(400).json({ error: "priority must be 0..99 (100 is the un-boosted default)" });
  }
  const sel = b.select || {};
  if (!sel.tickers && !Number.isInteger(sel.max_events) && !sel.never_full_scanned) {
    return res.status(400).json({ error: "select must set at least one of tickers / max_events / never_full_scanned" });
  }

  const { where, args } = selector(sel);
  const p = pool();
  try {
    const preview = await p.query(
      `SELECT w.id, c.ticker, w.priority AS old_priority, COALESCE(ev.n,0)::int AS events, w.url
       ${FROM} WHERE ${where} ORDER BY COALESCE(ev.n,0) ASC, c.ticker ASC LIMIT ${limit}`, args);
    const rows = preview.rows;
    const summary = {
      matched: rows.length,
      zero_event_companies: rows.filter((r) => r.events === 0).length,
      priority, limit, dry_run: dryRun,
      sample: rows.slice(0, 20).map((r) => ({ ticker: r.ticker, events: r.events, url: r.url })),
    };
    if (dryRun || !rows.length) return res.status(200).json({ ...summary, updated: 0 });

    // A boost changes the ORDER a unit is claimed in — it must NEVER make one eligible again inside its own week.
    // complete_work re-arms full to last_scan + 7d, so pulling due_at to now() on a unit already crawled this week
    // forces a duplicate full BFS (~400s of render plus a fresh VLM pass) for data we already hold. Advance due_at
    // only once that window has elapsed; otherwise keep the slot and let the higher priority put the unit at the
    // FRONT of next week's sweep. {QUEUE.PY:19 "_FULL_INTERVAL_S ... 7 * 24 * 3600  # weekly re-arm"}
    // {USER 2026-07-28 "EVENTS IF FETCHED THIS WEEK WILL NEVER REFETCH THIS WEEK, EVEN IF PRIORITY IS HIGH BUT
    //  LEAVE TO FIRST AS NEXT WEEK"} [CONFIDENCE: CONFIRMED 100% — direct instruction; an unconditional
    //  due_at=now() re-crawled 7 units inside their week on 2026-07-28 before it was caught].
    const upd = await p.query(
      `UPDATE waterevents.work_queue SET priority=$1, updated_at=now(),
              due_at = CASE WHEN last_scanned_at IS NULL
                              OR last_scanned_at <= now() - make_interval(secs => $3)
                            THEN now() ELSE due_at END
       WHERE id = ANY($2::uuid[]) AND status='queued'`,                 // re-assert status: a row may have been
      [priority, rows.map((r) => r.id), FULL_INTERVAL_S]);              // claimed between the preview and the update
    return res.status(200).json({ ...summary, updated: upd.rowCount, weekly_window_respected: true });
  } catch (e) {
    return res.status(500).json({ error: String(e.message || e) });
  }
}
