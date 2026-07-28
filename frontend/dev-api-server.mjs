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
const STATIC_ROUTES = {
  "/api/stats": "./api/stats.js",
  "/api/companies": "./api/companies.js",
  "/api/ir-companies": "./api/ir-companies.js",
  "/api/token-usage": "./api/token-usage.js",
  "/api/status": "./api/status.js",   // live health of Supabase / Firecrawl / DeepSeek / OpenAI
  "/api/discovery": "./api/discovery.js",   // event_agent crawl progress — event URLs found per company
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
    rawRes.writeHead(500, { "Content-Type": "application/json" });
    rawRes.end(JSON.stringify({ error: String(e) }));
  }
});

// PORT: dev uses 8490 (vite proxies /api here). Production self-host on the VM sets PORT (e.g. 8080) + WEBAPP_DIST and
// binds 0.0.0.0 so the VM's external IP can reach it. {USER 2026-06-09 "different localhost"; 2026-07-27 "web app on VM"}.
const PORT = Number(process.env.PORT || 8490);
server.listen(PORT, "0.0.0.0", () => console.log(`[web] app+api on http://0.0.0.0:${PORT}  (dist=${DIST})`));
