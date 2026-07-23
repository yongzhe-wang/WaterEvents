"""event_agent.urls — tiny, dependency-free URL helpers shared by the BFS (crawl.py) and the DB layer (db.py).

WHY its own module: `_canon` is the canonical dedup key both the in-memory BFS AND the Postgres `dedup_key` rely on, so
the two MUST compute it identically. Putting it here (stdlib-only) also keeps db.py off crawl.py's heavy import chain
(crawl imports providers.watercrawl/qwen_llm → playwright/openai), so the DB layer + its verify test run with just
asyncpg — no browser/VLM install needed. {DB.PY "dedup_key = _canon(urls[0])"} [CONFIDENCE: CONFIRMED 100% — same key
in memory and in the DB is what makes ON CONFLICT idempotent against the BFS's own dedup].
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def _canon(url: str) -> str:
    """Canonical dedup key: lowercase scheme+host, drop #fragment + trailing slash. Path case + query preserved."""
    try:
        # case-INSENSITIVE scheme check: "HTTPS://X" must NOT get "https://" prepended (→ "https://HTTPS://X", a broken
        # dedup_key that defeats ON CONFLICT idempotency). {AUDIT 2026-07-23 MEDIUM}.
        p = urlsplit(url if url.lower().startswith("http") else "https://" + url)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, "")) or url
    except Exception:                                        # noqa: BLE001 — unparseable → use the raw string as key
        return url
