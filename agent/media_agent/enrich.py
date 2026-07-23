"""media_agent.enrich — THE ENRICHMENT ENDPOINT: (a known event + its detail page) → the event's enriched record
{title,date,type, urls[], basic_info[], transcript_segments[]}. Mirrors agent/event_agent/extract.py in structure AND
in its truncation discipline — the smallest verifiable media component, no queue / DB / browser / close-loop.

用一句话讲完: 给 known_event {title,date,type,media_urls[]} + page {page_url,page_text,image_b64} → 拼一个 job(media
SYSTEM + known 作 reference 注入 + 图 + 强制 SCHEMA)→ providers.qwen_llm.QwenClient.send_many 并行发 → _normalize
把 model 的贡献 fill-and-append 成 enriched record(urls = known ∪ 页面新发现, basic_info 有序 block, 页面内联
transcript 分流进 transcript_segments)。**model 只吐"新发现"不回吐已知(fill-and-append)。传输在 provider,不改。**

NO CHUNKING (deleted 2026-07-23, mirror of event_agent/extract.py). The old chunk fallback went TEXT-ONLY and DROPPED
the screenshot; without the layout image the VL model under-extracts AND bags nav links as junk (coca-cola 119 events
WITH the shot → 32 without). Instead: send the FULL inline text + the screenshot in ONE call, sized to fit the server
context (--max-model-len 32768 for the generous 24000 cap; the screenshot is ALWAYS sent so a text trim loses no
layout). If output STILL truncates (finish_reason=='length', QwenClient透传为 __finish__) on an extreme mega-page →
FAIL LOUD: mark output_truncated=True + REPORT, keep the partial, NEVER silently split-and-lose-the-shot. {USER
2026-07-23 "remove the blocking logic and just scale the model output size if needed"}.
"""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urljoin

from providers.qwen_llm import QwenClient          # generic parallel sender, no media logic — reused unchanged

from . import prompts

# Input char caps — NO chunking (the chunk fallback was DELETED; see enrich_page). A screenshot page uses a GENEROUS
# text cap so a normal IR page is never trimmed, and the screenshot ALWAYS accompanies the text so any trim loses no
# layout. {EXTRACT.PY same de-chunk fix} [CONFIDENCE: CONFIRMED 100% — chunking went text-only, dropped the shot, lost events].
_MAX_INPUT_CHARS = int(os.environ.get("MEDIA_MAX_INPUT_CHARS", "48000"))
_VISION_TEXT_CHARS = int(os.environ.get("MEDIA_VISION_TEXT_CHARS", "24000"))

# Feed / asset urls that are never a real media item — dropped from the event's urls[] (mirror event_agent's filter).
_NONMEDIA_URL_RE = re.compile(
    r'\.(xml|rss|atom|css|js|png|jpe?g|gif|svg|ico|woff2?)(\?|#|$)'
    r'|/(rss|feeds?|atom|sitemap)(/|\.|\?|#|$)',
    re.I)


def _merge_urls(known_media: list, new_urls: list, page_url: str) -> list[str]:
    """The event's full media urls = known ∪ page-discovered, resolved absolute, feed/asset dropped, deduped (order:
    known first, then new). A relative/protocol-relative new_url is urljoin'd against the page; still-non-http dropped."""
    out, seen = [], set()
    for u in list(known_media or []) + list(new_urls or []):
        if not isinstance(u, str) or not u.strip():
            continue
        absu = urljoin(page_url, u.strip()) if not u.strip().lower().startswith("http") else u.strip()
        if not absu.lower().startswith(("http://", "https://")) or absu in seen or _NONMEDIA_URL_RE.search(absu):
            continue
        seen.add(absu)
        out.append(absu)
    return out


def _apply(reply: dict, acc: dict, known_event: dict, page_url: str) -> None:
    """FILL-AND-APPEND one model reply into the accumulator acc (used by both the single-call and chunked paths so
    they build the record identically). Metadata keeps the known non-empty value and only FILLS empties; basic_info
    blocks + transcript_segments are appended; new_urls are collected into acc['_new'] for a single merge at the end."""
    for f in ("title", "date", "type"):                       # fill ONLY an empty field from the page (trust known)
        if not acc[f] and (reply.get(f) or "").strip():
            acc[f] = (reply.get(f) or "").strip()
    for b in (reply.get("basic_info") or []):                 # ordered content blocks (md / list / table)
        if isinstance(b, dict) and b.get("type"):
            acc["basic_info"].append(b)
    for s in (reply.get("transcript_segments") or []):        # inline transcript → its OWN slot, never basic_info
        if isinstance(s, dict) and (s.get("text") or "").strip():
            acc["transcript_segments"].append(
                {"speaker": (str(s.get("speaker") or "").strip() or "SPEAKER_00"), "text": (s.get("text") or "").strip()})
    acc["_new"].extend(u for u in (reply.get("new_urls") or []) if isinstance(u, str) and u.strip())


