// Today · Media — the STAGE-2 page. Everything on it answers "what is the enrichment pipeline doing, and what did it
// produce?", the way the Today page answers the same question for stage-1 discovery.
//
// 用一句话讲完: 顶上一条队列条(积压 / 在飞 / 已富集 / 失败 + 最近一小时的产出速率)→ 三张资源卡(render VM 的 fetch
// 车道、docling、whisper,正好是 stage-2 依赖的三样)→ 一条 url 台账的成败分布 → 最后一张"最近跑完的事件"表,每行带
// artifact 按钮,点开就能看这次跑出来的 md / pdf / 转写 / 台账。
//
// WHY a separate page instead of more cards on Today: the two stages have different bottlenecks and different failure
// modes, and one screen showing both made neither legible. The render VM already reports the two lanes SEPARATELY
// because they were deliberately isolated into weighted tenants, so this split mirrors one the running system already
// enforces — stage-1 holds the `browser` lane, stage-2 holds `fetch`.
// {RENDER /health 2026-08-05 "\"FETCH\": {\"TOTAL\": 6.0, \"INFLIGHT\": {\"MEDIA\": 6.0}, \"WAITING\": {\"MEDIA\": 21}}"}
// [CONFIDENCE: CONFIRMED 100% — read off the live render service; the tenant split predates this page.]
//
// {USER 2026-08-05 "let's keep two pages one is today events and one is today media, and sepraate the stauts display
//  there, so and deploy eveyrthing to qewb app first, i want to autall inspect the medai runs"}
//
// 上游触发: nav 里的 Media 项。下游连接: /api/today-media(队列+资源+runs)、/api/artifacts(按钮清单)、
// /api/artifact(弹窗内容)。
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import ArtifactModal from "../components/ArtifactModal";
import { ArtifactButtons, useArtifactInventory } from "../components/artifacts";
import type { ArtInvMap } from "../components/artifacts";

// ── Shapes returned by /api/today-media ──────────────────────────────────────────────────────────────────────────────
interface FetchLane { enabled: boolean | null; total: number | null; inflight: number; waiting: number; cap_now: number | null; weights: Record<string, number> | null; }
interface Docling { concurrency: number | null; inflight: number | null; queued: number | null; done: number | null; errors: number | null; cores: number | null; load: number | null; pct: number | null; }
interface Whisper { concurrency: number | null; inflight: number | null; done: number | null; errors: number | null; gpu_mem_used_mb: number | null; gpu_mem_total_mb: number | null; gpu_util_pct: number | null; }
interface Pipeline {
  backlog: number | null; inflight: number | null; enriched: number | null; failed: number | null;
  totals: { blocks: number | null; files: number | null; segments: number | null; audio: number | null };
  meta: { title_fixed: number | null; date_fixed: number | null };
  ledger: { done: number | null; failed: number | null; skipped: number | null };
  recent: { enriched_1h: number | null; blocks_1h: number | null };
}
interface RunRow {
  id: string; company: string; title: string; date: string; type: string;
  status: string;                 // 'enriched' | 'failed' — the run's outcome
  enriched_at: string | null;
  fail_reason: string | null;
  n_urls: number;
  url: string | null;
}
interface MediaToday { pipeline: Pipeline; resources: { fetch_lane: FetchLane | null; docling: Docling | null; whisper: Whisper | null }; runs: RunRow[]; }

