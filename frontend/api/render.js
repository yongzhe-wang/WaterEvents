// POST /api/render — run ONE url through the render VM's real entry points and hand back exactly what the pipeline
// would have received.
//
// 用一句话讲完: 贴一个 url,选一个入口(render_detail / render_full / render_shot / expand_events_page / fetch_doc),
// 这里转发到 ir-render-16 的 :8100,把它返回的 text / links / html 原样送回浏览器 —— 不裁剪、不美化、不重排,因为
// playground 的价值就在于看到管线真正拿到的那份东西,任何加工都会引入一个"看起来对但其实不是"的假象。
//
// WHY the five entries are exposed separately rather than collapsed into one "render": they are NOT variations of a
// setting, they are different functions with different success tests, and the split is deliberate at code level —
// render_full succeeds on LINK COUNT (discovery), render_detail on CONTENT PRESENCE (enrichment).
// {ORCHESTRATOR.PY "CODE-LEVEL ISOLATION FROM RENDER_FULL (A SEPARATE FUNCTION, NOT A BOOL FLAG)"}
// {USER 2026-07-22 "I WANT CODE LEVEL ISOLATION NOT JUST A TRIGGER"}
// Picking the wrong one is a real diagnostic mistake, so the UI must make the choice explicit.
// [CONFIDENCE: CONFIRMED 100% — read from the orchestrator's own docstring.]
//
// Upstream: PlaygroundView's Render half. Downstream: the render service on ir-render-16 (internal IP, same VPC).

// The same internal address /api/today-media already uses. 10.128.0.11 is ir-render-16 on the VPC — internal, so this
// never leaves Google's network and needs no credential of its own.
// {API/TODAY-MEDIA.JS "CONST RENDER_URL = PROCESS.ENV.RENDER_HEALTH_URL || \"HTTP://10.128.0.11:8100/HEALTH\""}
const RENDER_BASE = (process.env.RENDER_REMOTE_URL || "http://10.128.0.11:8100").replace(/\/+$/, "");

// Whitelist, not passthrough: the entry name is concatenated into a url, so accepting an arbitrary string would let a
// caller reach any path on the render service. The set is closed because the render service's route table is closed.
// {SERVICE.PY "APP.ROUTER.ADD_POST(\"/RENDER_SHOT\", H_RENDER_SHOT)" … "ADD_POST(\"/FETCH_DOC\", H_FETCH_DOC)"}
const ENTRIES = {
  // The stage-2 path: this is literally what handle_html feeds the VLM.
  render_detail: { path: "/render_detail", body: (u, w) => ({ url: u, wait_ms: w }) },
  // The stage-1 discovery path — same chain, but its fallbacks are judged on link count.
  render_full: { path: "/render_full", body: (u, w) => ({ url: u, wait_ms: w }) },
  // Adds the full-page JPEG. Large, so the UI has to ask for it deliberately.
  render_shot: { path: "/render_shot", body: (u, w) => ({ url: u, wait_ms: w }) },
  // Drives the year filter and load-more control. 60-80s by design, not by accident.
  // {SERVICE.PY "IT IS EXPENSIVE ON PURPOSE — UP TO 6 PER-YEAR NAVIGATIONS PLUS LOAD-MORE ROUNDS, ~60-80s"}
  expand_events_page: { path: "/expand_events_page", body: (u) => ({ url: u }) },
  // The document path: download on the render VM, then Docling. Returns a DocResult, not a render tuple.
  fetch_doc: { path: "/fetch_doc", body: (u, _w, extra) => ({ url: u, structured: !!extra.structured }) },
};

