"""event_agent.extract — THE ENDPOINT: (page text + optional screenshot + url) → build the event prompt → send to
Qwen IN PARALLEL (via the qwen_llm provider transport) → {events, routes}.

用一句话讲完: 给一批页面(每个 = url + 全文 text + 可选截图)→ 每页拼一个 job(event SYSTEM + user + 图 + 强制
schema)→ providers.qwen_llm.QwenClient.send_many 一次并行全发 → 拿回 {events, routes} → 确定性兜底(feed/资产 URL
regex 秒杀、去重、强制 event⊥route 互斥)。**event 逻辑在这里,传输在 provider —— 换模型/换 provider 不动这层。**
"""
from __future__ import annotations

import asyncio
import os
import re

from providers.qwen_llm import QwenClient          # the PROVIDER transport — generic parallel sender, no event logic

from . import prompts                              # the EVENT logic — instruction + schema

# NO CHUNKING. We DELETED the input/output "block-it-into-chunks" fallback — it caused MORE harm than the truncation it
# fought: the chunk path went TEXT-ONLY (dropped the screenshot), and without the layout screenshot the VL model can't
# tell global nav/footer from real events → it (a) bagged 49 apple.com/shop nav links into a junk metadata-less event
# AND (b) under-extracted (coca-cola 119 events with the shot → 32 without). {DEBUG 2026-07-23 apple: EVENT_VISION_TEXT_
# CHARS=8000 → input-over-cap → _extract_chunked text-only → req dump "image: present=False" → junk + lost events;
# first fetch (no chunk, full context) returned 399}. {USER 2026-07-23 "remove the blocking logic and just scale the model
# output size if needed"} [CONFIDENCE: CONFIRMED 100% — the req dumps show the exact image=False chunk that produced junk].
# The fix instead: SEND THE FULL inline text + the screenshot in ONE call, and SCALE the server (--max-model-len 32768) +
# output (QWEN_MAX_TOKENS) so it fits. If output STILL truncates on an extreme mega-list page → FAIL LOUD (report it, mark
# _error), never silently split-and-lose-the-shot.
MAX_INPUT_CHARS = int(os.environ.get("EVENT_MAX_INPUT_CHARS", "48000"))   # text-only pages: full budget
# Vision pages: a high safety cap so a truly pathological page can't blow past even a 32768 ctx — but generous enough
# that a normal rich IR page (coca-cola/apple ≈ 11-15k inline chars) is NEVER trimmed → its screenshot is ALWAYS kept.
# {DEBUG: 8000 was catastrophically low — it trimmed normal pages into the shot-dropping chunk path}. Requires the server
# at --max-model-len 32768 (image ~1.5k tok + this ~7k + system ~1k + output ≤8k ≈ 17.5k < 32768).
_VISION_TEXT_CHARS = int(os.environ.get("EVENT_VISION_TEXT_CHARS", "24000"))

# Deterministic non-event URL filter — the structural traps (feeds / asset stores). Cheaper + more reliable than any
# model, so we strip them post-hoc even though the prompt forbids them too (belt + suspenders).
_NONEVENT_URL_RE = re.compile(
    r'\.(xml|rss|atom|css|js|png|jpe?g|gif|svg|ico|woff2?)(\?|#|$)'
    r'|/(rss|feeds?|atom|sitemap)(/|\.|\?|#|$)'
    r'|/content/dam/|/sites/[^/]+/files/|/media/documents?/',
    re.I)


# The page_text feeds links INLINE as `[anchor](url)`, and the model SOMETIMES copies that whole markdown wrapper into
# a url field instead of the bare url — e.g. "[Webcast](https://...)". A bare startswith("http") check then rejects it,
# the event loses all its urls, and _normalize DROPS the event → 0 events for a page the model actually read correctly.
# {DEBUG 2026-07-23 tests/output/www_microsoft_com.txt: model emitted "urls":["[Webcast](https://.../earnings-fy-2026-q4)"]
# → _clean_urls dropped all → 0 events, while Coca-Cola (bare urls) got 120}. [CONFIDENCE: CONFIRMED 100% — Microsoft +
# Apple debug dumps show good events lost purely to the markdown wrapper]. Unwrap it before the http check.
_MD_LINK_RE = re.compile(r'\[[^\]]*\]\((https?://[^)\s]+)\)')


