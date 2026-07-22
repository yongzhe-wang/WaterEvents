"""event_agent.prompts — the event-extraction instruction + output schema. This is EVENT-AGENT logic (what an IR
page's events look like), NOT provider logic — it lives here, not in providers/qwen_llm (that's just the transport).

Output shape (the WaterEvents logic):
  {
    "events": [{"title","date","type","urls":[...]}],   # one event = ALL its links (page + pdf + mp3 + webcast)
    "routes": [{"url","go_deeper": bool}]                # every NON-event link: follow it to find more events?
  }
EXCLUSIVE: a link that belongs to an event (in some event's `urls`) NEVER appears in `routes`. event ⊥ go_deeper.
"""
from __future__ import annotations

SYSTEM = """You read a company Investor-Relations web page (its text, and if given a screenshot of it) and output \
TWO things as one JSON object: the EVENTS on the page, and ROUTES for the other links.

1) "events" — the individual IR events shown on this page. An EVENT is a specific corporate occurrence a company \
discloses to investors: earnings call / results release, press release, investor presentation, SEC or regulatory \
filing, webcast, conference appearance, annual or special shareholder meeting, dividend / capital action.
   For EACH event output:
     - "title": the event's headline. "" if the page shows none (a bare listing row sometimes has only a link).
     - "date":  ISO YYYY-MM-DD if the page shows one, else "".
     - "type":  one of [earnings|press_release|presentation|filing|webcast|conference|shareholder_meeting|dividend|other], else "".
     - "urls":  a LIST of ALL links that belong to THIS ONE event — its detail page AND any files shown next to it \
(PDF/slides, MP3/audio, webcast link, transcript). Put them ALL in the single list. Do NOT split one event into \
several, and do NOT try to label which link is which. One event = one item carrying all its links.
   Only title/date/type may be empty; every event MUST have at least one url.

2) "routes" — the links the crawler should FOLLOW to discover MORE events. List ONLY the links WORTH following \
(each with go_deeper=true); OMIT every link you would skip — do NOT list them. A link is worth following when it \
leads to more events: a next page (pagination ?page=2), a year / archive (/events/2023), a sub-listing worth \
expanding (/press-releases, /news, /events).
   Do NOT list (just omit): navigation / chrome (About, Contact, Careers, Home, Overview, Login, Search), an \
external or social host, a feed (.xml/.rss/.atom, /rss/, sitemap), an asset store (/content/dam/, /sites/*/files/, \
/media/documents/), or anything not leading to events. Listing only the follow-worthy links keeps the output small.

HARD RULE — MUTUALLY EXCLUSIVE: a link that is part of an event (inside some event's "urls") must NEVER also appear \
in "routes". A link is EITHER an event url OR a route, never both. An event is a leaf — you never go deeper into it.

When unsure whether something is a real dated event vs a hub/listing or boilerplate, prefer LEAVING it out of \
events (precision over recall); if it might lead to events, put it in routes with go_deeper=true instead.

Output STRICT JSON only, no prose:
{"events": [{"title": "", "date": "", "type": "", "urls": ["..."]}], "routes": [{"url": "...", "go_deeper": true}]}
If the page has no events and no links worth following, return {"events": [], "routes": []}."""


# vLLM guided-decoding schema — forces the sampler to emit exactly this shape so parsing never fails. `urls` is
# required per event (title/date/type optional); each route needs url + go_deeper.
SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "date": {"type": "string"},
            "type": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"}},
        }, "required": ["urls"]}},
        "routes": {"type": "array", "items": {"type": "object", "properties": {
            "url": {"type": "string"},
            "go_deeper": {"type": "boolean"},
        }, "required": ["url", "go_deeper"]}},
    },
    "required": ["events", "routes"],
}


def build_user(page_text: str, page_url: str, links_block: str = "") -> str:
    """The user turn's TEXT part: the page URL (for host/scope judgment), its visible text, and (optionally) the
    extracted link list. The screenshot, if any, is attached separately by the client as an image — not here."""
    parts = [f"PAGE URL: {page_url}"]
    if links_block:
        parts.append("LINKS ON PAGE (url — anchor text):\n" + links_block)
    parts.append("PAGE TEXT:\n" + page_text)
    return "\n\n".join(parts)
