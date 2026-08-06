// GET /api/today-media — the STAGE-2 (media_agent) dashboard: pipeline counts, the enrichment lanes' live state, and
// the most recent media RUNS (enriched + failed events, newest first) so a human can open one and see what came out.
//
// 用一句话讲完: 这个 endpoint 回答"富集管线现在到底在干什么" —— 队列里堆了多少、几个在飞、产出了多少块/文档/转写、
// 每个 url 的下场分布,以及 docling / whisper / render 的 fetch 车道各自的占用,最后带一串最近跑完的事件让人点开看。
// 它和 /api/today 是**按 stage 切开**的一对:/api/today 讲 stage-1(发现新事件),这个讲 stage-2(把事件富集成内容)。
//
// WHY split at all: the two stages have DIFFERENT bottlenecks and different failure modes, and mixing their status into
// one screen made neither legible. Stage-1 is paced by a solver against render+VLM; stage-2 is a drain-the-queue job
// bounded by document extraction. The render VM already reports the two lanes SEPARATELY because they were deliberately
// isolated into weighted tenants, so the split here mirrors a split that already exists in the running system:
// {RENDER /health 2026-08-05 "\"TENANTS\": {\"BROWSER\": {\"TOTAL\": 24.0, \"INFLIGHT\": {\"EVENT\": 4.0}},
//  \"FETCH\": {\"TOTAL\": 6.0, \"INFLIGHT\": {\"MEDIA\": 6.0}, \"WAITING\": {\"MEDIA\": 21}}}"}
// [CONFIDENCE: CONFIRMED — read off the live render service; browser=stage-1's lane, fetch=stage-2's lane.]
//
// {USER 2026-08-05 "let's keep two pages one is today events and one is today media, and sepraate the stauts display
//  there ... i want to autall inspect the medai runs"}
//
// 上游触发: MediaTodayView 每 30s 拉一次。下游连接: Supabase PostgREST(waterevents schema)+ 三个服务的 /health。
import { sb, sbAll } from "../lib/_db.js";

// Same service addresses /api/today uses, same env-override shape. Duplicated as constants rather than imported because
// today.js does not export them; keeping them literal here means this endpoint keeps working if today.js is refactored.
// {API/TODAY.JS:55 "CONST RENDERURL = PROCESS.ENV.RENDER_HEALTH_URL || \"HTTP://10.128.0.11:8100/HEALTH\""}
// [CONFIDENCE: CONFIRMED — copied from the live file; 10.128.0.11 is ir-render-16 on the internal network.]
const RENDER_URL  = process.env.RENDER_HEALTH_URL  || "http://10.128.0.11:8100/health";
const DOCLING_URL = process.env.DOCLING_HEALTH_URL || "http://127.0.0.1:8101/health";   // RunPod, via the tools tunnel
const WHISPER_URL = process.env.WHISPER_HEALTH_URL || "http://127.0.0.1:8102/health";   // RunPod, same tunnel

// PostgREST connection details, re-derived here for the ONE thing sb() cannot do: read a response HEADER.
// {LIB/_DB.JS:63 "EXPORT ASYNC FUNCTION SB(PATH) { ... RETURN R.JSON(); }"} — it returns the parsed body and drops the
// response object, so Content-Range never reaches the caller.
const REST_URL = process.env.SUPABASE_REST_URL || "https://vtwdantvlurtvymhhorr.supabase.co/rest/v1";
const REST_KEY = process.env.SUPABASE_PUBLISHABLE_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ0d2RhbnR2bHVydHZ5bWhob3JyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU3MzMzOTMsImV4cCI6MjEwMTMwOTM5M30.-ls5eNkxKHJSE_YBtfV_Zhi-qT2QKDPbYMHkhJ3JUOU";

/**
 * Row COUNT for a PostgREST filter without transferring the rows.
 *
 * WHY this exists instead of `(await sb(path)).length`: the events table is 273,936 rows and PostgREST caps a response
 * at 1000, so counting client-side is both wrong (silently truncated) and ruinous (this endpoint polls every 30s).
 * `Prefer: count=exact` + `Range: 0-0` makes Postgres do the counting and returns ZERO rows plus a Content-Range header
 * of the form "0-0/273936" — the count is the part after the slash.
 * {MEASURED 2026-08-05 psql "SELECT STATUS, COUNT(*) FROM WATEREVENTS.EVENTS GROUP BY STATUS" ->
 *  "DISCOVERED | 273936" "ENRICHED | 371" "FAILED | 7" "RENDERING | 3"}
 * The same mistake in the other direction is already recorded next door as a measured incident:
 * {API/MEDIA.JS "MEASURED 2026-07-28 SBALL(EVENTS ... NO LIMIT) -> ROWS=146758 IN 130.5S"} while that view polled
 * every 30s, so requests stacked faster than they completed.
 * [CONFIDENCE: CONFIRMED — row counts verified against psql on the production DB; the 130.5s incident is recorded
 *  in api/media.js by the author who measured it.]
 *
 * Returns null (never throws) when the count cannot be read, so one bad counter degrades ONE number on the dashboard
 * instead of failing the whole request.
 */
