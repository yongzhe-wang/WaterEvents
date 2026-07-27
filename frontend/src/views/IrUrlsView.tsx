// IR_URLS page — company RAIL (left) + the URL table (right): exactly WHICH urls the crawler uses as entry points for
// each company. A company's entry set = its primary `ir_url` (kind "primary") + every `event_hubs` url discovered by
// ir_url_agent (events / presentations / calendar sub-pages) — all of which get seeded into the crawl frontier together
// (multi-frontier). WHY: the seed set went from 1 url/company to N and was invisible; this makes it inspectable.
// {USER 2026-07-26 "create a new tab on the left and call it IR_URLS, and still by company, it is the url for each
// company we use here — basically just some visualization"}.
import { useEffect, useMemo, useState } from "react";

interface UrlRow {
  url: string;
  kind: string;            // primary | homepage | events | presentations | calendar | news | unknown
  source: string;          // ir_url_agent | legacy | fmp | manual-… | seed
  is_seed: boolean;        // does the crawl actually start from this url
  alive: boolean | null;   // agent rendered the page successfully when it found the link (null = unknown/legacy)
}
// A MONITORING HUB — a listing page the incremental service re-scans each cycle (work_queue type=incremental). Distinct
// from the seed UrlRow above: a UrlRow is where a crawl STARTS, a HubRow is a page we KEEP WATCHING. {USER 2026-07-26}.
interface HubRow {
  url: string;
  status: string;                    // queued | running | failed
  last_event_count: number | null;   // events found on the last scan (null = never scanned yet)
  last_scanned_at: string | null;     // null = freshly seeded, not scanned yet
}
interface CompanyUrls {
  id: string;
  label: string;
  ir_url: string | null;
  source: string;
  validation: string;      // "ir_url_agent: N hubs, M seed" when the agent has run on this company
  url_count: number;
  seed_count: number;
  urls: UrlRow[];
  hub_count: number;       // # monitoring hubs (work_queue incremental) — separate concept from seed urls
  hubs: HubRow[];
}
const PAGE = 300;          // rows rendered per "Load more" click (the All-companies view flattens to ~14k rows)

