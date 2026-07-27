"""event_agent — the EVENT service (this fork): company URL → events, plus the queue-draining scheduler that runs it.

Subpackages (single-responsibility, ≤4 modules/layer per the repo restructure):
  crawl/      — the crawl engine: engine (BFS close-loop) · extract (VLM endpoint) · prompts · trace
  storage/    — Postgres persistence: events (flush + pages) · queue (work_queue) · urls (dedup identity)
  scheduler/  — the scheduling service: worker (omnipotent pool) · scan (one unit) · seed (populate queue)
    solver/   — the packing controller: pacer (T*) · metrics (C_R/C_V/hit_rate) · profile (capacity)
  title/      — cheap VLM-free title backfill (og:title / PDF /Title), per-scan hook + batch CLI
"""
# LAZY exports via PEP 562 __getattr__: `from agent.event_agent import crawl_company` still works, but importing the
# PACKAGE no longer eagerly pulls extract→crawl→providers. storage/scheduler import with ONLY asyncpg present (no browser).
# All three attrs resolve through the crawl/ facade (crawl/__init__ re-exports engine.crawl_company + extract.*).
_LAZY = {"crawl_company": ".crawl", "extract_page": ".crawl", "extract_pages": ".crawl"}


def __getattr__(name: str):                                  # PEP 562 — only fires on attribute access, not at import
    if name in _LAZY:
        import importlib
        mod = importlib.import_module(_LAZY[name], __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["crawl_company", "extract_page", "extract_pages"]
