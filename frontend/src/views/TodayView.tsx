// Today page — TWO panels: (1) the live WORK QUEUE (full weekly-BFS + incremental deep=1 hub units, side by side) and
// (2) the newest events, descending by date. Polls /api/today every 30s so the queue + feed stay live as workers run.
// {USER 2026-07-25 "today page: current worker queue (full + deep=1 together) + new events descending by date"}.
import { useEffect, useState } from "react";

interface NextRow { company: string; url: string; due_at: string; }   // a next-up queued unit (company + url)
interface QStat { total: number; queued: number; running: number; failed: number; due_now: number; events_seen: number; remaining: number; next: NextRow[]; }
interface EvRow { id: string; company: string; date: string; discovered: string | null; type: string; title: string; url: string | null; }
interface Sched {
  profile: string; t_star_h: number | null; binding: string | null;
  c_r: number | null; c_v: number | null; hit_rate: number | null; eta_full_h: number | null;
  inc_hubs: number | null; note: string | null; updated_at: string | null;
}
interface Today { scheduler: Sched | null; queue: { full: QStat; incremental: QStat }; events: EvRow[]; }

// event_date is varied-granularity TEXT — Date.parse handles ISO + "Month DD, YYYY"; a quarter won't parse (shown raw).
function parseDate(s: string): number { const t = Date.parse((s || "").trim()); return isNaN(t) ? 0 : t; }
function fmtDate(s: string): string {
  if (!s) return "—";
  const t = parseDate(s);
  return t ? new Date(t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" }) : s;
}
// WHEN we discovered the event (created_at) as a relative "1m ago / 2h ago / 3d ago" — the freshness signal, since the
// feed is ordered by discovery time. {USER 2026-07-27 "a new col called when discovered, 1m ago 1 hour ago etc"}.
function relTime(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// Strip scheme + trailing slash so a queued url reads as a path, not a wall of "https://".
function shortUrl(u: string): string {
  return (u || "").replace(/^https?:\/\//, "").replace(/\/+$/, "") || "—";
}

// One queue card: a work type (full / incremental) with its live counts AND the next-5 units a worker will claim.
// `remainingLabel` phrases the still-to-do-this-period count ("to finish this week" / "left this cycle").
function QueueCard({ title, sub, s, remainingLabel }: { title: string; sub: string; s: QStat; remainingLabel: string }) {
  return (
    <div className="q-card">
      <div className="q-card-head"><span className="q-card-title">{title}</span><span className="q-card-sub">{sub}</span></div>
      <div className="q-card-big">{s.total}<span className="q-card-big-sub"> units</span></div>
      {/* still-to-do THIS PERIOD — how many haven't been scanned yet this week (full) / this cycle (incremental).
          {USER 2026-07-27 "full: this week we still have N not done; incremental: this cycle N urls not finished"}. */}
      <div style={{ fontSize: 13, margin: "2px 0 8px" }}>
        <b style={{ color: "#6ee7a8" }}>{s.remaining.toLocaleString()}</b> <span style={{ opacity: 0.7 }}>{remainingLabel}</span>
      </div>
      <div className="q-card-stats">
        <span className="q-stat"><b>{s.due_now}</b> due now</span>
        <span className="q-stat"><b>{s.running}</b> running</span>
        <span className="q-stat"><b>{s.queued}</b> queued</span>
        {s.failed > 0 && <span className="q-stat q-fail"><b>{s.failed}</b> failed</span>}
      </div>
      {/* NEXT UP — the 5 units a worker claims next (soonest-due first). {USER 2026-07-26 "show a list of next 5 urls or
          companies waiting to be done"}. */}
      {s.next && s.next.length > 0 && (
        <div style={{ marginTop: 12, borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: 10 }}>
          <div style={{ opacity: 0.5, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
            Next up
          </div>
          {s.next.map((n, i) => (
            <div key={i} style={{ display: "flex", gap: 8, fontSize: 12, padding: "3px 0", opacity: 0.85 }}>
              <span style={{ minWidth: 90, fontWeight: 600, flexShrink: 0 }}>{n.company}</span>
              <a className="lt-link" href={n.url} target="_blank" rel="noreferrer"
                 style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{shortUrl(n.url)}</a>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Humanize an hours value → "~10h" / "~45min" / "—". No jargon.
function every(h: number | null): string {
  if (h == null) return "—";
  return h < 1 ? `~${Math.round(h * 60)}min` : `~${Math.round(h)}h`;
}

// The SCHEDULER strip — PLAIN LANGUAGE, only what a human cares about: how often we re-check each watched page, how many
// we're watching, and how long a full deep re-crawl of everything takes. The internal knobs (T*, C_R/C_V, hit_rate,
// binding resource, profile) are DELIBERATELY hidden — nobody outside the engine cares what "C_V" is. {USER 2026-07-26
// "don't show the technicals, show what we care about — no one knows what c_v is"}.
function SchedulerBar({ s }: { s: Sched }) {
  const cell = (label: string, val: string, hint?: string) => (
    <div className="q-stat" style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 120 }}>
      <span style={{ opacity: 0.6, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</span>
      <b style={{ fontSize: 18 }}>{val}</b>
      {hint && <span style={{ opacity: 0.5, fontSize: 11 }}>{hint}</span>}
    </div>
  );
  return (
    <div className="q-card" style={{ marginBottom: 14 }}>
      <div className="q-card-head">
        <span className="q-card-title">Scheduler</span>
        <span className="q-card-sub">auto-paced — always checking, never idle</span>
      </div>
      <div className="q-card-stats" style={{ gap: 28, flexWrap: "wrap", alignItems: "flex-start" }}>
        {cell("Refresh cycle", every(s.t_star_h), "every watched page re-checked")}
        {cell("Watching", s.inc_hubs != null ? s.inc_hubs.toLocaleString() : "—", "pages monitored for new events")}
        {cell("Full re-crawl", every(s.eta_full_h), "deep pass over all companies")}
      </div>
      <div style={{ marginTop: 10, opacity: 0.6, fontSize: 12 }}>
        Re-checks every watched page about {every(s.t_star_h).replace("~", "every ")}, and runs deep re-crawls whenever
        there's spare capacity — so the machines are never sitting idle.
      </div>
    </div>
  );
}

export default function TodayView() {
  const [data, setData] = useState<Today | null>(null);
  const [loading, setLoading] = useState(true);
  // the "Page" viewer modal — same as EventsView: click a row's Page button → /api/page → show the VLM prompt + the
  // rendered page the model read. {USER 2026-07-27 "the raw page visualizer like the event page ... create a new col"}.
  const [src, setSrc] = useState<{ title: string; url: string | null; content: string | null; prompt: string | null; loading: boolean } | null>(null);
  function openPage(e: EvRow) {
    setSrc({ title: e.title || e.company, url: null, content: null, prompt: null, loading: true });
    fetch(`/api/page?event_id=${encodeURIComponent(e.id)}`).then((r) => r.json())
      .then((d) => setSrc({ title: e.title || e.company, url: d.url || null, content: d.content ?? null, prompt: d.system_prompt ?? null, loading: false }))
      .catch(() => setSrc({ title: e.title || e.company, url: null, content: null, prompt: null, loading: false }));
  }

  useEffect(() => {
    const load = () => fetch("/api/today").then((r) => r.json())
      .then((d) => { if (d && d.queue) { setData(d); setLoading(false); } })
      .catch(() => setLoading(false));
    load();
    const id = setInterval(load, 30000);   // live: refresh queue + feed every 30s
    return () => clearInterval(id);
  }, []);

  const q = data?.queue;
  // Keep the API order = created_at DESC = WHEN DISCOVERED on a run (newest-discovered first). DO NOT re-sort by
  // event_date — that column is display-only. {USER 2026-07-26 "by when discovered from the run; date just for visual"}.
  const events = data?.events || [];

  return (
    <div className="body">
      <div className="events-panel">
        <div className="body-full">
          {/* SCHEDULER strip — the packing solver's live T* decision (dynamic rotation keeping VLM+CPU busy) */}
          {data?.scheduler && <SchedulerBar s={data.scheduler} />}

          {/* PANEL 1 — the worker queue: full + incremental together */}
          <div className="section-label">Worker queue<span className="rule" /></div>
          {q ? (
            <div className="q-row">
              <QueueCard title="Full run" sub="weekly · deep BFS (20+ pages/co)" s={q.full}
                remainingLabel="companies still to crawl to finish this week" />
              <QueueCard title="Incremental"
                sub={data?.scheduler?.t_star_h != null ? `every ${data.scheduler.t_star_h}h · deep=1 (hub page)` : "deep=1 (hub page)"}
                s={q.incremental}
                remainingLabel="hubs still to scan this cycle" />
            </div>
          ) : loading ? <div className="loading">Loading queue…</div> : <div className="artifacts-empty">Queue empty — nothing enqueued yet.</div>}

          {/* PANEL 2 — newest events by DISCOVERY time (order = created_at desc); the Date column is display-only */}
          <div className="section-label" style={{ marginTop: 26 }}>New events — newest discovered<span className="rule" /></div>
          {loading ? (
            <div className="loading">Loading events…</div>
          ) : events.length === 0 ? (
            <div className="artifacts-empty">No events yet.</div>
          ) : (
            <table className="links-table">
              <thead><tr><th>Discovered</th><th>Date</th><th>Type</th><th>Title</th><th>Company</th><th>Page</th><th>Event URL</th></tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}>
                    <td className="lt-date" title={e.discovered || ""}>{relTime(e.discovered)}</td>
                    <td className="lt-date">{fmtDate(e.date)}</td>
                    <td><span className="chip">{e.type || "untyped"}</span></td>
                    <td className="lt-title">{e.title || "—"}</td>
                    <td className="lt-company">{e.company}</td>
                    <td><button className="src-btn" onClick={() => openPage(e)}>page</button></td>
                    <td>{e.url ? <a className="lt-link" href={e.url} target="_blank" rel="noreferrer">{e.url}</a> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* PAGE VIEWER modal — the VLM prompt (what we asked) + the rendered page (what the model read), per event. Same as
          EventsView, now on the Today feed too. {USER 2026-07-27 "raw page visualizer ... create a new col"}. */}
      {src && (
        <div className="src-overlay" onClick={() => setSrc(null)}>
          <div className="src-modal" onClick={(ev) => ev.stopPropagation()}>
            <div className="src-head">
              <div className="src-title">Source page — {src.title}</div>
              <button className="src-close" onClick={() => setSrc(null)}>✕</button>
            </div>
            {src.url && <a className="src-url" href={src.url} target="_blank" rel="noreferrer">{src.url}</a>}
            <div className="src-body">
              {src.loading ? "Loading…" : (
                <>
                  {src.prompt && (
                    <>
                      <div className="section-label" style={{ marginTop: 0, marginBottom: 6 }}>VLM prompt — what we asked<span className="rule" /></div>
                      <pre className="src-pre" style={{ opacity: 0.85, whiteSpace: "pre-wrap" }}>{src.prompt}</pre>
                    </>
                  )}
                  <div className="section-label" style={{ marginBottom: 6 }}>Page — what the model read<span className="rule" /></div>
                  {src.content ? <pre className="src-pre">{src.content}</pre>
                    : <div className="artifacts-empty">No source page stored for this event yet (the crawler writes it on the next scan).</div>}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
