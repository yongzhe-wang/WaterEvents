"""event_agent.prompts — the event-extraction + routing instructions and output schemas. This is EVENT-AGENT logic
(what an IR page's events + navigation look like), NOT provider logic — it lives here, not in providers/qwen_llm.

TWO SEPARATE PASSES (split 2026-07-24, was one combined prompt). WHY split: one ~140-line prompt did BOTH "extract
every dated event" AND "score every link as a route" in a single call — so (a) every chunked extraction request carried
the full routes ruleset it never used, wasting context, and (b) the model had to classify events AND judge navigation
at once, which worsened the long-list COLLAPSE (at ≥~20 uniform rows it stopped filling date/title and emitted url-refs
only). Splitting gives each call a SMALLER, FOCUSED prompt + SMALLER input:
  • EXTRACTION (SYSTEM_EVENTS / EVENTS_SCHEMA): sees the page CONTENT (Lnn-tagged, chunked) → outputs events only.
  • ROUTING    (SYSTEM_ROUTES / ROUTES_SCHEMA): sees only a compact LINK LIST (no body text) → outputs routes only.
event⊥route exclusivity is enforced AFTER both, in extract._combine (a ref that is an event url is dropped from routes).
{USER 2026-07-24 "separate the routing and the classification, so we can save more context"} [CONFIDENCE: CONFIRMED
100% — direct user instruction; DRAFT split for the user to own/tune in this file].

Output shapes:
  EVENTS: {"events": [{"title","date","type","urls":["L3","L4"]}]}   # one event = ALL its link REFERENCES (page+pdf+…)
  ROUTES: {"routes": [{"ref":"L7","score":0-1}, ...]}                 # link REFERENCES to FOLLOW (go deeper), each 0-1

Lnn REFERENCE IDS: every link is shown as a SHORT id `Lnn` (extraction shows it inline `[anchor](L47)`; routing shows
it as a list line `L47 — anchor`). The model copies the `L47` token (2-3 tokens) instead of a full url (15-30 tokens).
`tag_links()` builds the inline-tagged text + id→url map for EXTRACTION; `link_list()` builds the flat list + map for
ROUTING; `resolve_ids()` maps ids back to urls and DROPS any unknown/hallucinated id. {USER 2026-07-24 "using l1 l2 l3"}
[CONFIDENCE: CONFIRMED 100% — token math: ~25 tok/url → ~3 tok/ref].
"""
from __future__ import annotations

import re

# Match an inline markdown link `[anchor](url)` in the rendered reading-order text. Group 1 = anchor, group 2 = url.
_INLINE_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
# Match an Lnn reference id the model emits (bare "L47" or a stray "[anchor](L47)" wrapper).
_REF_RE = re.compile(r"^\[?[^\]]*\]?\(?\s*(L\d+)\s*\)?$")


# ── EXTRACTION prompt — EVENTS ONLY (no routing rules) ──────────────────────────────────────────────────────────────
SYSTEM_EVENTS = """You are an INVESTOR reading a company's Investor-Relations page. The page is shown to you in reading \
order with every link inline as [anchor text](Lnn) right where it appears (Lnn is a short reference id — L1, L2, …). \
You are scanning the page for the EVENTS you, as an investor, actually care about, and writing them down as JSON.

Think like an investor: what has happened or is scheduled that matters to a shareholder? — earnings calls and results, \
dividends and buybacks, SEC/regulatory filings (10-K, 10-Q, 8-K, proxy), investor presentations and slide decks, \
webcasts, conference and fireside-chat appearances, and annual or special shareholder meetings, plus material press \
releases. You do NOT write down the site's navigation menu, product or marketing pages, careers, or social links — \
those are not events (another step handles navigation).

For EACH event you spot, record:
  - "title": the headline as the page shows it — copy it verbatim. If there is no explicit headline but the date and \
nearby text make the event clear, write a short one-line title yourself (e.g. "Q1 2026 Earnings Conference Call"). Only \
if you genuinely cannot tell what it is, leave it "".
  - "date": exactly the granularity the page shows — never invent, never downgrade. A full calendar date ("June 10, \
2026") → "YYYY-MM-DD" ("2026-06-10"); a bare quarter ("Q1 2026") → "2026-Q1"; month only → "YYYY-MM"; year only → \
"YYYY"; none → "".
  - "type": the ONE best fit — "earnings" (earnings call / results), "press_release" (news / announcement), \
"presentation" (investor/analyst-day deck), "filing" (10-K/10-Q/8-K/proxy/SEC), "webcast" (live/replay stream), \
"conference" (conference or fireside appearance), "shareholder_meeting" (AGM), "dividend" (dividend/buyback), or \
"other" if it is clearly an event but none fit. A general news item is "press_release"; do not leave type "".
  - "urls": the Lnn reference ids of ALL links that belong to THIS one event — its detail page plus any PDF/slides, \
audio, webcast, or transcript shown right next to it. Copy the Lnn tokens verbatim (e.g. ["L14","L15"]). The links \
sitting next to an event's title/date are its links — group by that adjacency; never attach the page's top-nav menu \
links to an event, and never split one event into several.
  - "evidence": a short snippet (roughly 3-12 words) COPIED VERBATIM from the page above, showing this event's printed \
date and/or headline — your proof it is real (e.g. "May 27, 2026 Q2'26 Earnings Conference Call"). Copy the exact \
characters as they appear; do not paraphrase, summarize, or invent.

EXTRACT, DON'T GENERATE — the rule that matters most. Every event MUST come with "evidence": a short snippet COPIED \
VERBATIM from the page above (the exact words showing this event's printed date and/or headline). If you cannot copy a \
real snippet that shows a specific dated disclosure, the event does not exist — do NOT output it. Never write evidence \
you did not copy word-for-word. So a bare navigation link ("Earnings", "Webcasts", "Press Releases", "SEC Filings", \
"10-K", "Events & Presentations") with no dated disclosure printed next to it has NO evidence to copy → output NOTHING \
for it (routing handles navigation). NEVER manufacture a title like "<section> of Q1 2026" or invent a quarter/date the \
page does not print. If the page is only a menu of such links, return {"events": []}. NEVER output a raw url or invent \
an Lnn — copy the shown "L47" verbatim.

Output STRICT JSON only, no prose. event urls hold Lnn REFERENCE IDS:
{"events": [{"title": "", "date": "", "type": "", "urls": ["L3", "L4"], "evidence": ""}]}
If you find no events, return {"events": []}."""