async function sbCount(path) {
  try {
    const r = await fetch(`${REST_URL}/${path}`, {
      headers: {
        apikey: REST_KEY,
        Authorization: `Bearer ${REST_KEY}`,
        "Accept-Profile": "waterevents",   // the artifact tables are NOT in `public` — without this every read 404s
        Prefer: "count=exact",             // ask Postgres for the true total, not the page length
        Range: "0-0",                      // ...and send none of the rows back
      },
      signal: AbortSignal.timeout(6000),
    });
    if (!r.ok && r.status !== 206) {       // 206 Partial Content is the NORMAL success code for a Range request
      console.error(`supabase count ${r.status} on ${path}`);   // server-side only — never surfaced to the client
      return null;
    }
    const cr = r.headers.get("content-range") || "";            // e.g. "0-0/273936", or "*/0" when the filter matches nothing
    const n = cr.split("/")[1];
    return n && n !== "*" ? parseInt(n, 10) : 0;
  } catch (_e) {
    return null;                                                // timeout / network — degrade this one number to null
  }
}

/**
 * Live state of the three services stage-2 depends on, reduced to the fields this page shows.
 *
 * WHY only these three (and not VLM): stage-2's work is fetch -> extract -> transcribe. The VLM lane belongs to stage-1
 * and is shown on the Today Events page; putting it here too would suggest media is blocked on the GPU when its real
 * constraint is document extraction. The render VM appears here ONLY as its `fetch` tenant, because that is the exact
 * slice of it stage-2 is entitled to under the weighted isolation.
 * All three fetches run concurrently under allSettled so one dead service cannot delay or fail the others — the same
 * discipline /api/today already uses. {API/TODAY.JS:75 "CONST [RENDERR, DOCLINGR, WHISPERR, VLMR] = AWAIT PROMISE.ALLSETTLED(["}
 * [CONFIDENCE: CONFIRMED — pattern copied from the working endpoint next door.]
 */
async function mediaResources() {
  const [renderR, doclingR, whisperR] = await Promise.allSettled([
    fetch(RENDER_URL,  { signal: AbortSignal.timeout(3000) }).then((r) => r.json()),
    fetch(DOCLING_URL, { signal: AbortSignal.timeout(3000) }).then((r) => r.json()),
    fetch(WHISPER_URL, { signal: AbortSignal.timeout(3000) }).then((r) => r.json()),
  ]);

  // ── FETCH LANE — stage-2's slice of the render VM ──────────────────────────────────────────────────────────────────
  // `waiting` is the number that matters and the one a plain "inflight/total" card would hide: when every slot is busy
  // AND a queue has formed behind it, media is throttled at the gate, not at the document extractor. The live reading
  // when this was written showed exactly that shape — 6 of 6 slots held with 21 more queued.
  // {RENDER /health 2026-08-05 "\"FETCH\": {\"TOTAL\": 6.0, \"INFLIGHT\": {\"MEDIA\": 6.0}, \"WAITING\": {\"MEDIA\": 21}}"}
  // [CONFIDENCE: CONFIRMED — verbatim from the running service.]
  let fetchLane = null;
  if (renderR.status === "fulfilled") {
    try {
      const t = renderR.value?.tenants || {};
      const f = t.fetch || {};
      fetchLane = {
        enabled:  t.enabled ?? null,
        total:    f.total ?? null,                       // slots the media tenant may hold right now
        inflight: (f.inflight || {}).media ?? 0,         // ...and how many it is actually holding
        waiting:  (f.waiting  || {}).media ?? 0,         // queue depth BEHIND the gate — the throttle signal
        cap_now:  (f.cap_now  || {}).media ?? null,      // work-conserving cap: rises when stage-1 is idle
        weights:  f.weights ?? null,                     // the configured share (currently event:5 / media:5 = 50/50)
      };
    } catch { fetchLane = null; }                        // malformed json → card shows "—" rather than a wrong number
  }

  // ── DOCLING — the document extractor, and stage-2's real ceiling ───────────────────────────────────────────────────
  // concurrency is surfaced beside inflight on purpose: inflight ABOVE concurrency means requests are queuing INSIDE
  // the service, which is invisible from the outside and was the state that made a working pipeline look dead.
  // {DOCLING /health 2026-08-05 "CONCURRENCY=4 INFLIGHT=26"} — 4 running, 22 queued, per-document latency 28-52 min.
  // [CONFIDENCE: CONFIRMED — read off the live service while diagnosing the stall.]
  let docling = null;
  if (doclingR.status === "fulfilled") {
    try {
      const d = doclingR.value, h = d.host || {};
      docling = {
        concurrency: d.concurrency ?? null,
        inflight:    d.inflight    ?? null,
        queued:      d.inflight != null && d.concurrency != null ? Math.max(0, d.inflight - d.concurrency) : null,
        done:        d.docling     ?? null,
        errors:      d.errors      ?? null,
        cores:       h.cores       ?? null,
        load:        h.load1       ?? null,
        pct:         h.load_pct    ?? null,
      };
    } catch { docling = null; }
  }

  // ── WHISPER — audio transcription, GPU, shares the A40 with vLLM ───────────────────────────────────────────────────
  let whisper = null;
  if (whisperR.status === "fulfilled") {
    try {
      const d = whisperR.value, g = d.gpu || null;
      whisper = {
        concurrency: d.concurrency ?? null,
        inflight:    d.inflight    ?? null,
        done:        d.whisper     ?? d.transcribe ?? null,
        errors:      d.errors      ?? null,
        gpu_mem_used_mb:  g?.mem_used_mb  ?? null,
        gpu_mem_total_mb: g?.mem_total_mb ?? null,
        gpu_util_pct:     g?.util_pct     ?? null,
      };
    } catch { whisper = null; }
  }

  return { fetch_lane: fetchLane, docling, whisper };
}

