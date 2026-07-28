"""Watercrawl CLIENT — a firecrawl-shaped facade over the resident-Chromium pool, so call sites that speak
firecrawl's `fc.scrape(...) -> doc.markdown/.links/.html` can swap to a self-hosted, zero-cost engine with a
one-line change. WHY a firecrawl-shaped API: the crawl already knows how to consume a firecrawl Document
(reads `.markdown`, `.links`, `.html`); giving watercrawl the SAME shape makes it a drop-in "free firecrawl"
rather than a second dialect every call site must special-case. {USER 2026-07-04 "build the new tool under
providers called watercrawl ... put this entire system into that as a good tool"} [CONFIDENCE: CONFIRMED — direct directive].

Upstream: page.fetch_page / drive_archive pick watercrawl as the primary render+drive lane. Downstream: returns
a WaterDoc whose attributes mirror firecrawl's Document, so `getattr(doc, "markdown", "")` / `.links` / `.html`
work unchanged. The heavy lifting (browser lifecycle in runtime.py, rendering in render.py, driving in drivers/)
lives across the refactored watercrawl package; this facade only calls render.render().
"""
from __future__ import annotations

from . import render as _render


class WaterDoc:
    """A firecrawl-Document-shaped result: `.markdown`, `.links` (list of url strings), `.html`. WHY mirror
    firecrawl's fields: existing call sites do `getattr(doc, "markdown", "")` and `[l.url for l in doc.links]`
    — matching the names lets watercrawl be a drop-in without touching those readers. `.metadata` is a stub
    (watercrawl bills nothing, so cost.fc_credits reads 0)."""

    def __init__(self, markdown: str, links: list, html: str = "") -> None:
        self.markdown = markdown                             # extracted page text (innerText) — DeepSeek classify context
        self.links = list(links)                             # absolute on-page urls (already http-filtered by the pool)
        self.html = html                                     # raw HTML when requested (controls-gathering); "" otherwise
        self.metadata = {"credits_used": 0, "engine": "watercrawl"}   # zero-cost: no external billing


class WatercrawlClient:
    """Thin stateless facade — all state (the resident browser) lives in runtime.py's module singletons, so the
    client itself is cheap to construct per call, exactly like firecrawl_client()."""

    def scrape(self, url: str, *, formats: list | None = None, only_main_content: bool = False,
               actions: list | None = None, timeout: int = 60000, wait_ms: int = 0) -> WaterDoc:
        """Render `url` via the self-hosted browser → WaterDoc. `actions` mirrors firecrawl's action list: a
        sequence of {"type":"executeJavascript","script":...} / {"type":"wait","milliseconds":N} — we fold it
        into a single inject-JS + settle so the same year-select / load-more scripts the crawl already builds
        run unchanged. `formats`/`only_main_content`/`timeout` are accepted for signature-compatibility with
        firecrawl (the pool always returns markdown+links; html is best-effort). WHY collapse actions: the pool
        drives one control then extracts, which covers the crawl's executeJavascript+wait pattern; multi-step
        firecrawl action chains beyond that are not needed by the IR crawl. {SEE page._scrape_actions — the
        exact action shape we accept}."""
        inject = None
        settle = wait_ms
        for a in (actions or []):                            # translate firecrawl actions → one inject-JS + a settle wait
            if isinstance(a, dict) and a.get("type") == "executeJavascript" and a.get("script"):
                inject = (inject + ";" if inject else "") + a["script"]   # chain multiple scripts into one injection
            elif isinstance(a, dict) and a.get("type") == "wait":
                settle = max(settle, int(a.get("milliseconds") or 0))     # honor the longest requested settle
        text, links = _render.render(url, inject_js=inject, wait_ms=settle)   # the resident-browser render
        return WaterDoc(text, links)                         # firecrawl-shaped result


def watercrawl_client() -> WatercrawlClient:
    """Factory mirroring firecrawl_client() — returns a stateless facade over the resident browser pool.
    Swap `from src.providers.firecrawl import firecrawl_client` → `from src.providers.watercrawl import
    watercrawl_client` to move a render off paid firecrawl onto the free self-hosted lane."""
    return WatercrawlClient()
