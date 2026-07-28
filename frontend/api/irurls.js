// GET /api/irurls — the IR-URL map: for every company, WHICH URLs the crawler actually uses as entry points.
// One row per company: its primary `ir_url` (the seed) + every `event_hubs` entry discovered by ir_url_agent
// (the events / presentations / calendar sub-pages that get seeded into the crawl frontier alongside the homepage).
// WHY this page exists: the seed set is now MULTI-URL per company (ir_url + N event_hubs → multi-frontier crawl), and
// there was no way to SEE what we're actually crawling for a given company — this makes the seed universe inspectable.
// {USER 2026-07-26 "create a new tab on the left and call it IR_URLS, and still by company, it is the url for each
// company we use here — basically just some visualization"}.
import { sbAll } from "../lib/_db.js";

// Ticker is the label; fall back to the IR host when a company has no ticker (foreign listings sometimes lack one).
function hostOf(u) { try { return new URL(u).host.replace(/^www\./, ""); } catch { return u || "—"; } }

// event_hubs elements come in TWO shapes: the ir_url_agent object {url,kind,source,is_seed,alive,checked_at} and the
// LEGACY bare URL string (backfilled from the old ir-pipeline before the agent existed). Normalize both to one shape so
// the UI never has to branch. {DB 2026-07-26 "EVENT_HUBS[0] IS OBJECT 5 / IS STRING 2390" — both shapes live in prod}.
// SCHEME ALLOW-LIST. Every url on this page originates OUTSIDE our trust boundary: event_hubs is written by an LLM
// agent from third-party company websites, so its contents are attacker-influenceable by anyone who controls a page we
// crawl. IrUrlsView renders these straight into `href={u.url}`, and a `javascript:` href executes on OUR origin the
// moment an operator clicks it — where the queue-admin token is sitting in localStorage under "queueToken". That chain
// (poison a crawled page → land a javascript: hub → operator clicks → token exfiltrated → queue control) is closed by
// refusing anything that is not http(s) right here, at the boundary. The sibling endpoints already do exactly this
// (`api/events.js` and `api/media.js` both gate on `.startsWith("http")`); irurls was the one that did not, so this is
// closing an inconsistency rather than inventing a rule.
// {IRURLSVIEW.TSX:170 "<A CLASSNAME="LT-LINK" HREF={U.URL} TARGET="_BLANK" REL="NOREFERRER">"}
// {TODAYVIEW.TSX:193 "USESTATE<STRING>(() => LOCALSTORAGE.GETITEM("QUEUETOKEN") || "")"}
// [CONFIDENCE: CONFIRMED 100% — both sink and token store read in this repo; the two sibling endpoints' existing
//  http-prefix checks show the intended standard]. Note startsWith("http") alone would admit "httpjavascript:" style
//  oddities and is scheme-prefix matching rather than parsing, so parse properly and compare the protocol.
function safeUrl(u) {
  if (typeof u !== "string") return null;
  try {
    const p = new URL(u.trim());
    return (p.protocol === "http:" || p.protocol === "https:") ? u.trim() : null;
  } catch {
    return null;                              // not an absolute url at all → not renderable as a link
  }
}

function normHub(h) {
  if (typeof h === "string") {
    const u = safeUrl(h);
    return u ? { url: u, kind: "unknown", source: "legacy", is_seed: true, alive: null } : null;
  }
  if (h && typeof h === "object" && h.url) {
    const u = safeUrl(h.url);
    if (!u) return null;                      // non-http(s) hub → drop; never hand a scriptable scheme to the UI
    return {
      url: u,
      kind: h.kind || "unknown",
      source: h.source || "unknown",
      is_seed: h.is_seed !== false,          // absent → assume seed (legacy objects predate the flag)
      alive: h.alive === undefined ? null : h.alive,
    };
  }
  return null;                                // malformed element → drop rather than render junk
}

