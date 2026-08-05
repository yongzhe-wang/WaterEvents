// GET /api/today — the "Today" dashboard: (1) the live WORK QUEUE state (full weekly-BFS units + incremental deep=1
// hub units, shown together) and (2) the newest events, descending by date. Reads waterevents.work_queue + .events +
// .companies live from Supabase. {USER 2026-07-25 "today page: current worker queue (full runs + deep=1 together) +
// new events descending by date"}.
import { sbAll } from "../lib/_db.js";
import os from "node:os";
import { readFileSync } from "node:fs";   // /proc/meminfo — see the control block for why not os.freemem()

// LIVE resource usage — four services fan-out concurrently. After the render split (2026-08-05) this webapp runs on
// ir-media-8 (a 2-core control box); the actual render VM is 10.128.0.11:8100. Reading os.cpus() here is STILL correct
// but describes the CONTROL HOST, not the render host — the two are separate machines now. {USER 2026-08-05 "render host
// is a different machine (10.128.0.11) since the render split; correct the assumption, not just the comment"}.
// Shape returned matches the frozen contract:
//   { control, render, docling, whisper, vlm }
// every key is either a populated object or null (never throws, never delays >3s). {CONTRACT SECTION C 2026-08-05
// "ALL FOUR FETCHES RUN CONCURRENTLY WITH Promise.allSettled AND A 3S ABORTSIGNAL.TIMEOUT EACH."}.
// [CONFIDENCE: CONFIRMED 100% — contract text supplied verbatim by user 2026-08-05]
async function liveResources() {
  // ── CONTROL ─────────────────────────────────────────────────────────────────────────────────────────────────────────
  // This process runs on ir-media-8 (the control/webapp host), NOT on the render VM. os.cpus() and os.loadavg()
  // correctly describe ir-media-8. The old comment "render host" was wrong after the split; now labelled "control".
  // {CONTRACT SECTION C 2026-08-05 "control: {cores, load, pct, mem_used_mb, mem_total_mb} | null — THIS HOST (ir-media-8), FROM NODE OS"}
  // [CONFIDENCE: CONFIRMED 100% — user explicitly called out the wrong assumption in the task description]
  let control = null;
  try {
    const cores = os.cpus().length;                          // how many logical CPUs this control box has
    const load1 = os.loadavg()[0];                          // 1-min load average of THIS host (ir-media-8), not render
    // Memory from /proc/meminfo, NOT os.freemem(). On Linux node's freemem() is sysinfo().freeram = MemFree, which
    // counts page cache as USED and so overstates the number — while the render and tools services both compute
    // total − MemAvailable per the contract. Three cards labelled "memory used" computed two different ways is the
    // quiet kind of wrong that misleads without ever looking broken, so this box reads the same file they do.
    // {CONTRACT SECTION A/B 2026-08-05 "MEMORY FROM /PROC/MEMINFO (MemTotal, MemAvailable) — USED = TOTAL - AVAILABLE"}
    // [CONFIDENCE: CONFIRMED — node docs define freemem() as the OS free-memory call, which on Linux is MemFree].
    let memTotalMb = null, memUsedMb = null;
    try {
      const mi = readFileSync("/proc/meminfo", "utf8");
      const kb = (k) => { const m = mi.match(new RegExp("^" + k + ":\\s+(\\d+)", "m")); return m ? +m[1] : null; };
      const total = kb("MemTotal"), avail = kb("MemAvailable");
      if (total != null && avail != null) { memTotalMb = Math.round(total / 1024); memUsedMb = Math.round((total - avail) / 1024); }
    } catch { /* not Linux, or /proc unreadable → leave both null; the card just omits the memory line */ }
    control = {
      cores,
      load: +load1.toFixed(2),
      pct: Math.round((load1 / cores) * 100),
      mem_used_mb: memUsedMb,
      mem_total_mb: memTotalMb,
    };
  } catch { control = null; }                               // os module failure → card shows "—"

  // ── RENDER ──────────────────────────────────────────────────────────────────────────────────────────────────────────
  // The render VM (10.128.0.11) exposes /health with a "host" sub-object plus render-specific fields. Env override
  // RENDER_HEALTH_URL allows staging/dev to point elsewhere without code change.
  // {CONTRACT SECTION C 2026-08-05 "RENDER HTTP://10.128.0.11:8100/HEALTH (OVERRIDE WITH ENV RENDER_HEALTH_URL)"}
  // [CONFIDENCE: CONFIRMED 100% — verbatim from contract]
  const renderUrl = process.env.RENDER_HEALTH_URL || "http://10.128.0.11:8100/health";

  // ── DOCLING ─────────────────────────────────────────────────────────────────────────────────────────────────────────
  // Docling PDF-parse worker: local tunnel at :8101, or env override.
  // {CONTRACT SECTION C 2026-08-05 "DOCLING HTTP://127.0.0.1:8101/HEALTH (OVERRIDE WITH ENV DOCLING_HEALTH_URL)"}
  // [CONFIDENCE: CONFIRMED 100%]
  const doclingUrl = process.env.DOCLING_HEALTH_URL || "http://127.0.0.1:8101/health";

  // ── WHISPER ─────────────────────────────────────────────────────────────────────────────────────────────────────────
  // Whisper transcription worker: local tunnel at :8102, or env override.
  // {CONTRACT SECTION C 2026-08-05 "WHISPER HTTP://127.0.0.1:8102/HEALTH (OVERRIDE WITH ENV WHISPER_HEALTH_URL)"}
  // [CONFIDENCE: CONFIRMED 100%]
  const whisperUrl = process.env.WHISPER_HEALTH_URL || "http://127.0.0.1:8102/health";

  // ── CONCURRENT FAN-OUT ──────────────────────────────────────────────────────────────────────────────────────────────
  // All four HTTP fetches (render /health, docling /health, whisper /health, vllm /metrics) fire at the same time.
  // Promise.allSettled means one down service never delays the others — each 3s timeout is independent.
  // {CONTRACT SECTION C 2026-08-05 "ALL FOUR FETCHES RUN CONCURRENTLY WITH Promise.allSettled AND A 3S
  // ABORTSIGNAL.TIMEOUT EACH. A SERVICE THAT IS DOWN YIELDS null FOR ITS KEY."} [CONFIDENCE: CONFIRMED 100%]
  const [renderR, doclingR, whisperR, vlmR] = await Promise.allSettled([

    // fetch render /health; parse the "host" sub-object + render-specific counters from the response JSON.
    fetch(renderUrl, { signal: AbortSignal.timeout(3000) }).then((r) => r.json()),

    // fetch docling /health; contains concurrency/inflight/done/errors + host sub-object.
    fetch(doclingUrl, { signal: AbortSignal.timeout(3000) }).then((r) => r.json()),

    // fetch whisper /health; contains concurrency/inflight/done/errors + gpu sub-object (may be null if no CUDA).
    fetch(whisperUrl, { signal: AbortSignal.timeout(3000) }).then((r) => r.json()),

    // vLLM /metrics is Prometheus text format — parse exactly as before. :8000 is the RunPod SSH tunnel.
    // This block is UNCHANGED from the previous implementation. {USER 2026-07-27 "add two cards: cpu bottleneck
    // current usage + vlm usage current"} [CONFIDENCE: CONFIRMED 100% — kept verbatim, only moved into allSettled]
    fetch("http://127.0.0.1:8000/metrics", { signal: AbortSignal.timeout(3000) }).then((r) => r.text()),
  ]);

  // ── RENDER PARSE ────────────────────────────────────────────────────────────────────────────────────────────────────
  // Field mapping: render.cores/load/pct ← host.cores / host.load1 / host.load_pct (computed by the render service).
  //                render.inflight/total/by_method/browser ← top-level fields from the render /health response.
  // {CONTRACT SECTION C 2026-08-05 "RENDER.CORES/LOAD/PCT <- HOST.CORES / HOST.LOAD1 / HOST.LOAD_PCT
  //  RENDER.INFLIGHT/TOTAL/BY_METHOD/BROWSER <- INFLIGHT / TOTAL / BY_METHOD / BROWSER"} [CONFIDENCE: CONFIRMED 100%]
  let render = null;
  if (renderR.status === "fulfilled") {
    try {
      const d = renderR.value;
      const h = d.host || {};                               // "host" sub-object from the render service /health
      render = {
        cores:       h.cores     ?? null,
        load:        h.load1     ?? null,                  // load1 = 1-min average, as named in the /health contract
        pct:         h.load_pct  ?? null,
        inflight:    d.inflight  ?? null,
        total:       d.total     ?? null,
        by_method:   d.by_method ?? null,
        browser:     d.browser   ?? null,
        mem_used_mb: h.mem_used_mb  ?? null,
        mem_total_mb:h.mem_total_mb ?? null,
      };
    } catch { render = null; }                             // malformed JSON → degrade to null
  }

  // ── DOCLING PARSE ───────────────────────────────────────────────────────────────────────────────────────────────────
  // docling.done ← the "docling" counter in the response (the counter name mirrors the worker type).
  // {CONTRACT SECTION C 2026-08-05 "DOCLING.DONE <- THE 'DOCLING' COUNTER"} [CONFIDENCE: CONFIRMED 100%]
  let docling = null;
  if (doclingR.status === "fulfilled") {
    try {
      const d = doclingR.value;
      const h = d.host || {};                               // "host" sub-object from the docling /health response
      docling = {
        concurrency: d.concurrency ?? null,
        inflight:    d.inflight    ?? null,
        done:        d.docling     ?? null,                // counter named "docling" per contract field mapping
        errors:      d.errors      ?? null,
        cores:       h.cores       ?? null,
        load:        h.load1       ?? null,
        pct:         h.load_pct    ?? null,
      };
    } catch { docling = null; }
  }

  // ── WHISPER PARSE ───────────────────────────────────────────────────────────────────────────────────────────────────
  // whisper.done ← the "whisper" counter; whisper.gpu_* ← gpu sub-object (null when no CUDA device).
  // {CONTRACT SECTION C 2026-08-05 "WHISPER.DONE <- THE 'WHISPER' COUNTER;
  //  WHISPER.GPU_* <- GPU.MEM_USED_MB / GPU.MEM_TOTAL_MB / GPU.UTIL_PCT"} [CONFIDENCE: CONFIRMED 100%]
  let whisper = null;
  if (whisperR.status === "fulfilled") {
    try {
      const d = whisperR.value;
      const gpu = d.gpu || null;                            // gpu sub-object is null when no CUDA device present
      whisper = {
        device:          d.device        ?? null,
        concurrency:     d.concurrency   ?? null,
        inflight:        d.inflight      ?? null,
        done:            d.whisper       ?? null,          // counter named "whisper" per contract field mapping
        errors:          d.errors        ?? null,
        gpu_mem_used_mb: gpu?.mem_used_mb  ?? null,
        gpu_mem_total_mb:gpu?.mem_total_mb ?? null,
        gpu_util_pct:    gpu?.util_pct     ?? null,
      };
    } catch { whisper = null; }
  }

  // ── VLM PARSE ───────────────────────────────────────────────────────────────────────────────────────────────────────
  // Prometheus text-format parse — UNCHANGED from the original implementation. kv_cache_usage_perc is the primary key;
  // gpu_cache_usage_perc is the legacy alias used by older vLLM builds.
  // {USER 2026-07-27 "ADD TWO CARDS: CPU BOTTLENECK CURRENT USAGE + VLM USAGE CURRENT"} [CONFIDENCE: CONFIRMED 100%]
  let vlm = null;
  if (vlmR.status === "fulfilled") {
    try {                                                   // vLLM /metrics is unauth'd; :8000 is the RunPod tunnel
      const txt = vlmR.value;
      const g = (k) => { const m = txt.match(new RegExp(k + "\\{[^}]*\\}\\s+([\\d.eE+-]+)")); return m ? +m[1] : null; };
      const kv = g("vllm:kv_cache_usage_perc") ?? g("vllm:gpu_cache_usage_perc");
      vlm = { running: g("vllm:num_requests_running"), waiting: g("vllm:num_requests_waiting"),
              kv_pct: kv == null ? null : Math.round(kv * 100) };
    } catch { vlm = null; }                                // tunnel down / not self-hosted → card shows "—"
  }

  return { control, render, docling, whisper, vlm };
}

