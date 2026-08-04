"""media_agent.enrich — THE ENRICHMENT ENDPOINT: (a known event + its detail page) → the event's enriched record
{title,date,type, urls[], basic_info[], transcript_segments[]}. Mirrors agent/event_agent/extract.py in structure AND
in its truncation discipline — the smallest verifiable media component, no queue / DB / browser / close-loop.

用一句话讲完: 给 known_event {title,date,type,media_urls[]} + page {page_url,page_text,image_b64} → 拼一个 job(media
SYSTEM + known 作 reference 注入 + 图 + 强制 SCHEMA)→ providers.qwen_llm.QwenClient.send_many 并行发 → _normalize
把 model 的贡献 fill-and-append 成 enriched record(urls = known ∪ 页面新发现, basic_info 有序 block, 页面内联
transcript 分流进 transcript_segments)。**model 只吐"新发现"不回吐已知(fill-and-append)。传输在 provider,不改。**

CHUNKING lives in handlers.py (`_split_blocks` / `_chunked_html` — the OUTPUT-truncation fallback), NOT here: enrich_page
sends the FULL inline text + the screenshot in ONE call, sized to fit the server context (--max-model-len 32768, generous
24000 cap; the screenshot is ALWAYS sent so a text trim loses no layout). WHY enrich itself doesn't input-chunk: media is
VISION-mode (the screenshot carries chart/table/transcript layout), so it is NOT subject to the ~15-row text-only collapse
that made event_agent re-add input chunking. If output STILL truncates (finish_reason=='length', QwenClient 透传为
__finish__) handlers.py splits + retries; an unrecoverable mega-page is marked output_truncated=True + REPORTED (fail loud).
"""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urljoin

from providers.qwen_llm import QwenClient          # generic parallel sender, no media logic — reused unchanged
from providers.qwen_llm import config as _qcfg     # legacy-mode output budget (config.MAX_TOKENS) for the input-fit guard

from ..extract import prompts
# DETERMINISTIC body + the shared input-overflow guard (single source in extract_html — handlers.py imports the same).
from ..extract.extract_html import extract_html, suppress_transcript_blocks, fit_input, ROUTE_MAX_TOKENS

# Input char caps — enrich_page does NOT input-chunk (media is VISION-mode; the screenshot carries layout, so no text-only
# collapse). A screenshot page uses a GENEROUS text cap so a normal IR page is never trimmed, and the screenshot ALWAYS
# accompanies the text so any trim loses no layout. handlers.py holds the OUTPUT-truncation chunk fallback (`_chunked_html`).
_MAX_INPUT_CHARS = int(os.environ.get("MEDIA_MAX_INPUT_CHARS", "48000"))
_VISION_TEXT_CHARS = int(os.environ.get("MEDIA_VISION_TEXT_CHARS", "24000"))
# ROUTE_MAX_TOKENS + fit_input live in extract_html (shared with handlers.py) so both html paths size input identically.

# Feed / asset urls that are never a real media item — dropped from the event's urls[] (mirror event_agent's filter).
_NONMEDIA_URL_RE = re.compile(
    r'\.(xml|rss|atom|css|js|png|jpe?g|gif|svg|ico|woff2?)(\?|#|$)'
    r'|/(rss|feeds?|atom|sitemap)(/|\.|\?|#|$)',
    re.I)


def _clean_urls(known_media: list, page_url: str) -> list[str]:
    """The event's media urls = EXACTLY the list event_agent handed us, resolved absolute, feed/asset dropped, deduped.

    用一句话讲完: 不再做 known ∪ page-discovered 的并集 —— URL 集合由 event_agent 一次性给定,media_agent 只做规范化
    (相对路径 urljoin、去 feed/asset、去重),不增不减。{USER 2026-08-03 "let's just use the original list from the
    event agent"} [CONFIDENCE: CONFIRMED 100% — direct user directive; the frontier-growth path was deleted with it]."""
    out, seen = [], set()
    for u in list(known_media or []):
        if not isinstance(u, str) or not u.strip():
            continue
        absu = urljoin(page_url, u.strip()) if not u.strip().lower().startswith("http") else u.strip()
        if not absu.lower().startswith(("http://", "https://")) or absu in seen or _NONMEDIA_URL_RE.search(absu):
            continue
        seen.add(absu)
        out.append(absu)
    return out


