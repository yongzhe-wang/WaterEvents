"""media_agent.prompts — the html-page enrichment instruction + output schema. Given a KNOWN event + ONE of its
pages, the VLM emits THIS page's CONTRIBUTION to the event archive (not the whole archive — fill-and-append).

Contribution shape (what one html page yields):
  {
    "title","date","type",                 # confirm/fill ONLY missing metadata (leave "" to keep the known value)
    "basic_info": [ {type:"md"|"list"|"table", ...} ],   # this page's content, structure-preserving, NOT summarized
    "transcript_segments": [ {speaker,text} ],           # a transcript FOUND ON THIS PAGE routes HERE, not basic_info
    "new_urls": [ "url", ... ]                           # this event's other materials to follow (pdf/audio/slides/…)
  }
KEY: html goes through the VLM (not Docling) because only the VLM discovers the media urls that drive the close-loop
AND routes inline-transcript content to the right slot. {USER 2026-07-23 "do vlm"} [CONFIDENCE: CONFIRMED 100%].
"""
from __future__ import annotations

SYSTEM = """You are given ONE web page belonging to a KNOWN investor-relations event (its rendered text with links \
inline as [anchor](url), and a screenshot). Produce THIS PAGE's contribution to the event's archive. Do NOT summarize, \
do NOT paraphrase, do NOT invent — copy the real content and PRESERVE ITS STRUCTURE. Output ONE JSON object with:

1) "title" / "date" / "type": only to CONFIRM or FILL metadata the page shows more clearly. Leave a field "" to keep \
the already-known value — do not overwrite good info. "type" ∈ [earnings|press_release|presentation|filing|webcast|\
conference|shareholder_meeting|dividend|other].

2) "basic_info": THIS page's readable content as an ORDERED list of blocks, in the page's natural reading order. Each \
block is exactly ONE of:
   - a paragraph or heading  → {"type": "md",    "md": "<markdown text, verbatim>"}
   - a bulleted/numbered list → {"type": "list",  "md": "- item\\n- item"}
   - a TABLE                  → {"type": "table", "caption": "<if any>", "headers": ["col", ...], "rows": [["cell", ...], ...]}
   Rules: keep TABLES as table blocks — NEVER flatten a table into prose (that destroys its structure). Copy text \
faithfully; do not shorten. SKIP pure site chrome (nav menus, headers, footers, cookie banners, share buttons).

3) "transcript_segments": IF this page contains an earnings-call / webcast TRANSCRIPT (speaker-attributed dialogue), \
extract it HERE as [{"speaker": "<name or role as shown>", "text": "<their words>"}] in order. This content is a \
TRANSCRIPT, so it goes here and MUST NOT also appear in basic_info. If the page has no transcript, return [].

4) "new_urls": list EVERY link on THIS page that belongs to THIS SAME event — do NOT miss any. This includes every \
MEDIA link — PDF release / slide deck / audio (mp3) / VIDEO (mp4) / YouTube or webcast replay — AND every content \
page or sub-section of the event. CRITICAL: a link belongs in new_urls EVEN IF you already wrote it inside a \
basic_info block — listing it in the prose does NOT excuse omitting it here; put its full url in new_urls too. And do \
NOT skip a link because its anchor says "View" / "Watch" / "Replay" instead of "Download" — a YouTube or video \
"View" link is still a link and MUST be listed. Give the full absolute urls. OMIT only: navigation / chrome, social, \
feeds/sitemaps, and links to OTHER events (those belong to the event-discovery crawl, not here).

Output STRICT JSON only, no prose. If the page has no usable content, return \
{"title":"","date":"","type":"","basic_info":[],"transcript_segments":[],"new_urls":[]}."""


# vLLM guided-decoding schema. A basic_info block is a permissive object: `type` is the discriminator; md is used by
# md/list blocks; caption/headers/rows by table blocks — all optional so guided decoding can emit either shape without
# forcing empty fields. transcript_segments carry speaker+text (start/end absent for html transcripts — no timestamps).
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": "string"},
        "type": {"type": "string"},
        "basic_info": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["md", "list", "table"]},
            "md": {"type": "string"},
            "caption": {"type": "string"},
            "headers": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        }, "required": ["type"]}},
        "transcript_segments": {"type": "array", "items": {"type": "object", "properties": {
            "speaker": {"type": "string"},
            "text": {"type": "string"},
        }, "required": ["text"]}},
        "new_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["basic_info", "new_urls"],
}


def build_user(page_text: str, page_url: str, known: dict) -> str:
    """The user turn's TEXT part: the KNOWN event metadata (as reference — confirm & extend, don't blindly trust) +
    the page url + the page's inline-linked reading-order content. The screenshot is attached separately as an image.
    `known` = the event_agent event {title,date,type,urls}."""
    ref = (f"KNOWN EVENT (reference — confirm & extend, do not blindly trust):\n"
           f"  title: {known.get('title','')}\n  date: {known.get('date','')}\n  type: {known.get('type','')}\n"
           f"  media urls already found: {known.get('urls', [])}")
    body = ("PAGE CONTENT (reading order — every link inline as [anchor](url) where it appears):\n" + page_text)
    return f"PAGE URL: {page_url}\n\n{ref}\n\n{body}"
