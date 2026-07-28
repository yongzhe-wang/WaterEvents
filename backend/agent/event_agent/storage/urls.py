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


# The four granularities the extraction prompt is allowed to emit, in the order the regexes must be tried (longest
# first, so "2026-03-15" is not mistaken for "2026-03"). {PROMPTS.PY:50-52 ""DATE": EXACTLY THE GRANULARITY THE PAGE
# SHOWS — NEVER INVENT, NEVER DOWNGRADE. A FULL CALENDAR DATE ("JUNE 10, 2026") → "YYYY-MM-DD"; A BARE QUARTER
# ("Q1 2026") → "2026-Q1"; MONTH ONLY → "YYYY-MM"; YEAR ONLY → "YYYY"; NONE → ""."}
# [CONFIDENCE: CONFIRMED 100% — the four shapes are enumerated verbatim in the prompt the model is held to].
_DATE_YMD_RE = re.compile(r"^(\d{4})-(\d\d)-\d\d$")          # 2026-03-15
_DATE_YQ_RE = re.compile(r"^(\d{4})-q([1-4])$")              # 2026-q1 (already lowercased by _norm_date)
_DATE_YM_RE = re.compile(r"^(\d{4})-(\d\d)$")                # 2026-03
_DATE_Y_RE = re.compile(r"^(\d{4})$")                        # 2026


def _date_bucket(d: str) -> str:
    """Collapse ANY of the prompt's four date granularities to ONE common coarse bucket: "YYYY-qN" (or "YYYY" when only
    a year is known, or "" when nothing is).

    WHY the key must not compare dates at their printed granularity: the extraction prompt explicitly orders the model to
    record "exactly the granularity the page shows — never invent, never downgrade", so THE SAME event legitimately
    arrives as "2026-Q1" from a hub listing and "2026-03-15" from its own detail page. Keyed verbatim those are two
    different dedup_keys, so the ON CONFLICT (company_id, dedup_key) that the whole idempotency story rests on does not
    fire and the event is inserted TWICE. Verified by executing the pre-fix function: _event_key("Q1 2026 Earnings Call",
    "2026-Q1", ...) and the same title with "2026-03-15" produced 'q12026earningscall|2026-q1' vs
    'q12026earningscall|2026-03-15'. Bucketing to the quarter is the coarsest granularity at which the four shapes can
    all agree while still keeping genuinely different events apart — a per-quarter "Quarterly Dividend" stays distinct
    because its quarter differs, which is the property _event_key's docstring already relied on.
    {PROMPTS.PY:50-52 "EXACTLY THE GRANULARITY THE PAGE SHOWS — NEVER INVENT, NEVER DOWNGRADE ... "YYYY-MM-DD" ...
     "2026-Q1" ... "YYYY-MM" ... "YYYY" ... NONE → ""."}
    {REPRODUCED 2026-07-28 by running the pre-fix _event_key: "SAME EVENT, DIFFERENT KEYS -> DUPLICATE ROW: TRUE"}
    [CONFIDENCE: CONFIRMED 100% — split reproduced by executing the real function, root cause quoted from the prompt].

    TRADE-OFF, stated explicitly: two DIFFERENT events that share a company, a title and a quarter now collapse into one
    row. Given the title is normalised to alphanumerics, that requires the same company to hold two same-named events in
    the same quarter — e.g. two "Investor Meeting" entries six weeks apart. Bucketing at the month instead would keep
    those apart but would NOT merge "2026-Q1" with "2026-03-15", which is the failure actually observed in production;
    the quarter is the only bucket at which the hub-vs-detail pair reconciles. Merging is also the cheaper error here:
    flush_events' ON CONFLICT UNIONS media_urls rather than overwriting, so a false merge keeps both events' links and
    loses only the distinction, whereas a false split creates a duplicate row that no consumer can reconcile.
    {EVENTS.PY flush_events "ON CONFLICT (COMPANY_ID, DEDUP_KEY) DO UPDATE SET MEDIA_URLS = (SELECT COALESCE(
     JSONB_AGG(DISTINCT U), '[]'::JSONB) FROM JSONB_ARRAY_ELEMENTS(EVENTS.MEDIA_URLS || EXCLUDED.MEDIA_URLS) AS U)"}
    [CONFIDENCE: CONFIRMED 100% — the merge-not-replace behaviour is read directly from the live INSERT]."""
    d = _norm_date(d)                                        # strip any time suffix first, lowercase
    if not d:
        return ""
    m = _DATE_YMD_RE.match(d) or _DATE_YM_RE.match(d)        # YYYY-MM-DD / YYYY-MM → derive the quarter from the month
    if m:
        month = int(m.group(2))
        if 1 <= month <= 12:                                 # guard a malformed "2026-13" from producing quarter 5
            return f"{m.group(1)}-q{(month - 1) // 3 + 1}"
        return m.group(1)                                    # unusable month → fall back to year-level agreement
    m = _DATE_YQ_RE.match(d)                                 # already quarter-shaped → canonical form as-is
    if m:
        return f"{m.group(1)}-q{m.group(2)}"
    m = _DATE_Y_RE.match(d)                                  # year only → the page never showed anything finer
    if m:
        return m.group(1)
    return d                                                 # unrecognised shape → key on it verbatim (never merge blindly)


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