def _apply(reply: dict, acc: dict, known_event: dict, page_url: str, skip_basic_info: bool = False) -> None:
    """FILL-AND-APPEND one model reply into the accumulator acc. Metadata keeps the known non-empty value and only
    FILLS empties; basic_info blocks + transcript_segments are appended.

    skip_basic_info=True in ROUTE mode: the deterministic extractor (extract_html) already filled acc['basic_info'], so the
    VLM's schema has NO basic_info field — we take ONLY its metadata + transcript. {DESIGN wf_7b61c8d0 STAGE 9}."""
    for f in ("title", "date", "type"):                       # fill ONLY an empty field from the page (trust known)
        if not acc[f] and (reply.get(f) or "").strip():
            acc[f] = (reply.get(f) or "").strip()
    if not skip_basic_info:                                    # LEGACY path only — ROUTE mode's body comes from extract_html
        for b in (reply.get("basic_info") or []):             # ordered content blocks (md / list / table)
            if isinstance(b, dict) and b.get("type"):
                acc["basic_info"].append(b)
    for s in (reply.get("transcript_segments") or []):        # inline transcript → its OWN slot, never basic_info
        if isinstance(s, dict) and (s.get("text") or "").strip():
            acc["transcript_segments"].append(
                {"speaker": (str(s.get("speaker") or "").strip() or "SPEAKER_00"), "text": (s.get("text") or "").strip()})


def _finalize(acc: dict, known_event: dict, page_url: str) -> dict:
    """acc → the enriched record the endpoint returns: metadata + urls[] + basic_info[] + transcript_segments[]
    + output_truncated. urls[] is event_agent's own list, normalized — nothing is discovered or added here."""
    return {
        "title": acc["title"], "date": acc["date"], "type": acc["type"],
        "urls": _clean_urls(known_event.get("media_urls"), page_url),
        "basic_info": acc["basic_info"],
        "transcript_segments": acc["transcript_segments"],
        "output_truncated": acc["output_truncated"],         # TRUE ⇒ a block was cut even after chunking — DO NOT trust as complete
    }


def _new_acc(known_event: dict) -> dict:
    """A fresh accumulator seeded with the known metadata (so an all-empty page keeps the known title/date/type)."""
    return {"title": (known_event.get("title") or "").strip(), "date": (known_event.get("date") or "").strip(),
            "type": (known_event.get("type") or "").strip(),
            "basic_info": [], "transcript_segments": [], "output_truncated": False}


async def _vlm(client: QwenClient, page_text: str, page_url: str, image_b64, known_event: dict,
               system: str | None = None, schema: dict | None = None, max_tokens: int | None = None) -> dict:
    """One qwen-VL call → the raw contribution dict (carries __finish__ so the caller detects truncation). system/schema
    default to the LEGACY full-copy prompt; ROUTE mode passes SYSTEM_ROUTE/SCHEMA_ROUTE (no basic_info → no overflow) and a
    small max_tokens (its output is tiny) so input+max stays under the context."""
    return await client.send_one(system=system or prompts.SYSTEM,
                                 user=prompts.build_user(page_text, page_url, known_event),
                                 image_b64=image_b64, guided_json=schema or prompts.SCHEMA, max_tokens=max_tokens)


