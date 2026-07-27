// Media page — a FLAT list of every event that HAS materials: Date | Type | Title | Company | Media (each url as a
// kind-tagged chip: pdf/audio/slides/video/page) | Enriched. Reads /api/media (waterevents.events with media_urls[]).
// Follows the WaterEvents design: media = the event's media_urls[] + its enrichment state (basic_info present).
// {USER 2026-07-23 "one page media, list view, simplest ... follow our current json and table design"}.
import { useEffect, useMemo, useState } from "react";

interface Material { url: string; kind: string; }
interface MediaRow {
  id: string;
  date: string;
  type: string;
  title: string;
  company: string;
  media: Material[];      // the event's material links + their kinds
  media_count: number;
  enriched: boolean;      // media_agent parsed the page → basic_info present
  status: string;         // discovered | enriched
}

function parseDate(s: string): number { const t = Date.parse((s || "").trim()); return isNaN(t) ? 0 : t; }
function fmtDate(s: string): string {
  if (!s) return "—";
  const t = parseDate(s);
  return t ? new Date(t).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" }) : s;
}

export default function MediaView() {
  const [rows, setRows] = useState<MediaRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [onlyEnriched, setOnlyEnriched] = useState(false);
  useEffect(() => {
    const load = () => fetch("/api/media").then((r) => r.json()).then((d) => { setRows(Array.isArray(d) ? d : []); setLoading(false); }).catch(() => setLoading(false));
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const sorted = useMemo(() => rows.slice().sort((a, b) => parseDate(b.date) - parseDate(a.date)), [rows]);
  const shown = onlyEnriched ? sorted.filter((r) => r.enriched) : sorted;
  const enrichedN = useMemo(() => sorted.filter((r) => r.enriched).length, [sorted]);

  if (loading && !rows.length) return <div className="loading">Loading media…</div>;

  return (
    <div className="body-full">
      <div className="section-label">Media ({shown.length})<span className="rule" /></div>

      <div className="type-filter">
        <button className={`type-chip${!onlyEnriched ? " on" : ""}`} onClick={() => setOnlyEnriched(false)}>
          All <span className="tc-n">{sorted.length}</span>
        </button>
        <button className={`type-chip${onlyEnriched ? " on" : ""}`} onClick={() => setOnlyEnriched(true)}>
          Enriched <span className="tc-n">{enrichedN}</span>
        </button>
      </div>

      {shown.length === 0 ? (
        <div className="artifacts-empty">No media yet — no event has materials.</div>
      ) : (
        <table className="links-table">
          <thead>
            <tr><th>Date</th><th>Type</th><th>Title</th><th>Company</th><th>Media</th><th>Enriched</th></tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id}>
                <td className="lt-date">{fmtDate(r.date)}</td>
                <td><span className="chip">{r.type || "untyped"}</span></td>
                <td className="lt-title">{r.title || "—"}</td>
                <td className="lt-company">{r.company}</td>
                <td className="lt-media">
                  {r.media.map((m, i) => (
                    <a key={i} className={`chip media-${m.kind}`} href={m.url} target="_blank" rel="noreferrer" title={m.url}>{m.kind}</a>
                  ))}
                </td>
                <td>{r.enriched ? <span className="chip green">yes</span> : <span className="chip">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
