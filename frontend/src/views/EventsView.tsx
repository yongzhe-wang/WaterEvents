// Events page — company RAIL (left, the "second sidebar") + a SEGMENT-LOADED events table (right). The rail loads from
// /api/companies (one cheap aggregate → instant, no waiting on the full events table). The table loads ONE PAGE at a time
// from /api/events?company_id=&limit=&offset= — selecting a company loads only that company's events, and "Load more"
// pages further. Each event has a "page" button → the original rendered content the LLM extracted it from (/api/page).
// {USER 2026-07-24 "add a second sidebar per company" + "show the ticker" + "web app takes too long to load, do segment
// loading" + "each event have a button that shows the original page content"}.
import { useEffect, useMemo, useRef, useState } from "react";

interface Company { id: string; label: string; event_count: number; }
interface EventRow {
  id: string;
  company_id: string;      // mapped to the ticker label via the companies rail
  date: string;            // event_date as text (YYYY / YYYY-MM / YYYY-Q1 / YYYY-MM-DD)
  type: string;
  title: string;
  url: string | null;      // primary media url (detail page)
  media_count: number;
  status: string;
}
const PAGE = 300;          // events per segment fetch

// event_date is varied-granularity TEXT — Date.parse handles ISO + "Month DD, YYYY"; a quarter won't parse (shown raw).
function parseDate(s: string): number { const t = Date.parse((s || "").trim()); return isNaN(t) ? 0 : t; }
function fmtDate(s: string): string {
  if (!s) return "—";
  const t = parseDate(s);
  return t ? new Date(t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" }) : s;
}
function initials(label: string): string {
  const a = (label || "").replace(/[^a-z0-9]/gi, "");
  return (a.slice(0, 2) || "•").toUpperCase();
}

export default function EventsView() {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [company, setCompany] = useState("all");     // "all" or a company id
  const [q, setQ] = useState("");                    // rail search box
  const [typeFilter, setTypeFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [src, setSrc] = useState<{ id: string; title: string; url: string | null; content: string | null; prompt: string | null; loading: boolean } | null>(null);
  const reqId = useRef(0);                            // guards against a slow older fetch overwriting a newer selection

  // Rail: load the company aggregate once + refresh every 30s (cheap, so the rail stays live as the crawl writes).
  useEffect(() => {
    const load = () => fetch("/api/companies").then((r) => r.json()).then((d) => setCompanies(Array.isArray(d) ? d : [])).catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  // Load ONE page of events for the current company selection (append=false resets, true pages further).
  function loadEvents(companyId: string, offset: number, append: boolean) {
    const mine = ++reqId.current;                     // only the latest request may commit its result
    (append ? setLoadingMore : setLoading)(true);
    const cq = companyId === "all" ? "" : `company_id=${encodeURIComponent(companyId)}&`;
    fetch(`/api/events?${cq}limit=${PAGE}&offset=${offset}`).then((r) => r.json()).then((d) => {
      if (mine !== reqId.current) return;             // a newer selection superseded this fetch → drop it
      const arr: EventRow[] = Array.isArray(d) ? d : [];
      setEvents((prev) => (append ? [...prev, ...arr] : arr));
      setHasMore(arr.length === PAGE);
      (append ? setLoadingMore : setLoading)(false);
    }).catch(() => { if (mine === reqId.current) (append ? setLoadingMore : setLoading)(false); });
  }
  useEffect(() => { loadEvents(company, 0, false); setTypeFilter("all"); }, [company]);   // company change → fresh first page

  const labelById = useMemo(() => Object.fromEntries(companies.map((c) => [c.id, c.label])), [companies]);
  const totalEvents = useMemo(() => companies.reduce((s, c) => s + (c.event_count || 0), 0), [companies]);
  const railList = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? companies.filter((c) => c.label.toLowerCase().includes(needle)) : companies;
  }, [companies, q]);

  // Type chips + rows from the LOADED segment (sorted newest-first; the server already ordered, but resort defensively).
  const sorted = useMemo(() => events.slice().sort((a, b) => parseDate(b.date) - parseDate(a.date)), [events]);
  const typeCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of sorted) { const t = e.type || "untyped"; m.set(t, (m.get(t) || 0) + 1); }
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [sorted]);
  const activeType = typeCounts.some(([t]) => t === typeFilter) ? typeFilter : "all";
  const shown = activeType === "all" ? sorted : sorted.filter((e) => (e.type || "untyped") === activeType);
  const selectedLabel = company === "all" ? "Events" : (labelById[company] || "Events");

  function openSource(e: EventRow) {
    setSrc({ id: e.id, title: e.title || (labelById[e.company_id] || ""), url: null, content: null, prompt: null, loading: true });
    fetch(`/api/page?event_id=${encodeURIComponent(e.id)}`).then((r) => r.json())
      .then((d) => setSrc({ id: e.id, title: e.title || (labelById[e.company_id] || ""), url: d.url || null, content: d.content ?? null, prompt: d.system_prompt ?? null, loading: false }))
      .catch(() => setSrc({ id: e.id, title: e.title || "", url: null, content: null, prompt: null, loading: false }));
  }

  return (
    <div className="body">
      {/* SECOND SIDEBAR — the company rail (loads from the cheap aggregate, so it's instant) */}
      <aside className="rail">
        <div className="rail-head">
          <input className="search" placeholder="Filter companies…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="rail-count">{companies.length} companies · {totalEvents} events</div>
        <div className="rail-list">
          <div className={`crow${company === "all" ? " active" : ""}`} onClick={() => setCompany("all")}>
            <div className="avatar">∀</div>
            <div className="crow-main"><div className="crow-name">All companies</div><div className="crow-sub">every company</div></div>
            <span className="crow-count">{totalEvents}</span>
          </div>
          {railList.map((c) => (
            <div key={c.id} className={`crow${company === c.id ? " active" : ""}`} onClick={() => setCompany(c.id)}>
              <div className="avatar">{initials(c.label)}</div>
              <div className="crow-main"><div className="crow-name">{c.label}</div><div className="crow-sub">{c.event_count} event{c.event_count !== 1 ? "s" : ""}</div></div>
              <span className="crow-count">{c.event_count}</span>
            </div>
          ))}
        </div>
      </aside>

      {/* RIGHT — the segment-loaded events table */}
      <div className="events-panel">
        <div className="body-full">
          <div className="section-label">{selectedLabel} ({shown.length}{hasMore ? "+" : ""})<span className="rule" /></div>

          {typeCounts.length > 1 && (
            <div className="type-filter">
              <button className={`type-chip${activeType === "all" ? " on" : ""}`} onClick={() => setTypeFilter("all")}>
                All <span className="tc-n">{sorted.length}</span>
              </button>
              {typeCounts.map(([t, n]) => (
                <button key={t} className={`type-chip${activeType === t ? " on" : ""}`} onClick={() => setTypeFilter(t)}>
                  {t} <span className="tc-n">{n}</span>
                </button>
              ))}
            </div>
          )}

          {loading ? (
            <div className="loading">Loading events…</div>
          ) : shown.length === 0 ? (
            <div className="artifacts-empty">No events yet — the crawl hasn't written any.</div>
          ) : (
            <>
              <table className="links-table">
                <thead>
                  <tr><th>Date</th><th>Type</th><th>Title</th><th>Company</th><th>Event URL</th><th>Source</th></tr>
                </thead>
                <tbody>
                  {shown.map((e) => (
                    <tr key={e.id}>
                      <td className="lt-date">{fmtDate(e.date)}</td>
                      <td><span className="chip">{e.type || "untyped"}</span></td>
                      <td className="lt-title">{e.title || "—"}</td>
                      <td className="lt-company">{labelById[e.company_id] || "—"}</td>
                      <td>{e.url ? <a className="lt-link" href={e.url} target="_blank" rel="noreferrer">{e.url}</a> : "—"}</td>
                      <td><button className="src-btn" onClick={() => openSource(e)}>page</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {hasMore && (
                <div className="load-more-wrap">
                  <button className="load-more" disabled={loadingMore} onClick={() => loadEvents(company, events.length, true)}>
                    {loadingMore ? "Loading…" : "Load more"}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* SOURCE-PAGE MODAL — the rendered text the model read to extract this event */}
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
                  {/* WHAT WE ASKED — the extraction system prompt (persona + rules) the VLM ran under */}
                  {src.prompt && (
                    <>
                      <div className="section-label" style={{ marginTop: 0, marginBottom: 6 }}>VLM prompt — what we asked<span className="rule" /></div>
                      <pre className="src-pre" style={{ opacity: 0.85, whiteSpace: "pre-wrap" }}>{src.prompt}</pre>
                    </>
                  )}
                  {/* WHAT THE MODEL READ — the exact rendered page content (links are inline-tagged as [anchor](Lnn) before sending) */}
                  <div className="section-label" style={{ marginBottom: 6 }}>Page — what the model read<span className="rule" /></div>
                  {src.content ? <pre className="src-pre">{src.content}</pre>
                    : <div className="artifacts-empty">No source page stored for this event yet (the crawler writes page content on the next run).</div>}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