async def enrich_page(known_event: dict, page: dict, client: QwenClient | None = None, use_image: bool = True) -> dict:
    """ONE (known event + its detail page) → enriched record. page = {page_url, page_text, image_b64?}.

    enrich_page does NOT input-chunk: media is VISION-mode (the screenshot carries chart/table/transcript layout), so it
    is NOT subject to the text-only collapse that made event_agent re-add chunking. It sends the FULL inline text + the
    screenshot in ONE call. page_text is trimmed only for a pathological page — and because the SCREENSHOT is ALWAYS sent,
    a text trim loses nothing visual. If output STILL truncates (finish=length) on an extreme mega-page → handlers.py's
    `_chunked_html` retries; an unrecoverable one is marked output_truncated + REPORTED (fail loud, never silent partial).
    Needs the server at --max-model-len 32768 for the generous 24000 cap; on a 16384 server set
    MEDIA_VISION_TEXT_CHARS=8000 (the screenshot still carries the layout). For many pages use enrich_pages (parallel)."""
    c = client or QwenClient()
    page_url = page.get("page_url", "")
    img = page.get("image_b64") if use_image else None
    # Generous cap — a normal IR page is never trimmed; a pathological one is trimmed but the screenshot still carries it.
    cap = _VISION_TEXT_CHARS if (use_image and img) else _MAX_INPUT_CHARS
    # Links stay in their natural [anchor](url) form. The Lnn reference-id rewrite that used to happen here existed ONLY
    # so the model could cheaply LIST discovered urls; url discovery is gone, so the rewrite is dead weight.
    raw_text = page.get("page_text") or ""
    acc = _new_acc(known_event)

    # ── DETERMINISTIC body (STAGE 2-6): trafilatura + pandas own basic_info so the VLM never copies (and overflows on) the
    # financial tables. tier=='empty' ⇒ JS-shell/thin page with no deterministic body → fall back to the LEGACY full-copy VLM
    # (recall over overflow-safety on the rare shell). {DESIGN wf_7b61c8d0} [CONFIDENCE: CONFIRMED — html present ⇒ det body].
    html = page.get("html") or ""
    det = extract_html(html, base_url=page_url, links=page.get("links"), thin=bool(page.get("thin")))
    route = det["tier"] != "empty"                            # True ⇒ deterministic body exists → shrink the VLM to ROUTE mode
    if route:
        acc["basic_info"] = list(det["blocks"])              # deterministic reading-order blocks ARE the body (final, real urls)

    # Size the VLM input to the mode's OUTPUT budget so input_tokens + max_tokens < context → the vLLM 400 pre-reject is
    # impossible (a .pdf rendered to 20769 input tokens + 12000 output = 400; ROUTE requests only 6000 and _fit_input caps
    # input to match). ROUTE mode's body is already deterministic, so a trimmed input only costs some url-routing reach.
    out_tokens = ROUTE_MAX_TOKENS if route else _qcfg.MAX_TOKENS
    page_text = fit_input(raw_text[:cap], out_tokens)     # (a) vision/text cap, then (b) context-fit against out_tokens

    # In ROUTE mode the VLM only confirms metadata + parses transcript + lists media ids → output is tiny, cannot overflow.
    # In LEGACY (empty tier) mode it does the old full copy from text/screenshot.
    reply = await _vlm(c, page_text, page_url, img, known_event,
                       system=prompts.SYSTEM_ROUTE if route else None,
                       schema=prompts.SCHEMA_ROUTE if route else None,
                       max_tokens=ROUTE_MAX_TOKENS if route else None)
    # HARD FAILURE = an EMPTY dict {} OR a NON-EMPTY {"_error": ...} (send_one returns the latter after retries — a
    # truthy dict, so a bare `if not reply` MISSES it and the failure sails through to a silent "enriched"). Catch BOTH.
    # {CLIENT.PY hard-fail "return {\"_error\": ...}"; AUDIT 2026-07-23 CRITICAL: `if not reply` never fires on {\"_error\"}}
    # [CONFIDENCE: CONFIRMED 100% — mirror of extract.py's _error propagation; a failed VLM call must NEVER become enriched].
    if not reply or reply.get("_error"):
        # ROUTE mode already has the deterministic body in acc — a failed VLM only loses metadata/transcript/urls, NOT the
        # content. Still flag truncated so the worker knows the media routing is incomplete. {DESIGN edge "VLM-fails-in-route"}.
        acc["output_truncated"] = True                        # no usable reply ⇒ NOT complete — flag it (worker fails on this)
        print(f"[enrich] ⛔ no usable reply {page_url[:70]} — output_truncated=True "
              f"(tier={det['tier']} err={reply.get('_error') if reply else 'empty'})", flush=True)
        return _finalize(acc, known_event, page_url)

    if reply.get("__finish__") == "length":                  # extreme mega-page overflowed the output cap → FAIL LOUD, no chunk
        acc["output_truncated"] = True
        print(f"[enrich] ⛔ OUTPUT TRUNCATED {page_url[:70]} — finish=length at MAX_TOKENS (mega page). REPORTED not chunked "
              f"— raise QWEN_MAX_TOKENS / --max-model-len. Partial content kept + flagged.", flush=True)

    _apply(reply, acc, known_event, page_url, skip_basic_info=route)   # ROUTE: skip basic_info (det owns it)
    if route:                                                 # STAGE 8: drop a det transcript-flagged block ONLY where the VLM
        acc["basic_info"] = suppress_transcript_blocks(       # actually routed it to transcript_segments (no double-listing,
            acc["basic_info"], det["transcript_idx"], acc["transcript_segments"])   # uncovered flagged block STAYS as body)
    return _finalize(acc, known_event, page_url)


async def enrich_pages(items: list[dict], client: QwenClient | None = None, use_image: bool = True) -> list[dict]:
    """MANY (known_event, page) pairs → many enriched records, ALL in flight at once (the parallel endpoint). Each
    item = {"known_event":{...}, "page":{...}}. One record per item, order preserved. The global semaphore in
    QwenClient still bounds in-flight requests, so a big batch stays a full-but-bounded vLLM queue."""
    c = client or QwenClient()
    return await asyncio.gather(*(enrich_page(it["known_event"], it["page"], client=c, use_image=use_image) for it in items))