def _unwrap_url(u: str) -> str:
    """If u is a markdown link `[text](http…)` return just the url; else return u stripped. Belt for the model copying
    the inline `[anchor](url)` form verbatim into a url/route field."""
    u = (u or "").strip()
    m = _MD_LINK_RE.match(u)
    return m.group(1) if m else u


def _clean_urls(raw: list) -> list[str]:
    """http(s) only, drop structural junk, dedup — preserving order. Used for an event's `urls` list. Unwraps a markdown
    `[text](url)` wrapper first (the model sometimes emits the inline link verbatim → would otherwise drop the event)."""
    out, seen = [], set()
    for u in raw or []:
        u = _unwrap_url(u)
        if u.startswith("http") and u not in seen and not _NONEVENT_URL_RE.search(u):
            seen.add(u)
            out.append(u)
    return out


def _normalize(result: dict) -> dict:
    """Model reply → {"events":[...], "routes":[...]} with the guarantees the caller relies on: every event has ≥1
    clean url; and `routes` is a FLAT list of go-deeper url strings — deduped, feed/asset urls dropped, and the HARD
    exclusivity kept (a url inside some event's `urls` NEVER also appears in routes; an event is a leaf)."""
    events, event_urls = [], set()
    for e in (result.get("events") or []):
        urls = _clean_urls(e.get("urls") or [])
        if not urls:                                          # an event must have at least one real url, else drop it
            continue
        events.append({"title": (e.get("title") or "").strip()[:300],
                       "date": (e.get("date") or "").strip(),
                       "type": (e.get("type") or "").strip(),
                       "urls": urls})
        event_urls.update(urls)
    routes, seen = [], set()
    for r in (result.get("routes") or []):
        # routes are now plain url strings (SCHEMA dropped the old {url,go_deeper} object). Stay defensive: a no-schema
        # retry could still emit the old {"url":...} shape, so accept both. {USER 2026-07-23 "just keep a list of urls
        # go deeper"} [CONFIDENCE: CONFIRMED 100% — direct user instruction to drop the go_deeper field].
        u = _unwrap_url(r if isinstance(r, str) else (r.get("url") or ""))   # routes can be markdown-wrapped too
        if not u.startswith("http") or u in seen or u in event_urls:   # EXCLUSIVE: an event's url is never a route
            continue
        if _NONEVENT_URL_RE.search(u):                        # a feed/asset is never worth crawling deeper — drop it
            continue
        seen.add(u)
        routes.append(u)                                      # every url in `routes` is a go-deeper target
    out = {"events": events, "routes": routes}
    # Propagate a transport HARD-failure marker (server down / network drop / retries exhausted) so the crawl can tell
    # "extraction FAILED on this page" apart from "page genuinely had 0 events" — both otherwise collapse to events:[].
    # {USER 2026-07-23 "fail loudly ... we dont want quality issue"} [CONFIDENCE: CONFIRMED 100% — directive].
    if result.get("_error"):
        out["_error"] = result["_error"]
    return out


def _text_cap(page: dict, use_image: bool) -> int:
    """The char cap _job applies to page_text: smaller WITH an image (the image carries layout AND its tokens are
    scarce), the full budget text-only. One source of truth so the over-input check and _job agree on the boundary."""
    has_img = bool(use_image and page.get("image_b64"))
    return _VISION_TEXT_CHARS if has_img else MAX_INPUT_CHARS


