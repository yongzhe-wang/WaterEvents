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
import uuid

from providers.qwen_llm import QwenClient

from agent.event_agent.storage import events as db                       # the SHARED WaterEvents DB layer (companies + events tables, claim/fail)
from ..storage import db_media                                 # media_agent's normalized-schema writer (content_blocks/transcript/url ledger)
from .enrich import enrich_page                        # the stage-2 endpoint: (known_event + detail page) → enriched record
# render via the bounded-retry wrapper, NOT bare watercrawl.render_shot — one empty render is a timeout, not a dead page.
from .render_retry import render_with_retry

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

# Escaped-failure counters. `_STATS["fail_event_failed"]` was already being INCREMENTED in process_event's innermost
# handler, but the dict was never defined and nothing ever printed it — `ruff --select F821` flagged it as an undefined
# name, i.e. the counter bumped only to raise NameError inside an except block whose whole purpose was not to raise.
# Defining it here and reporting it below is what makes the comment at that increment site ("surfaced in the per-batch
# + exit summary lines below") actually true.
# {RUFF 2026-07-28 "MEDIA_AGENT/PIPELINE/WORKER.PY:141 F821 UNDEFINED NAME `_STATS`"}
# [CONFIDENCE: CONFIRMED 100% — the name had exactly one occurrence in the file, the increment itself.]
_STATS: dict[str, int] = {
    "fail_event_failed": 0,     # fail_event ITSELF raised → the row stays 'rendering' until its lease lapses
    "escaped": 0,               # an exception got past process_event's own try/except and was caught by gather
}
_EMPTY_BACKOFF_S = int(os.environ.get("WATEREVENTS_EMPTY_BACKOFF_S", "10"))
_MAX_IDLE_ROUNDS = int(os.environ.get("WATEREVENTS_MAX_IDLE_ROUNDS", "6"))
# media wants VISION — the detail-page SCREENSHOT carries chart/table/transcript LAYOUT that text alone loses. But whether
# render even PRODUCES a screenshot is decided by the SHARED WATERCRAWL_NO_SHOT switch (event_agent defaults it ON = no
# shot). So couple use_image to that ONE switch: else use_image=True while render (NO_SHOT=1) hands back an EMPTY shot →
# media SILENTLY degrades to text-only and never knows. To run media in vision mode, launch its worker with
# WATERCRAWL_NO_SHOT=0 (render takes the shot → use_image=True). {port of event_agent's one-switch fix; media is vision-mode}
# [CONFIDENCE: CONFIRMED — worker.py read EVENT_USE_IMAGE while render read WATERCRAWL_NO_SHOT → the disconnect this fixes].
_NO_SHOT = os.environ.get("WATERCRAWL_NO_SHOT", "1") in ("1", "true", "yes")
_USE_IMAGE = not _NO_SHOT                                                          # never claim vision when render gives no shot
# a media ASSET (the thing itself), not the detail PAGE to render — we render the HTML detail page, not the pdf/audio.
_ASSET_RE = re.compile(r"\.(pdf|mp3|wav|m4a|zip|xlsx?|docx?|pptx?)(\?|#|$)", re.I)


def _as_list(v) -> list:
    """asyncpg returns a jsonb column as a Python list already — but stay robust to a str (double-encoded) or dirty
    non-list data (a dict), which would make a bare json.loads(dict) raise TypeError and crash the worker. {AUDIT
    2026-07-23: media_urls dict → json.loads(dict) TypeError}. Anything that isn't a clean list → []."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except Exception:                                # noqa: BLE001 — malformed json → empty, never crash
            return []
    return []


def _detail_url(media_urls: list[str]) -> str | None:
    """Pick the event's DETAIL PAGE to render: the first http url that is NOT a media asset (pdf/mp3/…). The assets are
    recorded as urls but we don't render them — the HTML detail page is where basic_info + newly-linked media live."""
    for u in media_urls or []:
        if isinstance(u, str) and u.lower().startswith("http") and not _ASSET_RE.search(u):
            return u
    return None


