"""event_agent.urls — tiny, dependency-free URL helpers shared by the BFS (crawl.py) and the DB layer (db.py).

WHY its own module: `_canon` is the canonical dedup key both the in-memory BFS AND the Postgres `dedup_key` rely on, so
the two MUST compute it identically. Putting it here (stdlib-only) also keeps db.py off crawl.py's heavy import chain
(crawl imports providers.watercrawl/qwen_llm → playwright/openai), so the DB layer + its verify test run with just
asyncpg — no browser/VLM install needed. {DB.PY "dedup_key = _canon(urls[0])"} [CONFIDENCE: CONFIRMED 100% — same key
in memory and in the DB is what makes ON CONFLICT idempotent against the BFS's own dedup].
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_TITLE_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _norm_title(t: str) -> str:
    """Title → lowercase, alphanumeric-only (drop all punctuation/whitespace/case). So "2Q26 Earnings Call!" ==
    "2q26  earnings call" == "2Q26-Earnings-Call". This is the stable half of an event's cross-source identity."""
    return _TITLE_PUNCT_RE.sub("", (t or "").lower())


def _norm_date(d: str) -> str:
    """Date → the DATE part, lowercased, time stripped: "2026-08-04T17:00:00" → "2026-08-04". Same event's date string
    is usually consistent across extractions; stripping the time suffix merges the with/without-time variants."""
    d = (d or "").strip().lower()
    return d.split("t")[0] if re.match(r"\d{4}-\d\d-\d\dt", d) else d


def _event_key(title: str, date: str, urls: list[str]) -> str:
    """An event's STABLE identity = normalized(title) + "|" + normalized(date). WHY not the url: the model attaches
    DIFFERENT urls to the SAME event across pages/chunks (social links, product/nav pages), so a url-based key duplicated
    one event into many rows (AMD Q2 earnings → 13 rows keyed by x.com/linkedin/youtube). Title+date is the event's real
    identity — same title AND same date = the same event (a per-quarter "Quarterly Dividend" stays distinct because its
    date differs). A title-less event (case-c: date only) has no title identity → fall back to the primary url's canon.
    {USER 2026-07-25 "lots of dup events ... dedup by url missed same-event-different-url"} [CONFIDENCE: CONFIRMED 100%]."""
    t = _norm_title(title)
    if t:
        return t + "|" + _norm_date(date)
    return _canon(urls[0]) if urls else ""


def _canon(url: str) -> str:
    """Canonical dedup key: lowercase scheme+host, drop #fragment + trailing slash. Path case + query preserved."""
    try:
        # case-INSENSITIVE scheme check: "HTTPS://X" must NOT get "https://" prepended (→ "https://HTTPS://X", a broken
        # dedup_key that defeats ON CONFLICT idempotency). {AUDIT 2026-07-23 MEDIUM}.
        p = urlsplit(url if url.lower().startswith("http") else "https://" + url)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, "")) or url
    except Exception:                                        # noqa: BLE001 — unparseable → use the raw string as key
        return url
