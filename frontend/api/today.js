// GET /api/today — the "Today" dashboard: (1) the live WORK QUEUE state (full weekly-BFS units + incremental deep=1
// hub units, shown together) and (2) the newest events, descending by date. Reads waterevents.work_queue + .events +
// .companies live from Supabase. {USER 2026-07-25 "today page: current worker queue (full runs + deep=1 together) +
// new events descending by date"}.
import { sbAll } from "../lib/_db.js";
import os from "node:os";

// LIVE resource usage — the two bottlenecks, right now. CPU (render, on THIS host: loadavg/cores) and VLM (the GPU, via
// the vLLM Prometheus /metrics through the SSH tunnel at :8000). {USER 2026-07-27 "add two cards: cpu bottleneck current
// usage + vlm usage current, like the parallel current running"}.
async function liveResources() {
  const cores = os.cpus().length;
  const load1 = os.loadavg()[0];                              // 1-min load average of the render host
  const cpu = { cores, load: +load1.toFixed(2), pct: Math.round((load1 / cores) * 100) };
  let vlm = null;
  try {                                                       // vLLM /metrics is unauth'd; :8000 is the RunPod tunnel
    const txt = await (await fetch("http://127.0.0.1:8000/metrics", { signal: AbortSignal.timeout(3000) })).text();
    const g = (k) => { const m = txt.match(new RegExp(k + "\\{[^}]*\\}\\s+([\\d.eE+-]+)")); return m ? +m[1] : null; };
    const kv = g("vllm:kv_cache_usage_perc") ?? g("vllm:gpu_cache_usage_perc");
    vlm = { running: g("vllm:num_requests_running"), waiting: g("vllm:num_requests_waiting"),
            kv_pct: kv == null ? null : Math.round(kv * 100) };
  } catch { vlm = null; }                                     // tunnel down / not self-hosted → card shows "—"
  return { cpu, vlm };
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
      sbAll("scheduler_state?select=profile,t_star_s,binding,c_r,c_v,hit_rate,eta_full_h,inc_hubs,note,updated_at&id=eq.1"),
    ]);

    // company label map (id → ticker, fallback IR host) — used by both the queue next-up list and the events feed.
    const host = Object.fromEntries((companies || []).map((c) => [c.id, c.ticker || hostOf(c.ir_url)]));

    // QUEUE — summarize each type (full = weekly deep BFS; incremental = deep=1 hub scan), together. Plus `next` = the
    // NEXT-UP items a worker will claim: queued rows in claim order (soonest due_at first), top 5, each with its company
    // label + url so you can SEE what's about to run, not just how many. {USER 2026-07-26 "show a list of next 5 urls or
    // companies waiting to be done"}.
    const now = Date.now();
    // period windows for the "still-to-do this period" counts: full = a 7-day weekly sweep; incremental = one T* rotation
    // (the pacer's current period, fallback 30min). A unit is "done this period" iff it was last_scanned within the window;
    // else it still needs a scan to finish the period. {USER 2026-07-27 "full: this week we still have N not done; incremental:
    // this cycle we still have N urls not finished"}.
    const weekAgo = now - 7 * 24 * 3600 * 1000;
    const tStarSec = (sched && sched[0] && sched[0].t_star_s) || 1800;
    const cycleAgo = now - tStarSec * 1000;
    const summarize = (t, staleBefore) => {
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
        remaining,                                                     // still to scan to finish this period (week / cycle)
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
      eta_full_h: s.eta_full_h, inc_hubs: s.inc_hubs, note: s.note, updated_at: s.updated_at,
    } : null;

    const resources = await liveResources();                 // live CPU (render) + VLM (GPU) usage right now
    res.json({ scheduler, resources, queue: { full: summarize("full", weekAgo), incremental: summarize("incremental", cycleAgo) }, events: feed });
  } catch (_e) {
    res.status(500).json({ error: "failed to load today" });   // never leak the raw PostgREST error (nestjs-conventions)
  }
}
