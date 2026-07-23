// Events page — a FLAT list of every discovered event across all companies: Date | Type | Title | Company | URL.
// Reads /api/events (waterevents.events, newest first). A type-filter chip row narrows the list. Reuses the old
// links-table + type-filter styling from styles.css. {USER 2026-07-23 "one page event, list view, simplest ... follow
// our current json and table design"} — the columns mirror the events-table shape (event_date/type/title/media_urls[0]).
import { useEffect, useMemo, useState } from "react";

interface EventRow {
  id: string;
  date: string;           // event_date as text (YYYY / YYYY-MM / YYYY-Q1 / YYYY-MM-DD)
  type: string;
  title: string;
  company: string;        // IR host
  url: string | null;     // primary media url (detail page)
  media_count: number;
  status: string;         // discovered | enriched
}

// event_date is stored as varied-granularity TEXT — Date.parse handles ISO + "Month DD, YYYY"; a quarter like
// "2026-Q1" won't parse (returns 0 → sorts last, shown raw). No regex. {events.event_date "kept as text"}.
function parseDate(s: string): number { const t = Date.parse((s || "").trim()); return isNaN(t) ? 0 : t; }
function fmtDate(s: string): string {
  if (!s) return "—";
  const t = parseDate(s);
  return t ? new Date(t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" }) : s;
}

export default function EventsView() {
  const [rows, setRows] = useState<EventRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [typeFilter, setTypeFilter] = useState("all");
  // Load + auto-refresh every 30s so the list fills live as the crawl writes to Supabase.
  useEffect(() => {
    const load = () => fetch("/api/events").then((r) => r.json()).then((d) => { setRows(Array.isArray(d) ? d : []); setLoading(false); }).catch(() => setLoading(false));
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const sorted = useMemo(() => rows.slice().sort((a, b) => parseDate(b.date) - parseDate(a.date)), [rows]);
  const typeCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of sorted) { const t = e.type || "untyped"; m.set(t, (m.get(t) || 0) + 1); }
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [sorted]);
  const active = typeCounts.some(([t]) => t === typeFilter) ? typeFilter : "all";
  const events = active === "all" ? sorted : sorted.filter((e) => (e.type || "untyped") === active);

  if (loading && !rows.length) return <div className="loading">Loading events…</div>;

  return (
    <div className="body-full">
      <div className="section-label">Events ({events.length})<span className="rule" /></div>

      {typeCounts.length > 1 && (
        <div className="type-filter">
          <button className={`type-chip${active === "all" ? " on" : ""}`} onClick={() => setTypeFilter("all")}>
            All <span className="tc-n">{sorted.length}</span>
          </button>
          {typeCounts.map(([t, n]) => (
            <button key={t} className={`type-chip${active === t ? " on" : ""}`} onClick={() => setTypeFilter(t)}>
              {t} <span className="tc-n">{n}</span>
            </button>
          ))}
        </div>
      )}

      {events.length === 0 ? (
        <div className="artifacts-empty">No events yet — the crawl hasn't written any.</div>
      ) : (
        <table className="links-table">
          <thead>
            <tr><th>Date</th><th>Type</th><th>Title</th><th>Company</th><th>Event URL</th></tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td className="lt-date">{fmtDate(e.date)}</td>
                <td><span className="chip">{e.type || "untyped"}</span></td>
                <td className="lt-title">{e.title || "—"}</td>
                <td className="lt-company">{e.company}</td>
                <td>{e.url ? <a className="lt-link" href={e.url} target="_blank" rel="noreferrer">{e.url}</a> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