function initials(label: string): string {
  const a = (label || "").replace(/[^a-z0-9]/gi, "");
  return (a.slice(0, 2) || "•").toUpperCase();
}
// Strip scheme + trailing slash so the table reads as paths, not a wall of "https://".
function shortUrl(u: string): string {
  return (u || "").replace(/^https?:\/\//, "").replace(/\/+$/, "") || "—";
}
// Last-scanned stamp for a hub — "never" when freshly seeded but not yet re-scanned by the incremental service.
function fmtScan(s: string | null): string {
  if (!s) return "never";
  const t = Date.parse(s);
  return isNaN(t) ? "—" : new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function IrUrlsView() {
  const [companies, setCompanies] = useState<CompanyUrls[]>([]);
  const [company, setCompany] = useState("all");    // "all" or a company id
  const [q, setQ] = useState("");                   // rail filter box
  const [kindFilter, setKindFilter] = useState("all");
  const [limit, setLimit] = useState(PAGE);         // client-side paging (data is already fully loaded)
  const [loading, setLoading] = useState(true);

  // One cheap fetch: every company + its url set. Refresh every 30s so the page stays live WHILE ir_url_agent is
  // still discovering (it writes event_hubs company-by-company, so counts climb during a run).
  useEffect(() => {
    const load = () => fetch("/api/irurls").then((r) => r.json())
      .then((d) => { setCompanies(Array.isArray(d) ? d : []); setLoading(false); })
      .catch(() => setLoading(false));
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => { setLimit(PAGE); setKindFilter("all"); }, [company]);   // new selection → fresh first page

  const totalUrls = useMemo(() => companies.reduce((s, c) => s + (c.url_count || 0), 0), [companies]);
  const totalHubs = useMemo(() => companies.reduce((s, c) => s + (c.hub_count || 0), 0), [companies]);
  const companiesWithHubs = useMemo(() => companies.filter((c) => (c.hub_count || 0) > 0).length, [companies]);
  const railList = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? companies.filter((c) => c.label.toLowerCase().includes(needle)) : companies;
  }, [companies, q]);

  // Flatten the selected scope into one row-per-url list (each row carries its company label for the All view).
  const flat = useMemo(() => {
    const scope = company === "all" ? companies : companies.filter((c) => c.id === company);
    const out: (UrlRow & { company: string; cid: string })[] = [];
    for (const c of scope) for (const u of c.urls || []) out.push({ ...u, company: c.label, cid: c.id });
    return out;
  }, [companies, company]);

  const kindCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const u of flat) { const k = u.kind || "unknown"; m.set(k, (m.get(k) || 0) + 1); }
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [flat]);
  const activeKind = kindCounts.some(([k]) => k === kindFilter) ? kindFilter : "all";
  const filtered = activeKind === "all" ? flat : flat.filter((u) => (u.kind || "unknown") === activeKind);
  const shown = filtered.slice(0, limit);
  const selected = company === "all" ? null : companies.find((c) => c.id === company);
  const selectedLabel = selected ? selected.label : "All IR urls";

  return (
    <div className="body">
      {/* SECOND SIDEBAR — the company rail, counting URLS (not events) since that's this page's subject */}
      <aside className="rail">
        <div className="rail-head">
          <input className="search" placeholder="Filter companies…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div className="rail-count">{companies.length} companies · {totalUrls} urls · {totalHubs} hubs ({companiesWithHubs} co)</div>
        <div className="rail-list">
          <div className={`crow${company === "all" ? " active" : ""}`} onClick={() => setCompany("all")}>
            <div className="avatar">∀</div>
            <div className="crow-main"><div className="crow-name">All companies</div><div className="crow-sub">every entry url</div></div>
            <span className="crow-count">{totalUrls}</span>
          </div>
          {railList.map((c) => (
            <div key={c.id} className={`crow${company === c.id ? " active" : ""}`} onClick={() => setCompany(c.id)}>
              <div className="avatar">{initials(c.label)}</div>
              <div className="crow-main">
                <div className="crow-name">{c.label}</div>
                <div className="crow-sub">{c.url_count} url{c.url_count !== 1 ? "s" : ""} · {c.hub_count} hub{c.hub_count !== 1 ? "s" : ""}</div>
              </div>
              <span className="crow-count">{c.url_count}</span>
            </div>
          ))}
        </div>
      </aside>

      {/* RIGHT — the url table for the current scope */}
      <div className="events-panel">
        <div className="body-full">
          <div className="section-label">
            {selectedLabel} ({filtered.length})<span className="rule" />
          </div>

          {/* When ONE company is selected, surface where its seed came from + the agent's own summary line. */}
          {selected && (
            <div className="type-filter">
              <span className="chip">seed source: {selected.source}</span>
              {selected.validation ? <span className="chip">{selected.validation}</span> : null}
            </div>
          )}

          {kindCounts.length > 1 && (
            <div className="type-filter">
              <button className={`type-chip${activeKind === "all" ? " on" : ""}`} onClick={() => setKindFilter("all")}>
                All <span className="tc-n">{flat.length}</span>
              </button>
              {kindCounts.map(([k, n]) => (
                <button key={k} className={`type-chip${activeKind === k ? " on" : ""}`} onClick={() => setKindFilter(k)}>
                  {k} <span className="tc-n">{n}</span>
                </button>
              ))}
            </div>
          )}

          {loading ? (
            <div className="loading">Loading ir urls…</div>
          ) : shown.length === 0 ? (
            <div className="artifacts-empty">No IR urls yet — ir_url_agent hasn't written any hubs for this scope.</div>
          ) : (
            <>
              <table className="links-table">
                <thead>
                  <tr><th>Company</th><th>Kind</th><th>URL</th><th>Seed</th><th>Found by</th></tr>
                </thead>
                <tbody>
                  {shown.map((u, i) => (
                    <tr key={`${u.cid}-${i}`}>
                      <td className="lt-company">{u.company}</td>
                      <td><span className="chip">{u.kind || "unknown"}</span></td>
                      <td><a className="lt-link" href={u.url} target="_blank" rel="noreferrer">{shortUrl(u.url)}</a></td>
                      <td>{u.is_seed ? <span className="chip">seed</span> : "—"}</td>
                      <td className="lt-date">{u.source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {filtered.length > shown.length && (
                <div className="load-more-wrap">
                  <button className="load-more" onClick={() => setLimit((n) => n + PAGE)}>
                    Load more ({filtered.length - shown.length} left)
                  </button>
                </div>
              )}
            </>
          )}

          {/* MONITORING HUBS — a SEPARATE section below the seed-url list. These are the listing pages the incremental
              service re-scans every cycle (work_queue), NOT the crawl's start urls above. Per-company only (the All view
              would flatten to ~5k rows). {USER 2026-07-26 "create another section below the current url list [for] the hub
              urls ... nothing related to the ir urls"}. */}
          {selected && (
            <>
              <div className="section-label" style={{ marginTop: 30 }}>
                Monitoring hubs — pages re-scanned for new events ({selected.hubs.length})<span className="rule" />
              </div>
              {selected.hubs.length === 0 ? (
                <div className="artifacts-empty">
                  No monitoring hubs — this company has no qualifying event-listing page yet (needs a crawl, or its pages
                  were filtered out as filings/single-event).
                </div>
              ) : (
                <table className="links-table">
                  <thead>
                    <tr><th>Hub URL</th><th>Status</th><th>Events last scan</th><th>Last scanned</th></tr>
                  </thead>
                  <tbody>
                    {selected.hubs.map((h, i) => (
                      <tr key={`hub-${i}`}>
                        <td><a className="lt-link" href={h.url} target="_blank" rel="noreferrer">{shortUrl(h.url)}</a></td>
                        <td><span className="chip">{h.status}</span></td>
                        <td className="lt-date">{h.last_event_count == null ? "—" : h.last_event_count}</td>
                        <td className="lt-date">{fmtScan(h.last_scanned_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