def _job(page: dict, use_image: bool) -> dict:
    """Build ONE client job from a page dict {page_url, page_text, image_b64?, links_block?}. Attaches the screenshot
    only when use_image (a Qwen-VL model is served)."""
    img = page.get("image_b64") if use_image else None
    # WITH a screenshot the model READS the page visually, so we must NOT also dump the full page_text — a tall page's
    # image tokens + 48k-char text blew past the VL context (E2E 2026-07-23: input 16982 > 16384). Send a SHORT text
    # snippet + the links_block (the exact urls the model can't read off pixels). Text-only jobs keep the full budget.
    text_cap = _text_cap(page, use_image)
    text = page.get("page_text") or ""
    if len(text) > text_cap:                                  # SILENT-CUT GUARD: never trim page text without saying so.
        # No chunking anymore — the cap is a high SAFETY bound (a normal IR page is well under it). Hitting it means a
        # genuinely pathological page; log LOUDLY so it's visible, and the fix is to raise EVENT_VISION_TEXT_CHARS +
        # --max-model-len, NOT to split (splitting drops the screenshot → junk). {USER 2026-07-23 "just scale ... if needed"}.
        print(f"[extract] ⚠️ INPUT-CUT {page.get('page_url','')[:70]} — page_text {len(text)} chars > {text_cap} cap, "
              f"dropping tail {len(text) - text_cap} chars — RAISE EVENT_VISION_TEXT_CHARS + --max-model-len", flush=True)
    return {
        "system": prompts.SYSTEM,
        "user": prompts.build_user(text[:text_cap], page.get("page_url", ""), page.get("links_block", "")),
        "image_b64": img,
        "guided_json": prompts.SCHEMA,
    }


# _split_blocks + _extract_chunked DELETED 2026-07-23 — the chunk fallback dropped the screenshot (text-only) and so
# made the VL model bag nav into junk events + under-extract. Replaced by "send full page + scale the server" (see the
# NO-CHUNKING note atop this file). {USER "remove the blocking logic and just scale the model output size if needed"}.


async def _extract_one(page: dict, c: QwenClient, use_image: bool) -> dict:
    """ONE page → normalized {events, routes}. NO CHUNKING: send the FULL inline text + the screenshot in ONE call. The
    screenshot is the VL model's PRIMARY signal for telling nav/footer from real events — the old chunk path dropped it
    (text-only) and the model then bagged nav links into junk events + under-extracted. If the OUTPUT still truncates
    (finish_reason=length, only on an extreme mega-list page) → FAIL LOUD: mark _error so the crawl reports the page
    INCOMPLETE, never a silent partial. The fit strategy is SCALING (--max-model-len 32768 + QWEN_MAX_TOKENS), not
    splitting. {USER 2026-07-23 "remove the blocking logic and just scale the model output size if needed"}."""
    res = (await c.send_many([_job(page, use_image)]))[0]
    if res.get("__finish__") == "length":                    # output cut → the output/context budget was too small here
        n = len(res.get("events") or [])
        print(f"[extract] ⛔ OUTPUT TRUNCATED {page.get('page_url','')[:70]} — finish_reason=length ({n} events before "
              f"cut). RAISE --max-model-len / QWEN_MAX_TOKENS. Marking INCOMPLETE (no silent partial).", flush=True)
        res["_error"] = "output_truncated: finish_reason=length — page needs a larger output budget"
    return _normalize(res)


async def extract_page(page: dict, client: QwenClient | None = None, use_image: bool = False) -> dict:
    """ONE page → {events, routes}. For many pages use extract_pages (true parallel). Both truncation twins handled."""
    return await _extract_one(page, client or QwenClient(), use_image)


async def extract_pages(pages: list[dict], client: QwenClient | None = None, use_image: bool = False) -> list[dict]:
    """MANY pages → many {events, routes}, ALL in flight at once (the parallel endpoint). `pages` = list of
    {page_url, page_text, image_b64?, links_block?}. One normalized result per page, order preserved. use_image=True
    only with a served Qwen-VL model (a text model would reject the image). Each page is sent in ONE call (full text +
    screenshot, no chunking); output-truncation fails loud. The global semaphore still bounds in-flight requests."""
    c = client or QwenClient()
    return await asyncio.gather(*(_extract_one(p, c, use_image) for p in pages))