def _finalize(acc: dict, known_event: dict, page_url: str) -> dict:
    """acc → the enriched record the endpoint returns: metadata + merged urls[] + basic_info[] + transcript_segments[]
    + output_truncated. The single place urls are merged (known ∪ new)."""
    return {
        "title": acc["title"], "date": acc["date"], "type": acc["type"],
        "urls": _merge_urls(known_event.get("media_urls"), acc["_new"], page_url),
        "basic_info": acc["basic_info"],
        "transcript_segments": acc["transcript_segments"],
        "output_truncated": acc["output_truncated"],         # TRUE ⇒ a block was cut even after chunking — DO NOT trust as complete
    }


def _new_acc(known_event: dict) -> dict:
    """A fresh accumulator seeded with the known metadata (so an all-empty page keeps the known title/date/type)."""
    return {"title": (known_event.get("title") or "").strip(), "date": (known_event.get("date") or "").strip(),
            "type": (known_event.get("type") or "").strip(),
            "basic_info": [], "transcript_segments": [], "_new": [], "output_truncated": False}


async def _vlm(client: QwenClient, page_text: str, page_url: str, image_b64, known_event: dict) -> dict:
    """One qwen-VL call → the raw contribution dict (carries __finish__ so the caller detects truncation)."""
    return await client.send_one(system=prompts.SYSTEM,
                                 user=prompts.build_user(page_text, page_url, known_event),
                                 image_b64=image_b64, guided_json=prompts.SCHEMA)


async def enrich_page(known_event: dict, page: dict, client: QwenClient | None = None, use_image: bool = True) -> dict:
    """ONE (known event + its detail page) → enriched record. page = {page_url, page_text, image_b64?}.

    NO CHUNKING (deleted 2026-07-23). The old chunk fallback went TEXT-ONLY and DROPPED the screenshot; without the
    layout image the VL model under-extracts AND bags nav links as junk — mirror of event_agent.extract's fix (coca-cola
    119 events WITH the shot → 32 without). Instead: send the FULL inline text + the screenshot in ONE call. page_text is
    trimmed only for a pathological page — and because the SCREENSHOT is ALWAYS sent, a text trim loses nothing visual.
    If output STILL truncates (finish=length) on an extreme mega-page → FAIL LOUD (mark output_truncated, report),
    NEVER silently split-and-lose-the-shot. {USER 2026-07-23 "remove the blocking logic and just scale the model output
    size if needed"} [CONFIDENCE: CONFIRMED 100% — extract.py req dumps proved the chunk path dropped the image + lost
    events]. Needs the server at --max-model-len 32768 for the generous 24000 cap; on a 16384 server set
    MEDIA_VISION_TEXT_CHARS=8000 (the screenshot still carries the layout). For many pages use enrich_pages (parallel)."""
    c = client or QwenClient()
    page_url = page.get("page_url", "")
    img = page.get("image_b64") if use_image else None
    # Generous cap — a normal IR page is never trimmed; a pathological one is trimmed but the screenshot still carries it.
    cap = _VISION_TEXT_CHARS if (use_image and img) else _MAX_INPUT_CHARS
    page_text = (page.get("page_text") or "")[:cap]
    acc = _new_acc(known_event)

    reply = await _vlm(c, page_text, page_url, img, known_event)
    # HARD FAILURE = an EMPTY dict {} OR a NON-EMPTY {"_error": ...} (send_one returns the latter after retries — a
    # truthy dict, so a bare `if not reply` MISSES it and the failure sails through to a silent "enriched"). Catch BOTH.
    # {CLIENT.PY hard-fail "return {\"_error\": ...}"; AUDIT 2026-07-23 CRITICAL: `if not reply` never fires on {\"_error\"}}
    # [CONFIDENCE: CONFIRMED 100% — mirror of extract.py's _error propagation; a failed VLM call must NEVER become enriched].
    if not reply or reply.get("_error"):
        acc["output_truncated"] = True                        # no usable reply ⇒ NOT complete — flag it (worker fails on this)
        print(f"[enrich] ⛔ no usable reply {page_url[:70]} — output_truncated=True "
              f"(err={reply.get('_error') if reply else 'empty'})", flush=True)
        return _finalize(acc, known_event, page_url)

    if reply.get("__finish__") == "length":                  # extreme mega-page overflowed the output cap → FAIL LOUD, no chunk
        acc["output_truncated"] = True
        print(f"[enrich] ⛔ OUTPUT TRUNCATED {page_url[:70]} — finish=length at MAX_TOKENS (mega page). REPORTED not chunked "
              f"— raise QWEN_MAX_TOKENS / --max-model-len. Partial content kept + flagged.", flush=True)

    _apply(reply, acc, known_event, page_url)                # apply whatever came back (partial-but-flagged if truncated)
    return _finalize(acc, known_event, page_url)


async def enrich_pages(items: list[dict], client: QwenClient | None = None, use_image: bool = True) -> list[dict]:
    """MANY (known_event, page) pairs → many enriched records, ALL in flight at once (the parallel endpoint). Each
    item = {"known_event":{...}, "page":{...}}. One record per item, order preserved. The global semaphore in
    QwenClient still bounds in-flight requests, so a big batch stays a full-but-bounded vLLM queue."""
    c = client or QwenClient()
    return await asyncio.gather(*(enrich_page(it["known_event"], it["page"], client=c, use_image=use_image) for it in items))
