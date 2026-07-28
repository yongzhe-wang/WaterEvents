// Shared plumbing for the three queue-admin routes (boost / unboost / boosted).
//
// 用一句话讲完: 用一个**列级最小权限**的 Postgres 角色(queue_admin,只能 UPDATE work_queue 的 priority/due_at/
// updated_at)开一个 pg pool,每个请求先过 fail-closed 的 token 守卫,再按选择器改那两个整数字段 —— 只碰
// status='queued' 的行,所以在跑的 worker 一个都不受影响。
//
// WHY a dedicated role instead of the service_role key: this webapp is reachable from the public internet
// (verified: curl http://<vm-ip>:8080/ returns 200 from outside), and every other route here is read-only via the
// anon key. Handing a public process the service_role key would trade a read-only surface for a total-control one.
// A column-scoped role cannot touch status / lease_owner / url / type, cannot DELETE, and cannot write events —
// each of those was verified to fail with "permission denied" before this shipped.
// {PSQL 2026-07-28 as queue_admin: "UPDATE ... SET status=... → ERROR: permission denied for table work_queue";
//  "DELETE FROM work_queue → ERROR: permission denied"; "UPDATE ... SET priority=100 → UPDATE 0" (allowed)}
// [CONFIDENCE: CONFIRMED 100% — boundary probed directly against the live database].
import pg from "pg";

// The three priority bands. 0 outranks incremental (10) and so pauses the rotation — reserved for one-off debugging;
// 50 is the bulk default: first among full, still yields to incremental. {claim_work "ORDER BY priority ASC, due_at ASC";
// DB 2026-07-28 "incremental priority 10 / full 100"} [CONFIDENCE: CONFIRMED — read off the claim query and the table].
export const FULL_DEFAULT_PRIORITY = 100;
// The weekly window a boost must never reopen. MUST track queue._FULL_INTERVAL_S on the worker side —
// complete_work re-arms a finished full unit to last_scan + this, and a boost may reorder that slot but not
// cancel it. {QUEUE.PY:19 "_FULL_INTERVAL_S = INT(OS.ENVIRON.GET("EVENTINC_FULL_INTERVAL_S", STR(7*24*3600)))"}
// [CONFIDENCE: CONFIRMED 100% — read off the worker's re-arm path].
export const FULL_INTERVAL_S = Number(process.env.EVENTINC_FULL_INTERVAL_S || 7 * 24 * 3600);
export const MAX_LIMIT = 2000;                 // a typo must not be able to re-prioritise the whole queue

let _pool = null;
export function pool() {
  if (_pool) return _pool;
  const dsn = process.env.QUEUE_ADMIN_DSN;
  if (!dsn) throw new Error("QUEUE_ADMIN_DSN unset");
  // statement_cache/prepared statements are incompatible with the Supavisor transaction pooler, and node-postgres
  // only uses them for explicitly named queries — we issue none, so the default config is safe here.
  _pool = new pg.Pool({ connectionString: dsn, max: 3, idleTimeoutMillis: 10_000, connectionTimeoutMillis: 8_000 });
  return _pool;
}

// FAIL-CLOSED auth. An unset token denies every request rather than defaulting to open — the opposite default would
// mean a fresh deploy that forgot the env var silently exposes queue control to the internet.
export function denied(req) {
  const expected = process.env.QUEUE_ADMIN_TOKEN || "";
  if (!expected) return { code: 503, error: "QUEUE_ADMIN_TOKEN not configured — admin routes disabled" };
  const got = req.headers?.["x-queue-token"] || "";
  // length check first so a wrong-length guess cannot leak timing on the compare
  if (got.length !== expected.length || got !== expected) return { code: 401, error: "bad or missing X-Queue-Token" };
  return null;
}

export function methodNotAllowed(req, want) {
  return req.method === want ? null : { code: 405, error: `use ${want}` };
}

// Build the row filter shared by preview and apply so the two can never select different sets.
// `status='queued'` is the safety property that makes this runnable against a live fleet: a 'running' row is
// mid-scan under a worker's lease and is never touched.
export function selector(sel = {}) {
  const where = ["w.type = 'full'", "w.status = 'queued'"];
  const args = [];
  if (sel.never_full_scanned) where.push("w.last_scanned_at IS NULL");
  if (Array.isArray(sel.tickers) && sel.tickers.length) {
    args.push(sel.tickers.map((t) => String(t).trim().toUpperCase()).filter(Boolean));
    where.push(`c.ticker = ANY($${args.length}::text[])`);
  }
  if (Number.isInteger(sel.max_events)) {
    args.push(sel.max_events);
    where.push(`COALESCE(ev.n, 0) < $${args.length}`);
  }
  return { where: where.join(" AND "), args };
}

export const FROM = `
  FROM waterevents.work_queue w
  JOIN waterevents.companies c ON c.id = w.company_id
  LEFT JOIN (SELECT company_id, count(*) n FROM waterevents.events GROUP BY 1) ev ON ev.company_id = w.company_id`;