# ── ROUTING prompt — ROUTES ONLY (no event-extraction rules; sees a LINK LIST, not the body text) ───────────────────
SYSTEM_ROUTES = """You are a person browsing this company's Investor-Relations website, trying to find ALL of its \
investor events. Most events are NOT on this page — they live in sections you have to CLICK into. You are given the \
page's URL and a list of its links, each shown as `Lnn — anchor text — url` (Lnn is a short reference id).

Look at the links the way you would in a browser and pick the ones you would CLICK to get to more events. Use BOTH the \
anchor text AND the url path — the path often decides it (`/investors/financial-reports`, `/ir/news`, `/events`, \
`/sec-filings` are clearly the pages you want, even if the anchor is vague).

You WOULD click, to hunt down events: News / Press Releases, Events & Presentations, Events & Webcasts, the IR Calendar, \
Financial Results / Quarterly Results / Financial Reports, SEC Filings / Regulatory Filings, Webcasts, the Annual or \
Shareholder Meeting, and pagination or year-archive links (?page=2, /events/2024). When you're not sure but a link \
plausibly leads to an events listing, click it (better to open a dead-end than miss the events).

You would NOT click — skip these entirely: product or store pages (/shop, /store, /products, a specific product), the \
company's general marketing/consumer site, About / Company / Leadership / Careers / Contact, Login / Account, Search, \
Privacy / Terms / Cookies, an email-alert signup, a social profile (x.com, linkedin, youtube, facebook, instagram), a \
sitemap or RSS/feed, or a single-purpose widget (a lone stock-chart, a glossary, an FAQ, an individual metric page).

For each link you'd click, output {"ref": "<its Lnn id, copied verbatim>", "score": <0.0-1.0>} — score how likely it \
leads to real events: a clear IR event-section = 0.8-1.0; a maybe = 0.4-0.6; a long-shot = 0.1-0.3. Do NOT give every \
link the same score, and do NOT click everything — pick the handful a person hunting for investor events actually \
would. But a page dominated by product/marketing links still has a few real IR-section links in it — find and click \
those; don't give up and return nothing. NEVER output a raw url or invent an Lnn — copy the shown "L47" verbatim.

Output STRICT JSON only, no prose:
{"routes": [{"ref": "L7", "score": 0.9}, {"ref": "L9", "score": 0.3}]}
If there is genuinely nothing worth clicking, return {"routes": []}."""


# vLLM guided-decoding schemas — force the sampler to emit exactly these shapes so parsing never fails.
# EVENTS: `urls` required per event (Lnn reference-id strings). resolve_ids() maps ids back to real urls after parsing.
EVENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "date": {"type": "string"},
            "type": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"}},   # Lnn reference ids, e.g. ["L3","L4"]
            "evidence": {"type": "string"},                           # verbatim snippet from the page — grounding anchor (verified in _normalize_events)
        }, "required": ["urls", "evidence"]}},
    },
    "required": ["events"],
}
# ROUTES: each route = {ref: Lnn id, score: 0.0-1.0} — the crawl frontier is a priority queue on score (highest first).
ROUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "routes": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "score": {"type": "number"},
        }, "required": ["ref", "score"]}},
    },
    "required": ["routes"],
}