export default async function handler(_req, res) {
  try {
    const hourAgo = new Date(Date.now() - 3600 * 1000).toISOString();

    // Every count is one HEAD-shaped request; they all fire together. Counting in parallel matters because this handler
    // is polled every 30s and a serial chain of a dozen round-trips to us-west-2 would not fit inside that budget.
    const [
      backlog, inflight, enriched, failed,
      titleFixed, dateFixed,
      blocks, files, segments, audio,
      ledgerDone, ledgerFailed, ledgerSkipped,
      enriched1h, blocks1h,
      resources, recent,
    ] = await Promise.all([
      sbCount("events?select=id&status=eq.discovered"),
      sbCount("events?select=id&status=eq.rendering"),          // claimed and being worked right now
      sbCount("events?select=id&status=eq.enriched"),
      sbCount("events?select=id&status=eq.failed"),
      // WHAT THE METADATA TASK ACTUALLY REPAIRED. A key is present in meta_fixed only when that field was replaced,
      // and its value is what it was replaced FROM — so these two counts are the task's real output rather than "how
      // many events we called the model on". Until 2026-08-06 the answer was structurally zero: the writer's UPDATE
      // never named title/date/event_type, so every correction the model returned was discarded.
      // {MIGRATION 20260806095419 "EVENTS.META_FIXED — STAGE-2 METADATA REPAIRS: {\"TITLE|DATE|TYPE\": \"<VALUE BEFORE>\"}"}
      // [CONFIDENCE: CONFIRMED 100% — the discarding UPDATE was read from db_media.py before this column existed.]
      sbCount("events?select=id&meta_fixed->>title=not.is.null"),
      sbCount("events?select=id&meta_fixed->>date=not.is.null"),
      // Documents, split by source kind. "blocks" kept its name on the wire because the dashboard label is decided
      // in the UI; what it counts is now html DOCUMENTS, not paragraph fragments.
      // {MIGRATION 20260805151246 "EVENT_DOCUMENTS — ONE ROW PER (EVENT, SOURCE URL)"}
      // [CONFIDENCE: CONFIRMED 100% — schema read back from psql after the migration applied.]
      sbCount("event_documents?select=id&kind=eq.html"),
      sbCount("event_documents?select=id&kind=neq.html"),
      sbCount("event_transcript_segments?select=id"),
      sbCount("event_audio?select=id"),
      // The url ledger tallied by outcome. This is the highest-signal number on the page: it is the ONLY place a
      // per-url failure is recorded, and its shape immediately names which extractor is broken.
      // {MEASURED 2026-08-05 over 688 ledger rows "HTML DONE 450 | PDF DONE 80 | HTML FAILED 60 | PDF FAILED 38 |
      //  VIDEO SKIPPED 30 | XLSX FAILED 28 | OTHER SKIPPED 5 | VIDEO FAILED 1 | XLSX DONE 1"} — xlsx at 28 failed
      // against 1 done is a broken path stating itself plainly.
      // [CONFIDENCE: CONFIRMED — tallied from the live table.]
      sbCount("event_media_urls?select=id&status=eq.done"),
      sbCount("event_media_urls?select=id&status=eq.failed"),
      sbCount("event_media_urls?select=id&status=eq.skipped"),
      sbCount(`events?select=id&enriched_at=gte.${hourAgo}`),   // throughput, measured on the WRITE timestamp
      sbCount(`event_documents?select=id&created_at=gte.${hourAgo}`),
      mediaResources(),
      // The RUNS themselves — what the user opens to inspect. Ordered by enriched_at desc so the newest completed run
      // is first. basic_info is deliberately NOT selected: it is the heaviest column and the row only needs to say
      // whether a run happened, not carry its whole output. The artifact inventory arrives separately via /api/artifacts.
      sb("events?select=id,company_id,title,event_date,event_type,status,enriched_at,media_urls,fail_reason"
         + "&status=in.(enriched,failed)&order=enriched_at.desc.nullslast&limit=150"),
    ]);

    // company_id -> ticker, so a run is identifiable without a second lookup per row. The companies table is ~2,785 rows
    // and this is the same join /api/today already does. {API/TODAY.JS:199 "CONST HOST = OBJECT.FROMENTRIES(...)"}
    // sbAll, not sb: PostgREST caps a response at 1000 rows REGARDLESS of the limit clause, and there are 2,785
    // companies — so `sb(... limit=5000)` silently returned 1,000 and 64% of runs rendered their company as "—".
    // {MEASURED 2026-08-06 REST "companies?select=id&limit=5000" -> 1000 rows}
    // {MEASURED 2026-08-06 REST "companies?select=id" WITH Prefer:count=exact -> "content-range: 0-0/2785"}
    // The endpoint next door already knew this: {API/TODAY.JS:194 "SBALL(\"COMPANIES?SELECT=ID,IR_URL,TICKER\")"}.
    // [CONFIDENCE: CONFIRMED 100% — both figures curl'd against the live REST endpoint.]
    const companies = await sbAll("companies?select=id,ticker,ir_url");
    const label = Object.fromEntries((companies || []).map((c) => [c.id, c.ticker || hostOf(c.ir_url)]));

    const runs = (recent || []).map((e) => ({
      id: e.id,
      company: label[e.company_id] || "—",
      title: e.title || "",
      date: e.event_date || "",
      type: e.event_type || "",
      status: e.status,                                  // 'enriched' | 'failed' — the run's outcome
      enriched_at: e.enriched_at || null,
      fail_reason: e.fail_reason || null,                // WHY it failed, straight from the worker
      n_urls: Array.isArray(e.media_urls) ? e.media_urls.length : 0,
      url: primaryUrl(e.media_urls),
    }));

    res.json({
      pipeline: {
        backlog, inflight, enriched, failed,
        // `blocks` counts html documents = the pages whose prose we extracted; `files` counts office documents = the
        // docling lane. Naming them by what produced them beats naming them by table.
        meta: { title_fixed: titleFixed, date_fixed: dateFixed },
        totals: { blocks, files, segments, audio },
        ledger: { done: ledgerDone, failed: ledgerFailed, skipped: ledgerSkipped },
        recent: { enriched_1h: enriched1h, blocks_1h: blocks1h },
      },
      resources,
      runs,
    });
  } catch (_e) {
    // Never leak the raw PostgREST error — it names tables and constraints.
    // {~/.claude/rules/nestjs-conventions.md "NEVER THROW RAW SUPABASE/DB ERRORS — THEY LEAK INTERNAL SCHEMA DETAILS"}
    res.status(500).json({ error: "failed to load media status" });
  }
}

// Host of a url, minus a leading "www." — the fallback company label when a company has no ticker.
function hostOf(u) { try { return new URL(u).host.replace(/^www\./, ""); } catch { return u || "—"; } }

// The event's primary link: the first http url in its media list. Same helper /api/today uses for the feed rows.
function primaryUrl(mediaUrls) {
  const a = Array.isArray(mediaUrls) ? mediaUrls : [];
  return a.find((u) => typeof u === "string" && u.startsWith("http")) || null;
}