function hostOf(u) { try { return new URL(u).host.replace(/^www\./, ""); } catch { return u || "—"; } }
function primaryUrl(mediaUrls) {
  const a = Array.isArray(mediaUrls) ? mediaUrls : [];
  return a.find((u) => typeof u === "string" && u.startsWith("http")) || null;
}

export default async function handler(_req, res) {
  try {
    const [queue, events, companies, sched] = await Promise.all([
      // the whole queue is bounded (~a few thousand rows) → fetch + aggregate in JS (PostgREST has no GROUP BY over REST).
      // url + company_id come along so we can list the NEXT-UP items (not just the counts). {USER 2026-07-26 "show a list
      // of next 5 urls or companies waiting to be done"}.
      sbAll("work_queue?select=type,status,due_at,last_event_count,url,company_id,last_scanned_at"),
      // order by created_at DESC = by WHEN WE DISCOVERED the event on a run (NOT by event_date — that's display-only). The
      // feed shows most-recently-discovered first. {USER 2026-07-26 "by when discovered from the run; the date is just for
      // visual, no sorting"}.
      sbAll("events?select=id,company_id,title,event_date,event_type,media_urls,created_at&order=created_at.desc&limit=200"),
      sbAll("companies?select=id,ir_url,ticker"),
      // the SCHEDULER row — the pacer (packing solver) already computed T*/binding/C_R/C_V/hit_rate/ETA and stored it here,
      // so the dashboard just reads this one row (no REST-side aggregation of scan_log). {pacer._publish → scheduler_state}.
      sbAll("scheduler_state?select=profile,t_star_s,binding,c_r,c_v,hit_rate,inc_hubs,note,updated_at&id=eq.1"),
    ]);

    // company label map (id → ticker, fallback IR host) — used by both the queue next-up list and the events feed.
    const host = Object.fromEntries((companies || []).map((c) => [c.id, c.ticker || hostOf(c.ir_url)]));

    // QUEUE — summarize each type (full = weekly deep BFS; incremental = deep=1 hub scan), together. Plus `next` = the
    // NEXT-UP items a worker will claim: queued rows in claim order (soonest due_at first), top 5, each with its company
    // label + url so you can SEE what's about to run, not just how many. {USER 2026-07-26 "show a list of next 5 urls or
    // companies waiting to be done"}.
    const now = Date.now();
    // Staleness windows for the "not refreshed recently" counts. A unit is covered iff it was last_scanned inside the
    // window; else it is behind. {USER 2026-07-27 "full: this week we still have N not done; incremental: this cycle we
    // still have N urls not finished"}.
    const weekAgo = now - 7 * 24 * 3600 * 1000;
    // WINDOW = EACH LANE'S OWN PERIOD (incremental T*, full 7 days), because this figure is ROUND PROGRESS —
    // "how many units are still owed a visit in the current rotation" — and a rotation's own length is the only correct
    // denominator for that.
    // Both previous attempts failed, in opposite directions, and the pair is worth remembering:
    //   • T* as a HEALTH signal was self-certifying — the pacer re-spaces every queued hub across exactly one T*, and
    //     when the fleet slowed the solver published a LARGER T*, so the window widened with the problem and the count
    //     stayed 0. Measured: 997/1000 scanned inside a 23.8h window, 0 stale, while 5,754 sat queued.
    //   • A fixed 24h window then made it useless the other way: with T* at 3.86h every hub is necessarily scanned
    //     several times inside 24h, so it read 0 permanently.
    // What resolves the contradiction is that health moved OUT of this card: waterevents.fleet_health() answers it on
    // production (VLM calls returning zero events), which no window can game. This card is then free to answer progress.
    // {MEASURED 2026-07-28 "997/1000 SCANNED WITHIN 23.8H, 0 STALE, 5754 QUEUED, remaining=0"}
    // {MEASURED 2026-07-29 same rows: 24h window -> 0 hubs; T* (3.86h) window -> 143 of 5,989}
    // {USER 2026-07-29 "i dont need this, i want how many hub left this round of incremental"}
    // [CONFIDENCE: CONFIRMED 100% — both counts computed side by side against live work_queue.]
    // `sched` is the sbAll ARRAY, not the row — the row is unwrapped further down as sched[0]. Reading .t_star_s off
    // the array yields undefined and would silently fall through to the 24h default, i.e. exactly the permanently-zero
    // display this change exists to remove, with no error to notice it by.
    const tStarH = sched?.[0]?.t_star_s ? sched[0].t_star_s / 3600 : 0;
    const staleWindowH = Number(process.env.TODAY_STALE_WINDOW_H) || tStarH || 24;
    const cycleAgo = now - staleWindowH * 3600 * 1000;
    const summarize = (t, staleBefore, windowH) => {
      const r = queue.filter((x) => x.type === t);
      const next = r.filter((x) => x.status === "queued")
        .sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at))   // soonest-due = what claim_work grabs next
        .slice(0, 5)
        .map((x) => ({ company: host[x.company_id] || hostOf(x.url), url: x.url, due_at: x.due_at }));
      // still-to-do this period = never scanned OR last scan older than one period ago (running counts as still-to-do)
      const remaining = r.filter((x) => !x.last_scanned_at || Date.parse(x.last_scanned_at) < staleBefore).length;
      return {
        total: r.length,
        queued: r.filter((x) => x.status === "queued").length,
        running: r.filter((x) => x.status === "running").length,
        failed: r.filter((x) => x.status === "failed").length,
        due_now: r.filter((x) => x.status === "queued" && Date.parse(x.due_at) <= now).length,   // due_now = coverage lag
        events_seen: r.reduce((s, x) => s + (x.last_event_count || 0), 0),
        remaining,                                                     // units NOT refreshed inside stale_window_h
        // Ship the window alongside the count so the UI labels it from data instead of a hardcoded string. Both are
        // env-tunable; a label that says "24h" while the constant says something else is the same class of drift that
        // made this number meaningless in the first place, so the number carries its own units.
        stale_window_h: windowH,
        // COVERAGE AGAINST THE ACTUAL CONTRACT, as a percentage. For full the contract is literally "every company gets
        // a deep pass inside one week", so this is the number the system is judged on and the only one worth showing
        // large. `remaining` above is the same fact as a count; this is it as a fraction, so it can be read without
        // knowing the denominator.
        coverage_pct: r.length ? Math.round((100 * (r.length - remaining)) / r.length) : null,
        next,                                                          // the 5 next-up units (company + url)
      };
    };

    // EVENTS feed — already ordered by created_at DESC (discovery time) from the query; DO NOT re-sort by event_date.
    // `date` = event_date is DISPLAY-ONLY. {USER 2026-07-26 "by when discovered; date just for visual, no sorting"}.
    const feed = (events || []).map((e) => ({
      id: e.id,
      company_id: e.company_id,
      company: host[e.company_id] || "—",
      date: e.event_date || "",           // display only — the row order is discovery-time, not this
      discovered: e.created_at || null,   // WHEN we discovered it on a run → the UI shows "1m ago / 2h ago / 3d ago"
      type: e.event_type || "",
      title: e.title || "",
      url: primaryUrl(e.media_urls),
    }));

    // SCHEDULER — the packing-solver state (T* = the dynamic incremental rotation, binding resource, live throughput,
    // full ETA). null when the pacer hasn't solved yet. {USER 2026-07-26 "show ... distributed per day request that adjust
    // dynamically based on an algorithm ... estimation of finish time"}.
    const s = (sched && sched[0]) || null;
    const scheduler = s ? {
      profile: s.profile,
      t_star_h: s.t_star_s != null ? +(s.t_star_s / 3600).toFixed(2) : null,   // incremental rotation period, hours
      binding: s.binding,                                                       // 'render' | 'vlm' — which resource sets T*
      c_r: s.c_r, c_v: s.c_v, hit_rate: s.hit_rate,                             // observed throughput + hash-change rate
      inc_hubs: s.inc_hubs, note: s.note, updated_at: s.updated_at,
    } : null;

    const resources = await liveResources();                 // live CPU (render) + VLM (GPU) usage right now
    res.json({ scheduler, resources, queue: { full: summarize("full", weekAgo, 168), incremental: summarize("incremental", cycleAgo, staleWindowH) }, events: feed });
  } catch (_e) {
    res.status(500).json({ error: "failed to load today" });   // never leak the raw PostgREST error (nestjs-conventions)
  }
}