def tag_links(page_text: str) -> tuple[str, dict[str, str]]:
    """(EXTRACTION input) Rewrite inline `[anchor](url)` links into `[anchor](Lnn)` reference ids, returning
    (tagged_text, {Lnn: url}). The model emits the SHORT id (L47 ≈ 2-3 output tokens) instead of the full url (15-30
    tokens). The SAME url reuses the SAME id. Ids are assigned in first-appearance order. {USER 2026-07-24 "using l1 l2
    l3"} [CONFIDENCE: CONFIRMED 100%]."""
    tag_map: dict[str, str] = {}
    url_to_id: dict[str, str] = {}
    counter = [0]

    def _sub(m: re.Match) -> str:
        anchor, url = m.group(1), m.group(2)
        rid = url_to_id.get(url)
        if rid is None:                                        # first time we see this url → mint a new id
            counter[0] += 1
            rid = f"L{counter[0]}"
            url_to_id[url] = rid
            tag_map[rid] = url
        return f"[{anchor}]({rid})"                            # anchor kept (semantics), url replaced by the id

    tagged = _INLINE_LINK_RE.sub(_sub, page_text or "")
    return tagged, tag_map


def link_list(page_text: str) -> tuple[str, dict[str, str]]:
    """(ROUTING input) Extract EVERY inline link from page_text into a compact flat list — one line
    `Lnn — anchor text — url` per UNIQUE url — and return (link_block, {Lnn: url}). WHY include the URL (not just the
    anchor): the url path is the STRONGEST routing signal — `/investors/financial-reports` vs `/uniform-rental` — and
    anchor-only left the model unable to tell IR sections from product/chrome, so it either bailed (0 routes) or
    bulk-routed. WHY a flat list, not the body text: routing only judges WHICH links to follow, so the links alone
    (not the reading-order content) slash its input context. Ids follow the SAME first-appearance / url-reuse scheme as
    tag_links, but this map is INDEPENDENT of any extraction map. {TEST 2026-07-24 cintas: anchor-only → 0 routes;
    anchor+url+chunking → the real /investors/* sections} [CONFIDENCE: CONFIRMED 100% — the url is the missing signal]."""
    tag_map: dict[str, str] = {}
    url_to_id: dict[str, str] = {}
    lines: list[str] = []
    for m in _INLINE_LINK_RE.finditer(page_text or ""):
        anchor, url = (m.group(1) or "").strip(), m.group(2)
        if url in url_to_id:                                   # same url already listed → one line per unique url
            continue
        rid = f"L{len(url_to_id) + 1}"                         # first-appearance order, matches the reading order
        url_to_id[url] = rid
        tag_map[rid] = url
        lines.append(f"{rid} — {anchor or '(no anchor text)'} — {url}")   # anchor AND url → the model has the real signal
    return "\n".join(lines), tag_map


def resolve_ids(items: list, tag_map: dict[str, str]) -> list[str]:
    """Map the model's emitted reference ids back to real urls via tag_map. ROBUST: an id NOT in the map (hallucinated /
    off-by-one) is DROPPED, never guessed; a bare `http…` url is kept as-is (self-verifying). Preserves order, dedups.
    Accepts an id wrapped as `[anchor](L47)` or bare `L47`."""
    out, seen = [], set()
    for it in items or []:
        s = (it if isinstance(it, str) else "").strip()
        if not s:
            continue
        if s.startswith("http"):                              # model emitted a real url directly → trust it
            url = s
        else:
            m = _REF_RE.match(s)                              # extract the Lnn id (bare or wrapped)
            rid = m.group(1) if m else s
            url = tag_map.get(rid)                            # unknown id → None → dropped (no guessing)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def build_events_user(page_text_tagged: str, page_url: str) -> str:
    """(EXTRACTION) The user turn's TEXT: page URL + reading-order content with links INLINE as [anchor](Lnn) (already
    tagged by tag_links). A screenshot, if any, is attached separately by the client."""
    return (f"PAGE URL: {page_url}\n\n"
            "PAGE CONTENT (reading order — every link shown inline as [anchor text](Lnn) reference id):\n"
            + page_text_tagged)


def build_routes_user(page_url: str, link_block: str) -> str:
    """(ROUTING) The user turn's TEXT: page URL + the flat link list from link_list(). No body content — routing judges
    links only, using BOTH the anchor text AND the url."""
    return (f"PAGE URL: {page_url}\n\n"
            "LINKS ON THIS PAGE (each shown as `Lnn — anchor text — url`). Use BOTH the anchor AND the url path to "
            "decide which to FOLLOW to reach more investor events:\n"
            + link_block)