// Long enough for the slowest entry that is working correctly, rather than for the median. expand_events_page alone is
// documented at 60-80s and fetch_doc carries a Docling call behind it whose worst observed single document was 735.2s.
// {PROVIDERS/TOOLS_REMOTE "MEASURED 2026-08-03 — EV_013_DOCX 735.2s (OCR ON) / 568.6s (OCR OFF), CONCURRENCY 4"}
// A shorter cap here would report a slow-but-healthy document as a failure, which is the specific misreading this whole
// page exists to prevent. [CONFIDENCE: CONFIRMED 100% — both figures from live runs recorded in the client module.]
const TIMEOUT_MS = Number(process.env.PLAYGROUND_RENDER_TIMEOUT_MS || 900_000);

export default async function handler(req, res) {
  if (req.method === "GET") {
    res.setHeader("Cache-Control", "no-store");
    return res.status(200).json({ entries: Object.keys(ENTRIES), base: RENDER_BASE, timeout_s: TIMEOUT_MS / 1000 });
  }
  if (req.method !== "POST") return res.status(405).json({ error: "POST or GET" });

  const b = req.body || {};
  const url = String(b.url || "").trim();
  const entry = String(b.entry || "render_detail");
  if (!/^https?:\/\//i.test(url)) return res.status(400).json({ error: "url must be http(s)" });
  const spec = ENTRIES[entry];
  if (!spec) return res.status(400).json({ error: `unknown entry ${entry}`, entries: Object.keys(ENTRIES) });

  // wait_ms=0 means "use the service's tuned default", so 0 must be FORWARDED AS 0 rather than replaced with one.
  // {SERVICE.PY "WAIT_MS=0 MEANS \"USE THE MODULE DEFAULT\" — FORWARD THE CALLER'S VALUE ONLY WHEN THEY ACTUALLY SET ONE,
  //  SO THE TUNED SETTLE_FIXED_MS DEFAULT KEEPS APPLYING"}
  const wait = Number.isFinite(Number(b.wait_ms)) ? Number(b.wait_ms) : 0;

  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort("timeout"), TIMEOUT_MS);
  const t0 = Date.now();
  try {
    const r = await fetch(`${RENDER_BASE}${spec.path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // A DEDICATED tenant, not stage-1's. The gate is weighted and work-conserving, so at browser total 24 with
        // weights event:5, media:5, playground:1 this caps at 24 x 1/11 = 2.2 slots while all three are active —
        // plenty for a human pressing a button, and stage-1 can never be pushed below its own share by it. That is
        // strictly better than either borrowing event's slots or having no access at all.
        // {TENANT_GATE.PY "CAP = SELF.TOTAL * SELF.W(TENANT) / SELF._ACTIVE_WEIGHT()"}
        // [CONFIDENCE: CONFIRMED 100% — the cap formula was read from the gate itself.]
        "X-WE-Tenant": "playground",
      },
      body: JSON.stringify(spec.body(url, wait, b)),
      signal: ac.signal,
    });
    const elapsed_ms = Date.now() - t0;
    const text = await r.text();
    let out;
    try { out = JSON.parse(text); } catch { out = { error: "upstream returned non-json", detail: text.slice(0, 2000) }; }

    // shot_b64 is a full-page JPEG and can be several MB. Dropping it unless asked keeps a routine render from pushing
    // megabytes into the browser for a field nobody opened. The LENGTH is reported either way so its absence is a
    // stated fact rather than a silent omission.
    if (out && out.shot_b64 && !b.want_shot) {
      out.shot_b64_bytes = out.shot_b64.length;
      delete out.shot_b64;
    }
    res.setHeader("Cache-Control", "no-store");
    return res.status(r.ok ? 200 : 502).json({ entry, url, elapsed_ms, ...out });
  } catch (e) {
    const why = ac.signal.reason === "timeout"
      ? `no response within ${TIMEOUT_MS / 1000}s`
      : String((e && e.message) || e);
    // 200 with an `error` field, matching the convention every other handler here uses, so the UI has one code path.
    return res.status(200).json({ entry, url, elapsed_ms: Date.now() - t0, error: "render failed", detail: why });
  } finally {
    clearTimeout(timer);
  }
}
