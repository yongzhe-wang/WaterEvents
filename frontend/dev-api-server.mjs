// Local dev shim that serves the Vercel serverless `api/*.js` functions on :8100.
//
// WHY this exists: the repo's intended local dev path (vite proxy /api -> FastAPI
// :8100 in dashboard/backend/app.py) is now STALE — app.py reads a local SQLite
// `data/companies.db` that the Supabase migration deleted.
// {ir_pipeline/config.py:42 "REMOVE THE DATA FOLDER, THE DATA SHOULD BE ALL ON SUPABASE"}
// [CONFIDENCE: CONFIRMED 100% — verified `ls data/companies.db` → "No such file or directory" this session]
// The LIVE data path is the Vercel functions in api/*.js, which read Supabase via
// a hardcoded URL + publishable key (no env needed). {api/_db.js:7-8 "const
// SUPABASE_URL = ...supabase.co; const SUPABASE_KEY = 'sb_publishable_...'"}
// [CONFIDENCE: CONFIRMED 100% — read _db.js this session]. This shim mounts those
// exact same handlers locally so `npm run dev` shows real prod data with zero
// vercel-CLI / auth setup. Run: `node dev-api-server.mjs` (vite proxies /api here).

import { createServer } from "node:http";
import { pathToFileURL } from "node:url";
import { readFile, stat } from "node:fs/promises";
import { join, extname, normalize } from "node:path";
import { timingSafeEqual } from "node:crypto";

// ── HTTP BASIC AUTH over the WHOLE surface ──────────────────────────────────────────────────────────────────────────
// 用一句话讲完: 这个进程既 serve React SPA 又 serve /api/*,而它监听 0.0.0.0:8080 且防火墙对 0.0.0.0/0 开放 —— 在此之前
// 任何人都能直接看到全部爬取数据和队列状态。把 gate 放在请求入口(静态和 API 分叉之前)意味着一处生效、覆盖全部,
// 浏览器原生弹框,前端一行都不用改。
//
// FAIL-CLOSED, matching lib/_queue_admin.js: an unset password denies everything rather than defaulting to open,
// because the opposite default means a deploy that forgets the env var silently re-exposes the dashboard and the
// failure mode is invisible. The 503 body says exactly which variable to set so that failure is self-explaining.
//
// The credential is read from the environment and MUST NOT be written into this repo. This codebase already carries a
// production Postgres password, a vLLM key and a proxy credential in tracked files and in git history — adding another
// literal here would repeat the exact finding two independent audits ranked as their #1.
// {AUDIT 2026-07-28 "PRODUCTION POSTGRES PASSWORD COMMITTED IN PLAINTEXT ACROSS 8 FILES, 3 COMMITS"}
//
// HONEST LIMIT: port 8080 is plain HTTP with no TLS, so Basic Auth sends base64(user:pass) on every request and anyone
// on the network path can read it. This raises the bar from "no authentication at all" to "needs a credential"; it does
// NOT make the channel confidential. Put this behind TLS (or an SSH tunnel / IAP) before treating the password as a
// real secret. [CONFIDENCE: CONFIRMED 100% — `ss -tlnp` shows 0.0.0.0:8080 and the firewall rule allow-webapp-8080 is
//  sourced 0.0.0.0/0; there is no TLS terminator in front of this process].
const AUTH_USER = process.env.WEBAPP_USER || "focusalpha";
const AUTH_PASS = process.env.WEBAPP_PASSWORD || "";

function authFailure(req) {
  if (!AUTH_PASS) return { code: 503, msg: "WEBAPP_PASSWORD not configured — the dashboard is disabled" };
  const hdr = req.headers?.authorization || "";
  if (!hdr.startsWith("Basic ")) return { code: 401, msg: "authentication required" };
  let got = "";
  try { got = Buffer.from(hdr.slice(6), "base64").toString("utf8"); } catch { return { code: 401, msg: "bad credentials" }; }
  const want = `${AUTH_USER}:${AUTH_PASS}`;
  // Length check first so a wrong-length guess cannot leak timing, then a constant-time compare on equal-length buffers
  // (timingSafeEqual throws on a length mismatch, which is why the guard has to come first).
  const a = Buffer.from(got), b = Buffer.from(want);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return { code: 401, msg: "bad credentials" };
  return null;
}

// STATIC WEB APP serving (production self-host on the GCP VM): besides the /api/* shim, this same process serves the
// built SPA from WEBAPP_DIST so ONE node process = the whole site (no nginx). /api/* → handlers; everything else → a
// static file from dist/, falling back to index.html for client-side routes. {USER 2026-07-27 "move web app to GCP media vm"}.
const DIST = process.env.WEBAPP_DIST || new URL("./dist", import.meta.url).pathname;
const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
  ".svg": "image/svg+xml", ".json": "application/json", ".ico": "image/x-icon", ".png": "image/png", ".jpg": "image/jpeg",
  ".woff2": "font/woff2", ".woff": "font/woff", ".webmanifest": "application/manifest+json", ".map": "application/json" };

