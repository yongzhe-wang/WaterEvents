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
  const url = new URL(rawReq.url, "http://localhost:8100");
  const match = route(url.pathname);

  // Unknown path -> 404 JSON (mirrors Vercel's not-found behavior loosely).
  if (!match) {
    rawRes.writeHead(404, { "Content-Type": "application/json" });
    rawRes.end(JSON.stringify({ error: "no route", path: url.pathname }));
    return;
  }

  // Build the Vercel-shaped `req`: query merges ?params with any dynamic path param
  // (e.g. ticker). The handlers only ever read req.query, so this is sufficient.
  const query = Object.fromEntries(url.searchParams.entries());
  Object.assign(query, match.params);
  const req = { query, method: rawReq.method, url: rawReq.url };

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

// :8490 is the port vite.config.ts proxies /api to. Moved off 8100 so parallel forks don't collide.
// {USER 2026-06-09 "use a different localhost, this one might be used by others"}
server.listen(8490, () => console.log("[dev-api] serverless api/*.js shim on http://localhost:8490"));
