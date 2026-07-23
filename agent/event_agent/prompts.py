"""event_agent.prompts — the event-extraction instruction + output schema. This is EVENT-AGENT logic (what an IR
page's events look like), NOT provider logic — it lives here, not in providers/qwen_llm (that's just the transport).

Output shape (the WaterEvents logic):
  {
    "events": [{"title","date","type","urls":[...]}],   # one event = ALL its links (page + pdf + mp3 + webcast)
    "routes": ["url", ...]                               # just the links to FOLLOW (go deeper) to find MORE events
  }
EXCLUSIVE: a link that belongs to an event (in some event's `urls`) NEVER appears in `routes`. An event is a leaf.
"""
from __future__ import annotations

import re

# LINK-TAGGING (mirror of media_agent) — rewrite every inline `[anchor](url)` → `[anchor](Lnn)` short REFERENCE ID so
# the model echoes a cheap 2-3 char id instead of re-typing long urls. The VL model reliably COPIES page content but
# LAZILY re-lists urls (Block: listed 12/28 media; an IR overview: dropped routes) — a short id makes "list them ALL"
# cheap so the drop disappears. extract.py tags page_text before the model sees it, then resolves the model's Lnn refs
# (in every event's urls[] AND in routes) back to real urls. {DEBUG 2026-07-23 model under-lists urls} [CONFIDENCE:
# CONFIRMED 100% — req dumps proved every url was IN the input; the model just under-listed when asked to re-type them].
_LINK_RE = re.compile(r'\[([^\]]*)\]\((https?://[^)\s]+)\)')
_MD_ANY_RE = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')            # [anchor](X) → X, where X is an Lref OR a url


def tag_links(page_text: str) -> tuple[str, dict]:
    """Rewrite every inline `[anchor](url)` → `[anchor](Lnn)` and return (tagged_text, {Lnn: url}). Same url → same id
    (dedup, stable first-seen order), so the model lists a repeated link once and every id resolves back."""
    mapping: dict[str, str] = {}
    order: dict[str, str] = {}

    def _sub(m: "re.Match") -> str:
        anchor, url = m.group(1), m.group(2)
        tag = order.get(url)                                  # reuse the id if this url was already tagged
        if tag is None:
            tag = f"L{len(order) + 1}"                        # 1-based ids in first-seen order
            order[url] = tag
            mapping[tag] = url
        return f"[{anchor}]({tag})"
    return _LINK_RE.sub(_sub, page_text or ""), mapping


def resolve_url_list(items: list, tag_map: dict) -> list[str]:
    """The model's url refs → real urls. Accepts a bare `Lnn`, a `[anchor](Lnn)` wrapper, or a bare / wrapped http url
    (a link the model read off the SCREENSHOT that had no text tag). An unknown `Lnn` (hallucinated ref) is dropped."""
    out: list[str] = []
    for it in items or []:
        if not isinstance(it, str):
            continue
        s = it.strip()
        m = _MD_ANY_RE.match(s)                               # unwrap [anchor](X) → X (X = Lref or url)
        if m:
            s = m.group(1)
        if s in tag_map:                                      # the normal path: a ref id we handed the model
            out.append(tag_map[s])
        elif s.lower().startswith("http"):                   # vision-only url (no text tag existed) — preserve it
            out.append(s)
        # else: an `Lnn` not in the map = a ref the model invented → drop, never guess a url
    return out


