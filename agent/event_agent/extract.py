"""event_agent.extract — THE ENDPOINT: (page text + optional screenshot + url) → build the event prompt → send to
Qwen IN PARALLEL (via the qwen_llm provider transport) → {events, routes}.

用一句话讲完: 给一批页面(每个 = url + 全文 text + 可选截图)→ 每页拼一个 job(event SYSTEM + user + 图 + 强制
schema)→ providers.qwen_llm.QwenClient.send_many 一次并行全发 → 拿回 {events, routes} → 确定性兜底(feed/资产 URL
regex 秒杀、去重、强制 event⊥route 互斥)。**event 逻辑在这里,传输在 provider —— 换模型/换 provider 不动这层。**
"""
from __future__ import annotations

import os
import re

from providers.qwen_llm import QwenClient          # the PROVIDER transport — generic parallel sender, no event logic

from . import prompts                              # the EVENT logic — instruction + schema

# How much page text to send to the model (truncate huge pages). Event-agent's call, not the transport's.
MAX_INPUT_CHARS = int(os.environ.get("EVENT_MAX_INPUT_CHARS", "48000"))   # ~12-16k tokens

# Deterministic non-event URL filter — the structural traps (feeds / asset stores). Cheaper + more reliable than any
# model, so we strip them post-hoc even though the prompt forbids them too (belt + suspenders).
_NONEVENT_URL_RE = re.compile(
    r'\.(xml|rss|atom|css|js|png|jpe?g|gif|svg|ico|woff2?)(\?|#|$)'
    r'|/(rss|feeds?|atom|sitemap)(/|\.|\?|#|$)'
    r'|/content/dam/|/sites/[^/]+/files/|/media/documents?/',
    re.I)


def _clean_urls(raw: list) -> list[str]:
    """http(s) only, drop structural junk, dedup — preserving order. Used for an event's `urls` list."""
    out, seen = [], set()
    for u in raw or []:
        u = (u or "").strip()
        if u.startswith("http") and u not in seen and not _NONEVENT_URL_RE.search(u):
            seen.add(u)
            out.append(u)
    return out


def _normalize(result: dict) -> dict:
    """Model reply → {"events":[...], "routes":[...]} with the guarantees the caller relies on: every event has ≥1
    clean url; feed/asset urls never survive; routes deduped; and the HARD exclusivity — a url that belongs to an
    event NEVER also appears in routes (event ⊥ go_deeper)."""
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
        u = (r.get("url") or "").strip()
        if not u.startswith("http") or u in seen or u in event_urls:   # EXCLUSIVE: an event's url is never a route
            continue
        seen.add(u)
        go_deeper = bool(r.get("go_deeper"))
        if _NONEVENT_URL_RE.search(u):                        # a feed/asset is never worth crawling deeper
            go_deeper = False
        routes.append({"url": u, "go_deeper": go_deeper})
    return {"events": events, "routes": routes}


def _job(page: dict, use_image: bool) -> dict:
    """Build ONE client job from a page dict {page_url, page_text, image_b64?, links_block?}. Truncates an oversized
    page. Attaches the screenshot only when use_image (i.e. a Qwen-VL model is served)."""
    return {
        "system": prompts.SYSTEM,
        "user": prompts.build_user((page.get("page_text") or "")[:MAX_INPUT_CHARS],
                                   page.get("page_url", ""), page.get("links_block", "")),
        "image_b64": page.get("image_b64") if use_image else None,
        "guided_json": prompts.SCHEMA,
    }


async def extract_page(page: dict, client: QwenClient | None = None, use_image: bool = False) -> dict:
    """ONE page → {events, routes}. For many pages use extract_pages (true parallel)."""
    c = client or QwenClient()
    res = (await c.send_many([_job(page, use_image)]))[0]
    return _normalize(res)


async def extract_pages(pages: list[dict], client: QwenClient | None = None, use_image: bool = False) -> list[dict]:
    """MANY pages → many {events, routes}, ALL in flight at once (the parallel endpoint). `pages` = list of
    {page_url, page_text, image_b64?, links_block?}. One normalized result per page, order preserved. use_image=True
    only with a served Qwen-VL model (a text model would reject the image)."""
    c = client or QwenClient()
    jobs = [_job(p, use_image) for p in pages]
    results = await c.send_many(jobs)
    return [_normalize(r) for r in results]
