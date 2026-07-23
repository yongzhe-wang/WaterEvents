"""media_agent.worker — THE ENRICHMENT WORKER (stage-2): turns `discovered` events into `enriched` events with basic_info.

用一句话讲完: 一个常驻进程,循环 { 从 events 表 SKIP-LOCKED claim 一批 `discovered` 事件 → 每个事件取它的详情页 url、
watercrawl 渲染+截图 → 调 media_agent.enrich.enrich_page 出 basic_info + 补全 media urls → 写回 status='enriched' },
队列空了退避退出。**它是 discovery worker 的 event 级镜像**:同一套 claim/lease/fencing/fail-loud,但工作单元是"一个
事件"而非"一家公司",所以一个巨型公司的上万事件被几十个 enrichment worker 自动分摊。

Flow (one event):
  claim (SKIP LOCKED, status→rendering + claim_token + lease) ──► pick detail url (first HTML, not PDF/mp3)
    ──► watercrawl.render_shot ──► enrich_page(known_event, page) ──► mark_enriched (basic_info + merged urls, fenced)
  render empty / enrich _error → fail_event (backoff+retry or dead_letter) — a failed event NEVER silently 'enriched'.

Run (on GCP, VLM on RunPod):
  WATEREVENTS_DB_DSN=... WATEREVENTS_DB_SCHEMA=waterevents QWEN_BASE_URLS=<runpod>/v1 python3 -m agent.media_agent.worker
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
import uuid

from providers import watercrawl
from providers.qwen_llm import QwenClient

from agent.event_agent import db                       # the SHARED WaterEvents DB layer (companies + events tables)
from .enrich import enrich_page                        # the stage-2 endpoint: (known_event + detail page) → enriched record

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
_EMPTY_BACKOFF_S = int(os.environ.get("WATEREVENTS_EMPTY_BACKOFF_S", "10"))
_MAX_IDLE_ROUNDS = int(os.environ.get("WATEREVENTS_MAX_IDLE_ROUNDS", "6"))
_USE_IMAGE = os.environ.get("EVENT_USE_IMAGE", "1") not in ("0", "false", "no")   # send the detail-page screenshot to the VL model
# a media ASSET (the thing itself), not the detail PAGE to render — we render the HTML detail page, not the pdf/audio.
_ASSET_RE = re.compile(r"\.(pdf|mp3|wav|m4a|zip|xlsx?|docx?|pptx?)(\?|#|$)", re.I)


def _detail_url(media_urls: list[str]) -> str | None:
    """Pick the event's DETAIL PAGE to render: the first http url that is NOT a media asset (pdf/mp3/…). The assets are
    recorded as urls but we don't render them — the HTML detail page is where basic_info + newly-linked media live."""
    for u in media_urls or []:
        if u.startswith("http") and not _ASSET_RE.search(u):
            return u
    return None


async def process_event(pool, client: QwenClient, ev) -> None:
    """Enrich ONE claimed event: render its detail page + VLM → basic_info, write back fenced. Every failure path is
    LOUD (fail_event with a reason) so a page we couldn't render / the VLM couldn't parse is NEVER marked enriched."""
    eid, tok = ev["id"], ev["claim_token"]
    media = ev["media_urls"] if isinstance(ev["media_urls"], list) else json.loads(ev["media_urls"] or "[]")
    known = {"title": ev["title"], "date": ev["event_date"], "type": ev["event_type"], "media_urls": media}
    url = _detail_url(media)
    if not url:                                          # only asset urls (pdf/mp3) → no HTML page to enrich from
        print(f"[enrich] ⛔ event {eid} has no HTML detail url (only assets) — fail", flush=True)
        await db.fail_event(pool, eid, tok, "no_html_detail_url")
        return
    r = await asyncio.to_thread(watercrawl.render_shot, url)   # render_shot is sync + marshals to the browser loop
    if not r.get("text") and not r.get("links"):        # walled/dead/empty → fail-loud, don't enrich a blank
        print(f"[enrich] ⛔ event {eid} detail render FAILED ({r.get('method','')!r}) {url[:60]} — fail", flush=True)
        await db.fail_event(pool, eid, tok, f"render_failed:{r.get('method','')}")
        return
    page = {"page_url": url, "page_text": r.get("inline") or r.get("text", ""),
            "image_b64": r.get("shot_b64") or None}
    enriched = await enrich_page(known, page, client=client, use_image=_USE_IMAGE)
    if enriched.get("_error"):                           # LLM hard-fail / truncation → fail-loud, never silent-enrich
        print(f"[enrich] ⛔ event {eid} VLM FAILED: {enriched['_error']} — fail", flush=True)
        await db.fail_event(pool, eid, tok, f"vlm:{enriched['_error']}")
        return
    # store basic_info + transcript as one JSON blob in the basic_info TEXT column; urls = enrich's merged known∪new
    blob = json.dumps({"basic_info": enriched.get("basic_info"),
                       "transcript_segments": enriched.get("transcript_segments")}, ensure_ascii=False)
    ok = await db.mark_enriched(pool, eid, tok, blob, enriched.get("urls") or media)
    n_blocks = len(enriched.get("basic_info") or [])
    print(f"[enrich] {'✅' if ok else '⚠️ lost-lease'} event {eid} → {n_blocks} basic_info blocks, "
          f"{len(enriched.get('urls') or media)} urls", flush=True)


async def worker_loop(pool) -> None:
    """Claim-batch → enrich-in-parallel → repeat until the discovered queue is drained (N idle rounds). One shared
    QwenClient keeps the RunPod connection warm across the whole run."""
    client = QwenClient()
    idle = 0
    while idle < _MAX_IDLE_ROUNDS:
        events = await db.claim_events(pool)             # a batch of discovered/retry-due events (SKIP LOCKED)
        if not events:
            idle += 1
            print(f"[enrich] queue empty ({idle}/{_MAX_IDLE_ROUNDS}) — backoff {_EMPTY_BACKOFF_S}s", flush=True)
            await asyncio.sleep(_EMPTY_BACKOFF_S)
            continue
        idle = 0
        print(f"[enrich] claimed {len(events)} events", flush=True)
        await asyncio.gather(*(process_event(pool, client, ev) for ev in events))   # enrich the batch in parallel
    print(f"[enrich] {_WORKER_ID} exiting — enrichment queue drained after {_MAX_IDLE_ROUNDS} idle rounds", flush=True)


async def main() -> None:
    print(f"[enrich] starting {_WORKER_ID}", flush=True)
    pool = await db.connect_pool()
    try:
        await worker_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