SYSTEM = """You read a company Investor-Relations web page — its CONTENT in reading order with every link shown \
INLINE as [anchor text](Lnn) where Lnn is that link's short REFERENCE ID (e.g. L7), plus (if given) a screenshot of \
it — and output TWO things as one JSON object: the EVENTS on the page, and ROUTES for the other links. Because links \
are inline, the links that sit NEXT TO an event's title/date in the text are that event's links; use that adjacency \
to group them. Whenever you need to output a link, output its Lnn REFERENCE ID (e.g. "L7"), NOT the full url.

1) "events" — the individual IR events shown on this page. An EVENT is a specific corporate occurrence a company \
discloses to investors: earnings call / results release, press release, investor presentation, SEC or regulatory \
filing, webcast, conference appearance, annual or special shareholder meeting, dividend / capital action.
   For EACH event output:
     - "title": the event's headline. THREE cases, in order: (a) the page shows a title → use it verbatim; \
(b) NO explicit title, but there is enough context (a date, the type, and nearby text) to describe the event → \
write a short one-line title yourself that summarizes it (e.g. "Q1 2026 Earnings Conference Call"); \
(c) neither a title nor enough context to summarize → leave it "". \
STRONG RULE: almost every event row on a real IR page HAS a visible headline next to its date/link (e.g. "Microsoft \
announces quarterly dividend", "Microsoft Cloud and AI Strength Fuels Third Quarter Results", "FY26 Q4 Earnings \
Conference Call") — that text IS the title; COPY IT VERBATIM. Leaving "title":"" when the row plainly shows a headline \
is WRONG — an empty title is only for the rare genuinely-unlabelled row (case (c)). Do not skip the title to save effort.
     - "date":  record whatever date granularity the page actually shows — do NOT invent AND do NOT DOWNGRADE. If the \
page prints a FULL calendar date ("June 10, 2026", "April 29, 2026", "July 29, 2026 2:30 PM"), output the FULL day \
"YYYY-MM-DD" ("2026-06-10", "2026-04-29", "2026-07-29") — NEVER collapse a shown calendar date into a fiscal quarter \
like "2026-Q1" (that throws away the exact day the page gave you). Only use a quarter/half form when the page ITSELF \
shows only a quarter ("Q1 2026" → "2026-Q1"). Month only → "YYYY-MM"; year only → "YYYY". If no date at all → "".
     - "type":  ALWAYS classify — pick the ONE best-fitting category, and only fall back to "other"; do NOT leave it "". \
Map by what the item IS: earnings call / quarterly or annual RESULTS release → "earnings"; a company NEWS item / \
ANNOUNCEMENT / product or partnership launch / any "NVIDIA/Apple Launches/Announces …" headline / newsroom or \
press-room story → "press_release"; investor / analyst-day slide DECK or presentation → "presentation"; a 10-K/10-Q/\
8-K / proxy / SEC or regulatory FILING → "filing"; a live or replay webcast / video / audio stream → "webcast"; a \
conference appearance / fireside chat → "conference"; annual or special SHAREHOLDER / stockholder MEETING (AGM) → \
"shareholder_meeting"; a dividend declaration / buyback / capital action → "dividend". A general news/announcement is \
NEVER left blank — its type is "press_release". Use "" ONLY when the item is a real event but fits truly none of these; \
prefer "other" over "" if it is clearly an event but uncategorizable.
     - "urls":  a LIST of the Lnn REFERENCE IDS of ALL links that belong to THIS ONE event — its detail page AND any \
files shown next to it (PDF/slides, MP3/audio, webcast link, transcript). These are exactly the inline [anchor](Lnn) \
links sitting next to this event's title/date in the text — copy their Lnn ids (e.g. ["L4","L5","L6"]), do NOT re-type \
the urls. List EVERY one of them (a short id is cheap — never drop a link). Do NOT split one event into several, and \
do NOT try to label which link is which. One event = one item carrying all its link ids.
   HARD GATE — an EVENT is a SPECIFIC dated disclosure, NEVER a menu item or a bare link cluster. Emit an event ONLY \
when it has a real DATE, OR a specific TITLE naming an actual occurrence (an earnings call, a filing, a presentation, \
a shareholder meeting, a dividend). A single link — or a CLUSTER of links — that carries only a category / product / \
topic label and NO date is site CHROME, not an event. A global site-navigation or FOOTER block is NEVER an event: e.g. \
product/store links (Store, Surface, Windows, Xbox, "Shop"), topic hubs ("Microsoft in Education", "AI", "Azure", \
"Cloud", "Developer", "Industries"), "Company / Careers / Contact / Sustainability", a "Follow us" social row \
(Facebook / X / LinkedIn / YouTube / Instagram), Sitemap, and language/region pickers. Rule of thumb: several links \
grouped under bare category names with NO dates = a NAV MENU, not an events list — do NOT emit it as an event, and do \
NOT put it in routes either (it is not an IR events section). Do NOT assign "other" to a nav/footer cluster just to \
give it a type — the fix is to NOT emit it at all.
   title may be empty when the page truly lacks one, but ONLY if the event has a DATE (case (c) above); "type" should \
ALMOST ALWAYS be set (a news/announcement is "press_release", an uncategorizable-but-real event is "other"). Every \
event MUST have at least one url AND (a DATE or a specific TITLE) — a url-only row with no date and no real event \
title is chrome; leave it out entirely.
   EMIT IT HERE — never defer a real event into "routes": if a row on THIS page ALREADY shows a DATE and names a \
specific disclosure (e.g. "June 10, 2026 Microsoft announces quarterly dividend — Press Release", "April 29, 2026 … \
Third Quarter Results — Press Release · Webcast", "July 29, 2026 FY26 Q4 Earnings Conference Call"), it is a COMPLETE \
event RIGHT HERE — output it in "events" with the link(s) sitting NEXT TO it as its urls. A fully-described dated \
disclosure is a LEAF: do NOT drop it into "routes" just because its title also links to a detail page. "routes" are \
ONLY for navigation you must OPEN to reach events NOT already shown on this page (section hubs like "News & Events" / \
"Press Releases", an IR calendar, pagination, year archives) — NEVER for an event already fully visible here. When a \
dated disclosure and a section hub both exist, the dated disclosure is an EVENT and only the hub is a ROUTE.
   GROUPING — an event's urls are the links physically NEXT TO its own title/date, NOT the page's TOP-NAVIGATION bar \
(menu items like "Earnings & Financials", "Annual Reports", "SEC Filings", "Board & ESG", "More"). Those top-nav menu \
links belong to no single event — NEVER attach them to an event's urls; an event whose only urls are top-nav links \
(and none of its own adjacent links) is mis-grouped — attach its real adjacent link, or drop it.

2) "routes" — a plain LIST of the link urls the crawler should FOLLOW to discover MORE events. Be AGGRESSIVE: an IR \
OVERVIEW / LANDING page usually lists FEW or NO events directly — its whole job is to NAVIGATE into the sections that \
do. So you MUST follow the IR event-section navigation EVEN WHEN IT LOOKS LIKE A MENU ITEM: "News & Events", \
"Events & Presentations", "Events & Webcasts", "Investor Events", "IR Calendar" / "Investor Calendar", "Presentations", \
"Webcasts", "News", "Press Releases", "Financial Results" / "Financial Reports" / "Quarterly Results", "SEC Filings" \
/ "Financial Filings" / "Regulatory Filings" / "Filings", "Annual Meeting" / "Shareholder Meeting" — PLUS pagination \
(?page=2) and year/archive links (/events/2023). "SEC Filings" / "Filings" is a HIGH-value event section (10-K / 10-Q \
/ 8-K / proxy are FILING events) — ALWAYS follow it and score it HIGH. When in doubt whether a nav \
item leads to events, INCLUDE it (recall over precision for ROUTES — better to follow a dead-end hub than miss the \
events section).
   OMIT only TRUE chrome that never leads to events: About, Company, Leadership, Careers, Contact, Login, \
Search, Home, Privacy / Terms / Cookie / Legal, Store / Products / a product page (e.g. /iphone, /surface, /shop), an \
external or social host, a feed (.xml/.rss/.atom, /rss/, sitemap), an asset store (/content/dam/, /sites/*/files/, \
/media/documents/). Everything that PLAUSIBLY leads to an event listing goes in routes. NOTE: "SEC Filings" / "Filings" \
and a "Governance" / "Board of Directors" page are NOT chrome — Filings hold filing events and a governance/board page \
links to the annual MEETING + proxy, so they ARE routes (follow them; score Filings HIGH, governance MID).
   CRITICAL — STAY INSIDE INVESTOR RELATIONS. A route must be an INVESTOR-RELATIONS page. A link that LEAVES the IR area \
for the company's general consumer / marketing website is NEVER a route — DROP it entirely (do NOT emit it even with a \
low score). This is the #1 way the crawl wastes its budget: from an IR page it wanders into product pages (/iphone, \
/ipad, /mac, /watch, /tv, /music, /entertainment, /surface, /xbox, /windows, /azure), retail (/shop, /store, /retail, \
/buy), or generic corporate-marketing sections (/newsroom that is PRODUCT press not IR, /sustainability, /education, \
/business, /industries). These are on the main www site, are NOT investor events, and MUST be omitted from routes — \
not scored low, OMITTED. Only pages that are part of the INVESTOR-RELATIONS site/section (events, presentations, \
webcasts, results, filings, press releases, IR calendar, shareholder meeting, dividends, governance) belong in routes.
   Output each route as {"ref": "Lnn", "score": <confidence 0.0-1.0>} where score is how confident you are this link \
leads to REAL EVENTS: an event-section nav on the SAME IR site (News & Events, IR Calendar, Events & Presentations, \
Press Releases, Financial Results, SEC Filings, Webcasts) = HIGH 0.8-1.0; an ambiguous hub / generic listing / a \
governance or board page = MID 0.4-0.6; a marketing / product / sustainability / generic-corporate page (often on the \
main www site, not the IR subdomain) that only MIGHT eventually reach events = LOW 0.1-0.3. The crawler follows HIGHEST-score routes first, so accurate scores \
keep it ON the IR event pages and OFF marketing pages — score the obvious event sections high and the marketing/www \
pages low.

HARD RULE — MUTUALLY EXCLUSIVE: a link that is part of an event (inside some event's "urls") must NEVER also appear \
in "routes". A link is EITHER an event url OR a route, never both. An event is a leaf — you never go deeper into it.

When unsure whether something is a real dated event vs a hub/listing or boilerplate, prefer LEAVING it out of \
events (precision over recall); if it might lead to events, put it in routes instead.

Output STRICT JSON only, no prose. event urls hold Lnn REFERENCE IDS; each route is {"ref":"Lnn","score":0.0-1.0}:
{"events": [{"title": "", "date": "", "type": "", "urls": ["L4", "L5"]}], "routes": [{"ref": "L12", "score": 0.9}, {"ref": "L20", "score": 0.3}]}
If the page has no events and no links worth following, return {"events": [], "routes": []}."""