// ── Small formatters, matching the Today page's vocabulary ───────────────────────────────────────────────────────────
function num(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString();
}
function fmtDate(s: string): string {
  if (!s) return "—";
  const t = Date.parse((s || "").trim());
  return isNaN(t) ? s : new Date(t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}
function relTime(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return "—";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/**
 * The queue strip — where the 273k-event backlog stands right now.
 *
 * WHY backlog gets the big number: stage-2 is a drain-the-queue job, not a steady-state service, so the one thing an
 * operator needs at a glance is how much is left and whether it is moving. The per-hour figures beside it are what
 * turn a static count into a rate; without them a large backlog is unreadable (is it stuck or just big?).
 * {MEASURED 2026-08-05 psql "SELECT STATUS, COUNT(*) FROM WATEREVENTS.EVENTS GROUP BY STATUS" ->
 *  "DISCOVERED | 273859" "ENRICHED | 393" "RENDERING | 61" "FAILED | 23"}
 * [CONFIDENCE: CONFIRMED 100% — live counts at the time this page was written.]
 */
function PipelineBar({ p }: { p: Pipeline }) {
  const cell = (label: string, value: ReactNode, sub: string) => (
    <div style={{ flex: 1, minWidth: 128 }}>
      <div className="q-card-sub" style={{ textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</div>
      <div className="q-card-big" style={{ fontSize: 26 }}>{value}</div>
      <div className="q-stat">{sub}</div>
    </div>
  );
  return (
    <div className="q-card" style={{ marginBottom: 14 }}>
      <div className="q-card-head">
        <span className="q-card-title">Enrichment queue</span>
        <span className="q-card-sub">stage-2 · media_agent</span>
      </div>
      <div style={{ display: "flex", gap: 18, flexWrap: "wrap", marginTop: 6 }}>
        {cell("Backlog", num(p.backlog), "events waiting to be enriched")}
        {/* The three VLM tasks, each shown by what it PRODUCED rather than by how often it ran. A count of calls says
            nothing about whether the call was worth making; a count of repairs and extractions does. */}
        {cell("Titles fixed", num(p.meta.title_fixed), "stage-1 title replaced by the page's own")}
        {cell("Dates fixed", num(p.meta.date_fixed), "stage-1 date replaced by the page's own")}
        {cell("Basic info", num(p.totals.blocks), "pages whose prose was extracted")}
        {cell("Docling", num(p.totals.files), "pdf / xlsx / pptx / docx parsed")}
        {cell("In flight", num(p.inflight), "claimed by a worker right now")}
      </div>
    </div>
  );
}

/**
 * The three lanes stage-2 actually runs through, each reduced to its saturation signal.
 *
 * WHY these three and not the VLM: media's path is fetch -> extract -> transcribe. The VLM lane belongs to stage-1 and
 * lives on the Today page; showing it here would suggest media is blocked on the GPU when its real constraint is
 * document extraction.
 *
 * WHY `waiting` is printed on the fetch card: an "inflight/total" reading alone cannot distinguish "comfortably busy"
 * from "throttled with a queue behind the gate". The live reading when this was written was the second one — every
 * slot held, 21 more waiting. {RENDER /health 2026-08-05 "\"FETCH\": {\"TOTAL\": 6.0, \"INFLIGHT\": {\"MEDIA\": 6.0},
 * \"WAITING\": {\"MEDIA\": 21}}"} [CONFIDENCE: CONFIRMED 100% — verbatim from the running service.]
 *
 * WHY docling prints `queued` next to concurrency: requests queue INSIDE the service, which is invisible from outside
 * and was exactly the state that made a working pipeline look dead — 4 slots running, 22 queued, and each document
 * reporting 28-52 minutes because that timer starts when the request is RECEIVED, not when it starts processing.
 * {DOCLING /health 2026-08-05 "CONCURRENCY=4 INFLIGHT=26"}
 * [CONFIDENCE: CONFIRMED 100% — read off the live service while diagnosing a stall that showed zero output for 30
 *  consecutive minutes with every health check returning 200.]
 */
function MediaResourceCards({ r }: { r: MediaToday["resources"] }) {
  const card = (title: string, sub: string, big: ReactNode, hot: boolean, lines: string[]) => (
    <div className="q-card" style={{ flex: 1 }}>
      <div className="q-card-head"><span className="q-card-title">{title}</span><span className="q-card-sub">{sub}</span></div>
      <div className="q-card-big" style={{ color: hot ? "#f87171" : undefined }}>{big}</div>
      <div className="q-card-stats" style={{ flexWrap: "wrap" }}>
        {lines.map((l, i) => <span key={i} className="q-stat">{l}</span>)}
      </div>
    </div>
  );

  const f = r.fetch_lane;
  // A queue behind the gate is the throttle signal — mark it, but only when work is actually waiting.
  const fetchHot = (f?.waiting ?? 0) > 0;
  const d = r.docling;
  const doclingHot = (d?.queued ?? 0) > 0;
  const w = r.whisper;
  const whisperHot = (w?.errors ?? 0) > 0;

  return (
    <div className="q-row" style={{ marginBottom: 14 }}>
      {card("Fetch lane · render VM", "media's share of the render VM",
        f ? <>{f.inflight}<span className="q-card-big-sub">/{f.total ?? "—"} slots</span></> : "—",
        fetchHot,
        f ? [
          `${f.waiting} waiting`,
          f.cap_now != null ? `cap now ${f.cap_now}` : "cap —",
          // The weights are the configured share between the two stages; printing them makes the isolation legible
          // rather than something you have to take on faith.
          f.weights ? `share ${Object.entries(f.weights).map(([k, v]) => `${k}:${v}`).join(" · ")}` : "share —",
        ] : ["render VM unreachable"])}

      {card("Docling · documents", "RunPod CPU",
        d ? <>{d.inflight ?? "—"}<span className="q-card-big-sub">/{d.concurrency ?? "—"} slots</span></> : "—",
        doclingHot,
        d ? [
          `${d.queued ?? 0} queued`,
          `${num(d.done)} done · ${num(d.errors)} errors`,
          d.pct != null ? `${d.pct}% cpu · ${d.cores ?? "—"} cores` : "cpu —",
        ] : ["docling unreachable"])}

      {card("Whisper · audio", "RunPod GPU (shared with VLM)",
        w ? <>{w.inflight ?? "—"}<span className="q-card-big-sub">/{w.concurrency ?? "—"} slots</span></> : "—",
        whisperHot,
        w ? [
          `${num(w.done)} done · ${num(w.errors)} errors`,
          w.gpu_util_pct != null ? `${w.gpu_util_pct}% gpu util` : "gpu util —",
          w.gpu_mem_used_mb != null ? `${w.gpu_mem_used_mb}/${w.gpu_mem_total_mb} MB` : "gpu mem —",
        ] : ["whisper unreachable"])}
    </div>
  );
}

/**
 * The url ledger tallied by outcome — the highest-signal number on this page.
 *
 * WHY it gets its own strip rather than a column: event_media_urls is the ONLY place a per-url failure is recorded, so
 * its shape names which extractor is broken without anyone having to open a single event. The reading that motivated
 * this display: {MEASURED 2026-08-05 over 688 ledger rows "HTML DONE 450 | PDF DONE 80 | HTML FAILED 60 |
 * PDF FAILED 38 | VIDEO SKIPPED 30 | XLSX FAILED 28 | OTHER SKIPPED 5 | VIDEO FAILED 1 | XLSX DONE 1"} — xlsx at
 * 28 failed against 1 done is a broken path stating itself plainly, and it was found by reading this table by hand.
 * [CONFIDENCE: CONFIRMED 100% — tallied from the live table before this component existed.]
 */
function LedgerBar({ l }: { l: Pipeline["ledger"] }) {
  const total = (l.done ?? 0) + (l.failed ?? 0) + (l.skipped ?? 0);
  const pct = (n: number | null) => (total > 0 ? Math.round(((n ?? 0) / total) * 100) : 0);
  return (
    <div className="q-card" style={{ marginBottom: 18 }}>
      <div className="q-card-head">
        <span className="q-card-title">URL ledger</span>
        <span className="q-card-sub">every media url's outcome · {num(total)} total</span>
      </div>
      <div className="q-card-stats" style={{ marginTop: 8, gap: 18 }}>
        <span className="q-stat">done {num(l.done)} ({pct(l.done)}%)</span>
        <span className="q-stat" style={{ color: (l.failed ?? 0) > 0 ? "#f87171" : undefined }}>
          failed {num(l.failed)} ({pct(l.failed)}%)
        </span>
        <span className="q-stat">skipped {num(l.skipped)} ({pct(l.skipped)}%)</span>
      </div>
    </div>
  );
}

export default function MediaTodayView() {
  const [data, setData] = useState<MediaToday | null>(null);
  const [loading, setLoading] = useState(true);
  // Which run + artifact kind the user clicked. null = no modal open.
  const [artModal, setArtModal] = useState<{ eventId: string; kind: string; title: string } | null>(null);

  useEffect(() => {
    const load = () => fetch("/api/today-media").then((r) => r.json())
      .then((d) => { if (d && d.pipeline) { setData(d); setLoading(false); } })
      .catch(() => setLoading(false));
    load();
    const id = setInterval(load, 30000);          // same 30s cadence as the Today page
    return () => clearInterval(id);
  }, []);

  const runs = data?.runs || [];
  // Artifact inventory for exactly the runs on screen — the same shared loader the Today page uses.
  const artInv: ArtInvMap = useArtifactInventory(runs.map((r) => r.id));

  return (
    <div className="body">
      <div className="events-panel">
        <div className="body-full">
          {data?.pipeline && <PipelineBar p={data.pipeline} />}
          {data?.resources && <MediaResourceCards r={data.resources} />}
          {data?.pipeline && <LedgerBar l={data.pipeline.ledger} />}

          <div className="section-label">Media runs — newest first<span className="rule" /></div>
          {loading ? (
            <div className="loading">Loading media runs…</div>
          ) : runs.length === 0 ? (
            <div className="artifacts-empty">No completed media runs yet.</div>
          ) : (
            <table className="links-table">
              <thead>
                <tr>
                  <th>Finished</th><th>Date</th><th>Type</th><th>Title</th><th>Company</th>
                  <th>Outcome</th><th>Artifacts</th><th>Event URL</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td className="lt-date" title={r.enriched_at || ""}>{relTime(r.enriched_at)}</td>
                    <td className="lt-date">{fmtDate(r.date)}</td>
                    <td><span className="chip">{r.type || "untyped"}</span></td>
                    <td className="lt-title">{r.title || "—"}</td>
                    <td className="lt-company">{r.company}</td>
                    <td>
                      {/* A failed run carries its reason from the worker; surfacing it in the title attribute means the
                          cause is one hover away instead of a journalctl session. */}
                      <span className={`chip${r.status === "failed" ? " bad" : ""}`}
                            title={r.fail_reason || `${r.n_urls} urls`}>
                        {r.status === "failed" ? (r.fail_reason || "failed") : `${r.n_urls} urls`}
                      </span>
                    </td>
                    <td>
                      <ArtifactButtons
                        inv={artInv[r.id]}
                        onOpen={(kind) => setArtModal({ eventId: r.id, kind, title: r.title || r.company })} />
                    </td>
                    <td>{r.url ? <a className="lt-link" href={r.url} target="_blank" rel="noreferrer">{r.url}</a> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {artModal && (
        <ArtifactModal eventId={artModal.eventId} kind={artModal.kind} title={artModal.title}
                       onClose={() => setArtModal(null)} />
      )}
    </div>
  );
}
