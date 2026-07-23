"""event_agent — company URL → list of events. The AGENT logic (event definition, prompt, extraction, routing).
Uses providers/watercrawl to open pages (+ screenshot) and providers/qwen_llm as the parallel Qwen transport.

Layout (bottom-up):
  prompts.py  — SYSTEM instruction + SCHEMA: {events:[{title,date,type,urls[]}], routes:["url",...]}
  extract.py  — THE ENDPOINT: extract_page / extract_pages — (page text + optional screenshot) → {events, routes}, parallel
  crawl.py    — THE MAIN LOOP: crawl_company(url) — render_shot → extract → follow routes (go-deeper urls) → BFS close-loop
  smoke.py    — run after qwen_llm serve.sh to verify parallelism + precision/recall + exclusivity
  db.py / worker.py — prod discovery worker (Postgres queue + batch flush); import only asyncpg, NOT the browser/VLM chain
"""
# LAZY exports via PEP 562 __getattr__: `from agent.event_agent import crawl_company` still works, but importing the
# PACKAGE no longer eagerly pulls extract→crawl→providers (playwright/openai). WHY: db.py / worker.py / verify_worker
# must import the package with ONLY asyncpg present — the DB coordination layer has no business dragging in a headless
# browser. {VERIFY 2026-07-23 on GCP VM: eager `from .crawl import crawl_company` at package load → ModuleNotFoundError
# 'providers' because the DB test box has no browser stack} [CONFIDENCE: CONFIRMED 100% — the failing import chain was
# package __init__ → extract → providers.qwen_llm]. Accessing crawl_company/extract_* triggers the real import on demand.
_LAZY = {"crawl_company": ".crawl", "extract_page": ".extract", "extract_pages": ".extract"}


def __getattr__(name: str):                                  # PEP 562 — only fires on attribute access, not at import
    if name in _LAZY:
        import importlib
        mod = importlib.import_module(_LAZY[name], __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["crawl_company", "extract_page", "extract_pages"]
