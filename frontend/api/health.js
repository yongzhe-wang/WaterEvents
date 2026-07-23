// GET /api/health — LIVE up/down of the WORKER-SIDE runtime dependencies (trafilatura, lxml_html_clean, pypdf, the
// bct DistilBERT heads, Webshare, DB). These live in the basic_info SERVICE's Python image — the dashboard's Node
// runtime can't import them, so the SERVICE self-tests each one every 60s and upserts to the `pipeline_health` table;
// this endpoint just reads that table. WHY it matters: the lxml>=5.2 split silently broke trafilatura and it only
// surfaced as 50 FAILED companies hours later — a live panel turns that into an instant red light. {USER 2026-07-21
// "show the status of all important endpoints like trafilatura and the tools apis ... correct all the time or down"}.
import { sb } from "../lib/_db.js";                           // shared Supabase REST reader

// Order the panel logically: the content pipeline deps first (the ones that broke), infra last.
const ORDER = ["trafilatura", "lxml_html_clean", "lxml", "pypdf", "curl_cffi", "bct_title_model", "bct_type_model", "webshare", "db"];

export default async function handler(_req, res) {
  try {
    // Read every row; select the columns we render. pipeline_health is tiny (one row per component).
    const rows = await sb("pipeline_health?select=component,status,detail,source,checked_at");
    const byComp = new Map((rows || []).map((r) => [r.component, r]));
    // Emit in the fixed order (known deps first), then any extra components the worker reported.
    const known = ORDER.filter((c) => byComp.has(c));
    const extra = (rows || []).map((r) => r.component).filter((c) => !ORDER.includes(c));
    const now = Date.now();
    const items = [...known, ...extra].map((c) => {
      const r = byComp.get(c);
      // STALE guard: if the worker hasn't updated a row in >5 min, its self-check stopped (service down/looping) →
      // show 'stale' rather than a misleadingly-green last-known-good.
      const age = r?.checked_at ? (now - new Date(r.checked_at).getTime()) / 1000 : null;
      const stale = age != null && age > 300;
      return {
        component: c,
        status: stale ? "stale" : (r?.status || "unknown"),
        detail: stale ? `no update for ${Math.round(age)}s` : (r?.detail || ""),
        source: r?.source || "",
        age_s: age == null ? null : Math.round(age),
      };
    });
    res.setHeader("Cache-Control", "no-store");               // always fresh — this is a live monitor
    res.status(200).json({ items, checked_at: rows?.[0]?.checked_at || null });
  } catch (e) {
    res.status(200).json({ items: [], error: String(e && e.message || e) });
  }
}
