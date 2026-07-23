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

SYSTEM = """You read a company Investor-Relations web page — its CONTENT in reading order with every link shown \
INLINE as [anchor text](url) right where it appears, plus (if given) a screenshot of it — and output TWO things as \
one JSON object: the EVENTS on the page, and ROUTES for the other links. Because links are inline, the links that sit \
NEXT TO an event's title/date in the text are that event's links; use that adjacency to group them.

1) "events" — the individual IR events shown on this page. An EVENT is a specific corporate occurrence a company \
discloses to investors: earnings call / results release, press release, investor presentation, SEC or regulatory \
filing, webcast, conference appearance, annual or special shareholder meeting, dividend / capital action.
   For EACH event output:
     - "title": the event's headline. THREE cases, in order: (a) the page shows a title → use it verbatim; \
(b) NO explicit title, but there is enough context (a date, the type, and nearby text) to describe the event → \
write a short one-line title yourself that summarizes it (e.g. "Q1 2026 Earnings Conference Call"); \
(c) neither a title nor enough context to summarize → leave it "".
     - "date":  record whatever date granularity the page actually shows — do NOT invent missing parts. Full day \
→ "YYYY-MM-DD"; month only → "YYYY-MM"; year only → "YYYY"; a quarter/half → "YYYY-Q1".."YYYY-Q4" or "YYYY-H1"/"YYYY-H2" \
(e.g. "Q1 2026" → "2026-Q1"). If no date at all → "".
     - "type":  one of [earnings|press_release|presentation|filing|webcast|conference|shareholder_meeting|dividend|other], else "".
     - "urls":  a LIST of ALL links that belong to THIS ONE event — its detail page AND any files shown next to it \
(PDF/slides, MP3/audio, webcast link, transcript). These are exactly the inline [anchor](url) links sitting next to \
this event's title/date in the text. Put them ALL in the single list. Do NOT split one event into several, and do \
NOT try to label which link is which. One event = one item carrying all its links.
   Only title/date/type may be empty; every event MUST have at least one url.

2) "routes" — a plain LIST of the link urls the crawler should FOLLOW to discover MORE events. List ONLY the links \
WORTH following; OMIT every link you would skip — do NOT list it. A link is worth following when it leads to more \
events: a next page (pagination ?page=2), a year / archive (/events/2023), a sub-listing worth expanding \
(/press-releases, /news, /events).
   Do NOT list (just omit): navigation / chrome (About, Contact, Careers, Home, Overview, Login, Search), an \
external or social host, a feed (.xml/.rss/.atom, /rss/, sitemap), an asset store (/content/dam/, /sites/*/files/, \
/media/documents/), or anything not leading to events. `routes` holds ONLY follow-worthy urls, so it stays small.

HARD RULE — MUTUALLY EXCLUSIVE: a link that is part of an event (inside some event's "urls") must NEVER also appear \
in "routes". A link is EITHER an event url OR a route, never both. An event is a leaf — you never go deeper into it.

When unsure whether something is a real dated event vs a hub/listing or boilerplate, prefer LEAVING it out of \
events (precision over recall); if it might lead to events, put it in routes instead.

Output STRICT JSON only, no prose:
{"events": [{"title": "", "date": "", "type": "", "urls": ["..."]}], "routes": ["...", "..."]}
If the page has no events and no links worth following, return {"events": [], "routes": []}."""


# vLLM guided-decoding schema — forces the sampler to emit exactly this shape so parsing never fails. `urls` is
# required per event (title/date/type optional); `routes` is a FLAT array of url strings — every url listed is one
# to follow, so no per-route go_deeper flag exists (dropping it also halves the routes token cost). {USER 2026-07-23
# "you dont even need that go_deeper field and just keep a list of urls go deeper"} [CONFIDENCE: CONFIRMED 100% —
# direct user instruction; the old {url,go_deeper:false} entries were pure token waste that helped truncate output].
SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "date": {"type": "string"},
            "type": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"}},
        }, "required": ["urls"]}},
        "routes": {"type": "array", "items": {"type": "string"}},   # plain url list — all are go-deeper targets
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
