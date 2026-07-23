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

import re

# Inline markdown link `[anchor](url)` — the shape watercrawl's inline text (and html_inline) emits for every link.
# We rewrite each into `[anchor](L<n>)` before the VLM sees it (tag_links), so the model echoes a SHORT ref id instead
# of re-typing a long url. {DEBUG 2026-07-23 Block investor-day: 28 media links IN the page_text, model listed 12}.
_LINK_RE = re.compile(r'\[([^\]]*)\]\((https?://[^)\s]+)\)')
# `(L7)` as it survives inside a basic_info md block the model reproduces — expanded back to the real url in resolve_md.
_MD_REF_RE = re.compile(r'\(L(\d+)\)')


def tag_links(page_text: str) -> tuple[str, dict]:
    """Rewrite every inline `[anchor](url)` → `[anchor](L<n>)` (a short REFERENCE ID) and return (tagged_text, {id:url}).

    WHY: the VL model reliably COPIES page content but is LAZY when asked to separately re-type a long-url LIST — on
    Block's investor-day page it listed 12 of 28 media links, dropping ALL 10 YouTube urls + ALL per-section PDFs while
    keeping only the useless `#section` nav anchors. {REQ_0003.TXT:235 "new_urls" HAD 12, INPUT HAD 28 [View]/[Download]
    links} [CONFIDENCE: CONFIRMED 100% — the req dump proved every url was IN the model's input; it just under-listed].
    A 2-3 char id (L7) is cheap to emit for ALL of them → the laziness disappears. Same url → same id (dedup, stable)."""
    mapping: dict[str, str] = {}                              # L<n> → absolute url
    order: dict[str, str] = {}                                # url → its already-assigned L<n> (dedup repeats to one id)

    def _sub(m: "re.Match") -> str:
        anchor, url = m.group(1), m.group(2)
        tag = order.get(url)                                  # reuse the id if this exact url was already tagged
        if tag is None:
            tag = f"L{len(order) + 1}"                        # ids are 1-based in first-seen order
            order[url] = tag
            mapping[tag] = url
        return f"[{anchor}]({tag})"                           # keep the familiar [text](ref) markdown shape, ref instead of url

    return _LINK_RE.sub(_sub, page_text or ""), mapping


def resolve_url_list(items: list, tag_map: dict) -> list[str]:
    """The model's new_urls (now REF IDs) → real urls. Known id → its url; a bare http url (a link the model read off
    the SCREENSHOT that had no text tag) kept as-is; an unknown/hallucinated `L<n>` dropped (unresolvable → noise)."""
    out: list[str] = []
    for it in items or []:
        if not isinstance(it, str):
            continue
        s = it.strip()
        if s in tag_map:                                      # the normal path: a ref id we handed the model
            out.append(tag_map[s])
        elif s.lower().startswith("http"):                   # vision-only url (no text tag existed) — preserve it
            out.append(s)
        # else: an `L<n>` not in the map = a ref the model invented → drop, never guess a url
    return out


def resolve_md(md: str, tag_map: dict) -> str:
    """A basic_info md block still carries `[anchor](L7)` refs (the model reproduced the page's links) → expand every
    `(L<n>)` back to its real url so the STORED content has working links, not dangling ref ids."""
    return _MD_REF_RE.sub(lambda m: "(" + tag_map.get("L" + m.group(1), "L" + m.group(1)) + ")", md or "")


SYSTEM = """You are given ONE web page belonging to a KNOWN investor-relations event (its rendered text with links \
inline as [anchor](Lnn) where Lnn is that link's short REFERENCE ID, e.g. L7, and a screenshot). Produce THIS PAGE's \
contribution to the event's archive. Do NOT summarize, \
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
faithfully; do not shorten. SKIP pure site chrome (nav menus, headers, footers, cookie banners, share buttons). When \
you reproduce a link inside md, KEEP its [anchor](Lnn) reference-id form exactly — do NOT expand it to a url.

3) "transcript_segments": IF this page contains an earnings-call / webcast TRANSCRIPT (speaker-attributed dialogue), \
extract it HERE as [{"speaker": "<name or role as shown>", "text": "<their words>"}] in order. This content is a \
TRANSCRIPT, so it goes here and MUST NOT also appear in basic_info. If the page has no transcript, return [].

4) "new_urls": the REFERENCE IDs (Lnn) of the links that are THIS EVENT'S OWN materials — its MEDIA and content, \
nothing else. Include ONLY: the webcast / audio (mp3) / VIDEO (mp4) / YouTube or webcast-replay link; the PDF / slide \
deck / presentation / transcript file(s) shown for THIS event; and any sub-page OF THIS event. A media link still \
counts even if it also appears inside a basic_info block. \
Do NOT include the site's global navigation or menus, social-media links (Facebook / Twitter / YouTube channel / \
LinkedIn), legal / footer links (privacy, terms, accessibility, cookies, "Powered by"), or investor-relations utility \
pages (email alerts, RSS, stock quote & chart, governance, board, FAQs, download library, request info, contact) — \
those are the IR SITE's chrome, NOT this event's media. If this event has no media links on the page, return []. \
Example: on an event page whose ONLY event materials are a webcast (L11) and three PDFs (L12, L13, L14), new_urls is \
exactly ["L11","L12","L13","L14"] — and nothing else, even though the page has 40+ other nav/social/footer links.

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
    # page_text here is ALREADY tag_links-processed by enrich_page — every link shows as [anchor](Lnn) not [anchor](url).
    body = ("PAGE CONTENT (reading order — every link inline as [anchor](Lnn), Lnn = that link's reference id):\n" + page_text)
    return f"PAGE URL: {page_url}\n\n{ref}\n\n{body}"