async def process_event(pool, client: QwenClient, ev) -> None:
    """Enrich ONE claimed event: render its detail page + VLM → basic_info, write back fenced. Every failure path is
    LOUD (fail_event with a reason) so a page we couldn't render / the VLM couldn't parse is NEVER marked enriched. The
    WHOLE body is wrapped so an UNEXPECTED exception (mark_enriched conn error / bad data) fails JUST this event via
    fail_event — it must NOT propagate out of gather and sink the batch (leaving the batch's rows stuck in 'rendering'
    until lease-expiry). {AUDIT 2026-07-23 HIGH: gather(return_exceptions=False) + no try/except}."""
    eid, tok = ev["id"], ev["claim_token"]
    try:
        media = _as_list(ev["media_urls"])
        known = {"title": ev["title"], "date": ev["event_date"], "type": ev["event_type"], "media_urls": media}
        url = _detail_url(media)
        if not url:                                          # only asset urls (pdf/mp3) → no HTML page to enrich from
            print(f"[enrich] ⛔ event {eid} has no HTML detail url (only assets) — fail", flush=True)
            await db.fail_event(pool, eid, tok, "no_html_detail_url")
            return
        # BOUNDED RETRY (was a bare single-shot render_shot). Under fleet concurrency a heavy IR page times out and comes
        # back EMPTY, while the same page renders fine alone — so a single empty render is NOT evidence the page is dead.
        # With fail_event's 3-strike escalation, the old single shot converted transient timeouts into permanent dead
        # letters. {ENGINE.PY:191-194 "RETRIES AN EMPTY RENDER UP TO _RENDER_TRIES TIMES WITH A SHORT BACKOFF: A
        # LOAD-INDUCED TIMEOUTERROR COMES BACK EMPTY, AND A RETRY ONCE THE BROWSER POOL HAS FREED UP USUALLY LANDS THE
        # PAGE."} [CONFIDENCE: CONFIRMED 100% — same render stack, same IR hosts, same concurrency as the event stage.]
        r = await render_with_retry(url)                    # render_shot is sync + marshals to the browser loop (in-thread)
        if not r.get("text") and not r.get("links"):        # STILL empty after every attempt → walled/dead, fail-loud
            print(f"[enrich] ⛔ event {eid} detail render FAILED after retries ({r.get('method','')!r}) {url[:60]} — fail", flush=True)
            await db.fail_event(pool, eid, tok, f"render_failed:{r.get('method','')}")
            return
        # PASS THE RAW HTML + LINKS THROUGH. enrich_page reads page["html"] to run the DETERMINISTIC body extractor
        # (trafilatura + pandas) and only falls back to the LEGACY full-copy VLM when that yields tier=='empty'
        # {ENRICH.PY:139-141 "HTML = PAGE.GET(\"HTML\") OR \"\" ... ROUTE = DET[\"TIER\"] != \"EMPTY\""}. This dict
        # previously carried only page_url/page_text/image_b64, so html was ALWAYS "" ⇒ extract_html's own guard
        # {EXTRACT_HTML.PY:235 "IF THIN OR NOT (HTML OR \"\").STRIP(): RETURN {... \"TIER\": \"EMPTY\"}"} forced tier
        # 'empty' on EVERY page ⇒ ROUTE mode was structurally unreachable in production and every event went down the
        # full-copy path that the ROUTE design exists to avoid (the one that overflows on earnings tables →
        # output_truncated=True → fail_event, never enriched). render_shot does return html {RENDER.PY:359 "{\"text\",
        # \"links\", \"html\", \"SHOT_B64\", \"METHOD\", \"INLINE\"}"} — it was simply dropped here.
        # [CONFIDENCE: CONFIRMED 100% — render_shot's documented return keys vs the keys this dict actually forwarded.]
        page = {"page_url": url, "page_text": r.get("inline") or r.get("text", ""),
                "image_b64": r.get("shot_b64") or None,
                "html": r.get("html") or "", "links": r.get("links") or []}
        enriched = await enrich_page(known, page, client=client, use_image=_USE_IMAGE)
        # enrich_page flags a HARD-FAIL (server down / retries exhausted) AND an output-truncation via output_truncated
        # (it does NOT return an _error key — _finalize returns output_truncated). Check THAT field, else the failure is
        # silently marked enriched. {AUDIT 2026-07-23 CRITICAL: worker checked enriched.get('_error') which never exists}.
        if enriched.get("output_truncated") or enriched.get("_error"):
            print(f"[enrich] ⛔ event {eid} VLM incomplete (hard-fail / truncated) {url[:60]} — fail, NOT enriched", flush=True)
            await db.fail_event(pool, eid, tok, "vlm_incomplete_or_truncated")
            return
        # persist into the NORMALIZED media schema (event_content_blocks / event_transcript_segments / event_media_urls),
        # fenced + transactional — replaces the old single-JSON-blob write to events.basic_info. {USER 2026-07-23 "建独立
        # 规范化 media schema"} [CONFIDENCE: CONFIRMED 100% — the 5 tables live via migration 20260723145355].
        ok = await db_media.mark_enriched_media(
            pool, eid, tok, enriched.get("basic_info") or [], enriched.get("transcript_segments") or [],
            enriched.get("urls") or media, source_url=url)
        n_blocks = len(enriched.get("basic_info") or [])
        print(f"[enrich] {'✅' if ok else '⚠️ lost-lease'} event {eid} → {n_blocks} basic_info blocks, "
              f"{len(enriched.get('urls') or media)} urls", flush=True)
    except Exception as e:                               # noqa: BLE001 — one event's crash must NOT sink the whole batch
        print(f"[enrich] ⛔ event {eid} UNEXPECTED {type(e).__name__}: {e} — fail (batch continues)", flush=True)
        try:
            await db.fail_event(pool, eid, tok, f"crash:{type(e).__name__}: {e}")
        except Exception as fe:                          # noqa: BLE001 — even fail_event failing must not raise out
            # fail_event ITSELF failed (pool exhausted / DB blip). Previously `pass` — the event then sat in 'rendering'
            # until its 30-minute lease lapsed and the reclaimer picked it up, so the system self-healed but the
            # throughput loss was COMPLETELY INVISIBLE: no log line, no counter, nothing to distinguish "slow" from
            # "silently dropping every event". Count it and say so, and name the lease as the only recovery path.
            # {EVENTS.PY:235 "ASYNC DEF RECONCILE_EVENTS(POOL: ASYNCPG.POOL) -> INT"} — that reclaimer is the ONLY thing
            # that frees this row, and a repo-wide grep found it has ZERO callers, so the recovery may not run at all.
            # [CONFIDENCE: CONFIRMED 100% — `grep -rn reconcile_events .` returned only the definition plus a pacer
            #  comment stating it does not touch work_queue; no scheduler/worker/shell entrypoint invokes it.]
            _STATS["fail_event_failed"] += 1             # surfaced in the per-batch + exit summary lines below
            print(f"[enrich] ⛔⛔ event {eid} fail_event ITSELF failed ({type(fe).__name__}: {fe}) — row stays "
                  f"'rendering' until its lease lapses; recovery needs reconcile_events to be running", flush=True)


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
        # process_event already self-contains failures (try/except → fail_event); return_exceptions=True is the belt so
        # even an escaped error can't sink the loop + strand the whole batch's claimed rows. {AUDIT 2026-07-23 HIGH}.
        results = await asyncio.gather(*(process_event(pool, client, ev) for ev in events), return_exceptions=True)
        # INSPECT the results instead of discarding them. `return_exceptions=True` was already correct, but throwing the
        # list away meant an exception that escaped process_event's own handler was swallowed twice over: the event stays
        # 'rendering' until its lease lapses, and NOTHING said so. That is indistinguishable from healthy-but-slow, which
        # is the failure mode this pipeline can least afford — production currently shows 146,793 events still
        # 'discovered' and all five enrichment tables empty, so silent per-event loss is exactly what must be visible.
        # [CONFIDENCE: CONFIRMED 100% — the gather result was previously unassigned; the zero-enrichment state is a live
        #  read of the events table taken during this audit.]
        escaped = [r for r in results if isinstance(r, BaseException)]
        if escaped:
            _STATS["escaped"] += len(escaped)
            for ev, r in zip(events, results):            # name the event id so a repeat offender is identifiable
                if isinstance(r, BaseException):
                    print(f"[enrich] ⛔ event {ev.get('id')} ESCAPED {type(r).__name__}: {r}", flush=True)
        if any(_STATS.values()):                          # per-batch line only when there is bad news to report
            print(f"[enrich] batch stats — escaped={_STATS['escaped']} "
                  f"fail_event_failed={_STATS['fail_event_failed']}", flush=True)
    print(f"[enrich] {_WORKER_ID} exiting — enrichment queue drained after {_MAX_IDLE_ROUNDS} idle rounds "
          f"(escaped={_STATS['escaped']}, fail_event_failed={_STATS['fail_event_failed']})", flush=True)


async def main() -> None:
    print(f"[enrich] starting {_WORKER_ID}", flush=True)
    pool = await db.connect_pool()
    try:
        await worker_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