export default async function handler(_req, res) {
  try {
    // Two independent reads: (1) the SEED start-urls per company (companies.event_hubs, from ir_url_agent) and (2) the
    // MONITORING HUBS per company (work_queue type=incremental — the listing pages the incremental service re-scans each
    // cycle). These are DIFFERENT concepts that happen to be shown on the same page: seed urls are where a crawl STARTS,
    // hubs are the pages we KEEP WATCHING for new events. {USER 2026-07-26 "the urls are just the base start url ... create
    // another section [for] the hub urls ... nothing related to the ir urls"} [CONFIDENCE: CONFIRMED — direct instruction].
    const [rows, hubRows] = await Promise.all([
      sbAll("companies?select=id,ticker,ir_url,event_hubs,ir_url_source,ir_url_validation&order=ticker.asc"),
      sbAll("work_queue?select=company_id,url,status,due_at,last_event_count,last_scanned_at&type=eq.incremental"),
    ]);
    // group the monitoring hubs by company_id → attach to each company below
    const hubsByCompany = new Map();
    for (const h of hubRows || []) {
      if (!h.company_id) continue;                         // a hub row with no company (shouldn't happen post-seed) → skip
      const arr = hubsByCompany.get(h.company_id) || [];
      const hu = safeUrl(h.url);                           // work_queue.url is crawler-derived → same untrusted origin
      if (!hu) continue;                                   // and it lands in a second href sink (IrUrlsView.tsx:209)
      arr.push({
        url: hu,
        status: h.status || "queued",                      // queued | running | failed
        last_event_count: h.last_event_count == null ? null : h.last_event_count,   // events found on the last scan
        last_scanned_at: h.last_scanned_at || null,        // null = never scanned yet (freshly seeded)
      });
      hubsByCompany.set(h.company_id, arr);
    }
    const out = (rows || []).map((r) => {
      const monHubs = (hubsByCompany.get(r.id) || []).sort((a, b) => (b.last_event_count || 0) - (a.last_event_count || 0));
      const hubs = (Array.isArray(r.event_hubs) ? r.event_hubs : []).map(normHub).filter(Boolean);
      // The primary ir_url is ALWAYS the first entry — it's the seed the worker crawls even with zero hubs. Dedupe it
      // out of the hub list so a company whose agent also returned the homepage doesn't show the same URL twice.
      // companies.ir_url is ALSO agent-written (ir_url_agent discovers it), so it gets the same gate as the hubs — it
      // renders through the identical href sink and is not more trusted just because it is the seed.
      const primaryUrl = safeUrl(r.ir_url);
      const primary = { url: primaryUrl, kind: "primary", source: r.ir_url_source || "seed", is_seed: true, alive: null };
      const seen = new Set([(primaryUrl || "").replace(/\/+$/, "")]);
      const extra = hubs.filter((h) => {
        const k = (h.url || "").replace(/\/+$/, "");
        if (!k || seen.has(k)) return false;
        seen.add(k);
        return true;
      });
      const urls = primaryUrl ? [primary, ...extra] : extra;
      return {
        id: r.id,
        label: r.ticker || hostOf(r.ir_url),
        ir_url: primaryUrl,                                // gated too — same untrusted origin, same rendering path
        source: r.ir_url_source || "—",              // who set the seed: ir_url_agent / fmp / manual-…
        validation: r.ir_url_validation || "",        // "ir_url_agent: N hubs, M seed" summary when the agent ran
        url_count: urls.length,
        seed_count: urls.filter((u) => u.is_seed).length,
        urls,
        hub_count: monHubs.length,     // # monitoring hubs (work_queue incremental) for this company — separate from seed urls
        hubs: monHubs,                 // the listing pages the incremental service re-scans each cycle
      };
    });
    res.json(out);
  } catch (_e) {
    res.status(500).json({ error: "failed to load ir urls" });   // never leak the raw PostgREST error
  }
}