async function serveStatic(pathname, rawRes) {
  const rel = pathname === "/" ? "/index.html" : pathname;
  let file = normalize(join(DIST, rel));
  if (!file.startsWith(DIST)) { rawRes.writeHead(403); rawRes.end("forbidden"); return; }   // path-traversal guard
  try {
    const s = await stat(file);
    if (s.isDirectory()) file = join(file, "index.html");
    const buf = await readFile(file);
    rawRes.writeHead(200, { "Content-Type": MIME[extname(file)] || "application/octet-stream" });
    rawRes.end(buf);
  } catch {
    try {                                                    // SPA fallback: unknown path → index.html (client routing)
      const buf = await readFile(join(DIST, "index.html"));
      rawRes.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      rawRes.end(buf);
    } catch { rawRes.writeHead(404); rawRes.end("not found"); }
  }
}

// Static path -> handler module file. The vite proxy forwards every /api/* call
// here; each Vercel handler is `export default async (req, res) => {}`.
// {api/stats.js:4 "export default async function handler(_req, res)"}
// Five entries here pointed at files that do not exist (stats / ir-companies / token-usage / status / discovery —
// leftovers from the ir-pipeline this repo replaced). They were not harmless: a request to one of them fell through to
// loadHandler's dynamic import(), which throws, and the catch-all replies with String(e) — i.e. it returned the
// server's absolute filesystem paths to an unauthenticated caller on the public internet. Meanwhile the two handlers
// that DO exist, media.js and health.js, had no entry at all, so /api/media 404'd under this shim even though
// MediaView fetches it (production works only because Vercel routes by filename, not by this table).
// {AUDIT 2026-07-28 — verified by listing api/ against every "./api/*.js" literal in this file}
// [CONFIDENCE: CONFIRMED 100% — the five files are absent from api/ and the two present ones were unrouted].
const STATIC_ROUTES = {
  "/api/companies": "./api/companies.js",
  "/api/media": "./api/media.js",     // Media tab — server-side paginated (was an unbounded full-table scan)
  "/api/health": "./api/health.js",   // liveness probe
  "/api/today": "./api/today.js",     // Today dashboard: work_queue state + newest events (this session)
  "/api/usage": "./api/usage.js",     // day-level render/VLM usage history for the click-to-chart modal
  "/api/events": "./api/events.js",   // events list (EventsView) — was missing from the shim (prod-only)
  "/api/page": "./api/page.js",       // source-page content per event (EventsView modal)
  "/api/irurls": "./api/irurls.js",   // per-company IR entry urls (ir_url + ir_url_agent's event_hubs) — IR_URLS tab
  // QUEUE ADMIN — the only WRITE routes in this server. All three are token-gated (fail-closed) and go through a
  // column-scoped Postgres role, never the anon key (which is read-only) and never service_role. They change only
  // work_queue.priority/due_at on `queued` rows, so a live crawl is never interrupted. {lib/_queue_admin.js}.
  "/api/queue/boost": "./api/queue-boost.js",       // POST — move chosen full units to the front
  "/api/queue/unboost": "./api/queue-unboost.js",   // POST — put them back at the default priority
  "/api/queue/boosted": "./api/queue-boosted.js",   // GET  — what is boosted + what it has produced so far
};

// Cache the dynamically-imported handler modules so we hit disk once per route.
const moduleCache = new Map();
async function loadHandler(relPath) {
  // Re-use an already-imported module if present (handlers are stateless).
  if (moduleCache.has(relPath)) return moduleCache.get(relPath);
  // pathToFileURL so the ESM import works on macOS absolute paths.
  const mod = await import(pathToFileURL(new URL(relPath, import.meta.url).pathname).href);
  moduleCache.set(relPath, mod.default);
  return mod.default;
}

// Resolve a request pathname to a handler + the dynamic params it implies.
// Only one dynamic route exists: /api/companies/:ticker -> api/companies/[ticker].js.
// {api/companies/[ticker].js:11 "const ticker = String(req.query.ticker || '')"}
function route(pathname) {
  // Exact static match first (the common case).
  if (STATIC_ROUTES[pathname]) return { relPath: STATIC_ROUTES[pathname], params: {} };
  // /api/companies/<TICKER> — MIRROR vercel.json's rewrite so dev matches prod: production rewrites
  // {source:"/api/companies/:ticker" → dest:"/api/companies?ticker=:ticker"}, i.e. it's served by
  // companies.js reading req.query.ticker — the old ./api/companies/[ticker].js file never existed in this
  // tree (the API was refactored to the single companies.js handler; the shim was left stale, 404-ing the
  // Events detail view). {vercel.json rewrites[0] "/api/companies/:ticker → /api/companies?ticker=:ticker"}
  // [CONFIDENCE: CONFIRMED — read vercel.json + confirmed no api/companies/ dir exists].
  const m = pathname.match(/^\/api\/companies\/([^/]+)$/);
  if (m) return { relPath: "./api/companies.js", params: { ticker: decodeURIComponent(m[1]) } };
  // /api/artifact/<ID> — one artifact's FULL content, lazy-loaded when an artifact is expanded.
  const am = pathname.match(/^\/api\/artifact\/([^/]+)$/);
  if (am) return { relPath: "./api/artifact/[id].js", params: { id: decodeURIComponent(am[1]) } };
  // No route matched.
  return null;
}

