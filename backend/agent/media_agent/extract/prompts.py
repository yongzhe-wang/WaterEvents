"""media_agent.prompts — the html-page enrichment instruction + output schema. Given a KNOWN event + ONE of its
pages, the VLM emits THIS page's CONTRIBUTION to the event archive (not the whole archive — fill-and-append).

用一句话讲完: 页面的 URL 集合由 event_agent 一次性给定(`known_event.media_urls`),media_agent 只处理这个固定列表 —
所以 VLM 不再负责"发现新链接",它的工作缩到只剩模型才做得了的两件事:确认 title/date/type,以及把页面内联的
speaker-attributed transcript 分流出来。正文(basic_info)在 ROUTE 路径上由 extract_html 的 trafilatura+pandas
确定性抽取,VLM 的 schema 里连这个字段都没有。

Contribution shape (what one html page yields):
  {
    "title","date","type",                 # confirm/fill ONLY missing metadata (leave "" to keep the known value)
    "basic_info": [ {type:"md"|"list"|"table", ...} ],   # LEGACY path only — ROUTE's schema has no such field
    "transcript_segments": [ {speaker,text} ],           # a transcript FOUND ON THIS PAGE routes HERE, not basic_info
  }

{USER 2026-08-03 "let's just use the original list from the event agent and the qwen is only for diff and text
cleaning title date and type"} [CONFIDENCE: CONFIRMED 100% — direct user directive; the frontier-growth machinery
(new_urls + the Lnn reference-id scheme + the exhaustive lxml url harvest) was deleted in the same change].
"""
from __future__ import annotations

SYSTEM = """You are given ONE web page belonging to a KNOWN investor-relations event (its rendered text with links \
inline as markdown, and a screenshot). Produce THIS PAGE's contribution to the event's archive. Do NOT summarize, \
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

Output STRICT JSON only, no prose. If the page has no usable content, return \
{"title":"","date":"","type":"","basic_info":[],"transcript_segments":[]}."""


# ── ROUTE mode (deterministic-body path) ───────────────────────────────────────────────────────────────────────────
# WHY: earnings/press-release HTML embeds full financial statements (prologis: 3 statements, 56KB) → asking the VLM to
# COPY them into basic_info verbatim overflows the output cap (finish_reason=length → truncated archive). The fix moves
# body extraction to media_agent/extract_html.py (trafilatura + pandas — deterministic, more accurate on wide tables) and
# SHRINKS the VLM's job to what only a model can do. With no basic_info OUTPUT field it can NEVER overflow by copying tables.
# {USER 2026-07-24 "for basic info don't have the VLM copy everything, use trafilatura + other deterministic extractors"}
# [CONFIDENCE: CONFIRMED 100% — direct instruction; prologis output_truncated=length proved the copy-overflow].
SYSTEM_ROUTE = """You are given ONE web page belonging to a KNOWN investor-relations event (its rendered text and a \
screenshot). THIS PAGE'S READABLE CONTENT (paragraphs, headings, lists, financial TABLES) HAS ALREADY BEEN EXTRACTED \
SEPARATELY AND DETERMINISTICALLY — do NOT reproduce it, do NOT copy tables, do NOT summarize the body. Your job is ONLY \
the two things that need a model. Output ONE JSON object with:

1) "title" / "date" / "type": only to CONFIRM or FILL metadata the page shows more clearly. Leave a field "" to keep \
the already-known value — do not overwrite good info. "type" ∈ [earnings|press_release|presentation|filing|webcast|\
conference|shareholder_meeting|dividend|other].

2) "transcript_segments": IF this page contains an earnings-call / webcast TRANSCRIPT (speaker-attributed dialogue), \
extract it HERE as [{"speaker": "<name or role as shown>", "text": "<their words, verbatim>"}] in order. This is the ONE \
kind of body content you DO extract, because routing speaker turns needs a model. If the page has no transcript, return [].

Output STRICT JSON only, no prose: {"title":"","date":"","type":"","transcript_segments":[]}."""


# ROUTE schema — basic_info REMOVED (deterministic side owns it); only metadata + transcript. Guided decoding on this
# schema CANNOT emit a body-copy field, so the output stays tiny (a few dozen bytes) and the earnings-table overflow is
# impossible by construction. {DESIGN wf_7b61c8d0} [CONFIDENCE: CONFIRMED — no basic_info key].
SCHEMA_ROUTE = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date": {"type": "string"},
        "type": {"type": "string"},
        "transcript_segments": {"type": "array", "items": {"type": "object", "properties": {
            "speaker": {"type": "string"},
            "text": {"type": "string"},
        }, "required": ["text"]}},
    },
    "required": ["transcript_segments"],
}


# vLLM guided-decoding schema (LEGACY path — JS-shell pages where the deterministic extractor found no body). A
# basic_info block is a permissive object: `type` is the discriminator; md is used by md/list blocks; caption/headers/rows
# by table blocks — all optional so guided decoding can emit either shape without forcing empty fields.
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
    },
    "required": ["basic_info"],
}


def build_user(page_text: str, page_url: str, known: dict) -> str:
    """The user turn's TEXT part: the KNOWN event metadata (as reference — confirm & extend, don't blindly trust) +
    the page url + the page's reading-order content. The screenshot is attached separately as an image.
    `known` = the event_agent event {title,date,type,urls}. Links stay in their natural [anchor](url) markdown form —
    the Lnn reference-id rewrite existed only to make the model cheaply LIST new urls, and url discovery is gone."""
    ref = (f"KNOWN EVENT (reference — confirm & extend, do not blindly trust):\n"
           f"  title: {known.get('title','')}\n  date: {known.get('date','')}\n  type: {known.get('type','')}\n"
           f"  media urls already found: {known.get('urls', [])}")
    body = "PAGE CONTENT (reading order):\n" + page_text
    return f"PAGE URL: {page_url}\n\n{ref}\n\n{body}"