# vLLM guided-decoding schema — forces the sampler to emit exactly this shape so parsing never fails. `urls` is
# required per event (title/date/type optional); `routes` is an array of {ref, score} objects — each route carries a
# 0.0-1.0 confidence it leads to real events, so the crawl frontier is a priority queue sorted highest-score-first
# (event-section navs get crawled before marketing/www pages). The go_deeper boolean was dropped (every route IS one to
# follow); the score REPLACED it. {USER 2026-07-23 "add a new field to each router, to rank them by confidence, ...
# frontier should always sort from highest to lowest"} [CONFIDENCE: CONFIRMED 100% — direct user instruction; the flat
# unscored list let subdomain-leak marketing pages flood the frontier ahead of the real IR event sections].
SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "date": {"type": "string"},
            "type": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"}},
        }, "required": ["urls"]}},
        # each route = {ref: Lnn, score: 0.0-1.0 confidence it leads to events} — the crawl's frontier is a priority
        # queue sorted by score (highest first), so event-section navs get crawled before marketing/www pages.
        "routes": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "score": {"type": "number"},
        }, "required": ["ref", "score"]}},
    },
    "required": ["events", "routes"],
}


def build_user(page_text: str, page_url: str, links_block: str = "") -> str:
    """The user turn's TEXT part: the page URL (for host/scope judgment), its visible text, and (optionally) the
    extracted link list. The screenshot, if any, is attached separately by the client as an image — not here."""
    parts = [f"PAGE URL: {page_url}"]
    if links_block:                                          # legacy flat-list path (kept for text-only engines w/o inline)
        parts.append("LINKS ON PAGE (url — anchor text):\n" + links_block)
    # Links are embedded INLINE in reading order as [anchor text](url) right where they appear, so an event's links
    # sit next to its title/date — group them by locality. {USER "embed the links into the context not links first"}
    parts.append("PAGE CONTENT (reading order — every link shown inline as [anchor text](url) where it appears):\n" + page_text)
    return "\n\n".join(parts)