const server = createServer(async (rawReq, rawRes) => {
  // Parse pathname + querystring once; base is arbitrary (we only use path+query).
  const url = new URL(rawReq.url, "http://localhost");

  // AUTH GATE — before the static/API fork, so ONE check covers the SPA, every /api/* route and every 404. Placing it
  // after the fork would have left whichever branch was edited later unprotected.
  const fail = authFailure(rawReq);
  if (fail) {
    // WWW-Authenticate is what makes the browser show its native login prompt instead of rendering a bare 401 body.
    // Only sent on 401 — a 503 means misconfiguration, and prompting for a password that cannot possibly work would
    // send the operator hunting for a bad credential instead of reading the message.
    const headers = { "Content-Type": "application/json" };
    if (fail.code === 401) headers["WWW-Authenticate"] = 'Basic realm="WaterEvents", charset="UTF-8"';
    rawRes.writeHead(fail.code, headers);
    rawRes.end(JSON.stringify({ error: fail.msg }));
    return;
  }
  // Non-API path → serve the static SPA (production self-host); /api/* falls through to the handler shim below.
  if (!url.pathname.startsWith("/api/") && !url.pathname.startsWith("/api?")) {
    await serveStatic(url.pathname, rawRes);
    return;
  }
  const match = route(url.pathname);

  // Unknown path -> 404 JSON (mirrors Vercel's not-found behavior loosely).
  if (!match) {
    rawRes.writeHead(404, { "Content-Type": "application/json" });
    rawRes.end(JSON.stringify({ error: "no route", path: url.pathname }));
    return;
  }

  // Build the Vercel-shaped `req`: query merges ?params with any dynamic path param
  // (e.g. ticker). Read-only handlers only ever touch req.query, but the queue-admin routes need the JSON body
  // and the auth header too — Vercel gives handlers both (req.body pre-parsed, req.headers always populated), so
  // supplying them here makes the shim MORE faithful to production, not less. Body is read only for methods that
  // can carry one, is size-capped, and a malformed payload yields body=null instead of throwing.
  // {api/queue-boost.js reads req.body + req.headers["x-queue-token"]}
  // [CONFIDENCE: CONFIRMED 100% — Vercel's Node runtime parses application/json into req.body].
  const query = Object.fromEntries(url.searchParams.entries());
  Object.assign(query, match.params);
  let body = null;
  if (rawReq.method !== "GET" && rawReq.method !== "HEAD") {
    const chunks = [];
    let bytes = 0;
    for await (const c of rawReq) {
      bytes += c.length;
      if (bytes > 1_000_000) break;      // an admin payload is a few hundred bytes; refuse to buffer more
      chunks.push(c);
    }
    const raw = Buffer.concat(chunks).toString("utf8");
    if (raw) { try { body = JSON.parse(raw); } catch { body = null; } }
  }
  const req = { query, method: rawReq.method, url: rawReq.url, headers: rawReq.headers, body };

  // Minimal `res` shim covering the only three methods the handlers call:
  // setHeader / status / json. {grep "res.json | res.setHeader | res.status"}
  let statusCode = 200;
  const res = {
    setHeader: (k, v) => rawRes.setHeader(k, v),
    // status() must return `this` so `res.status(502).json(...)` chains.
    status(code) { statusCode = code; return this; },
    json(body) {
      rawRes.writeHead(statusCode, { "Content-Type": "application/json" });
      rawRes.end(JSON.stringify(body));
    },
  };

  // Run the handler; any throw becomes a 500 so the dev server never crashes.
  try {
    const handler = await loadHandler(match.relPath);
    await handler(req, res);
  } catch (e) {
    // Log the real error server-side, return a generic body. String(e) on an import failure carries the server's
    // absolute filesystem paths, and this process is reachable from the public internet with no authentication — every
    // other handler already returns a fixed message ("failed to load media" etc.); this catch-all was the one that
    // leaked. {AUDIT 2026-07-28} [CONFIDENCE: CONFIRMED 100% — a dead STATIC_ROUTES entry reached exactly this path].
    console.error("[api] handler failed:", match.relPath, e);
    rawRes.writeHead(500, { "Content-Type": "application/json" });
    rawRes.end(JSON.stringify({ error: "internal error" }));
  }
});

// PORT: dev uses 8490 (vite proxies /api here). Production self-host on the VM sets PORT (e.g. 8080) + WEBAPP_DIST and
// binds 0.0.0.0 so the VM's external IP can reach it. {USER 2026-06-09 "different localhost"; 2026-07-27 "web app on VM"}.
const PORT = Number(process.env.PORT || 8490);
server.listen(PORT, "0.0.0.0", () => console.log(`[web] app+api on http://0.0.0.0:${PORT}  (dist=${DIST})`));
