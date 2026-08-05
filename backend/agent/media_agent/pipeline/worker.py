"""media_agent.worker — THE ENRICHMENT WORKER (stage-2): turns `discovered` events into `enriched` events with basic_info.

用一句话讲完: 一个常驻进程,循环 { 从 events 表 SKIP-LOCKED claim 一批 `discovered` 事件 → 对该事件 media_urls 里的
**每一个** url 按 kind 分派给对应 handler(html→渲染+VLM、pdf/pptx/docx/xlsx→Docling、audio→whisper、video/webcast→
yt-dlp 或浏览器抓流)→ 全部结果汇进 Chart → 一个事务写进 5 张规范化表 },队列空了退避退出。**它是 discovery worker
的 event 级镜像**:同一套 claim/lease/fencing/fail-loud,但工作单元是"一个事件"而非"一家公司"。

URL 集合是 event_agent 一次性给定的,**不发现、不增长** —— 每个事件的工作量恰好是 len(media_urls)。
{USER 2026-08-03 "let's just use the original list from the event agent"} [CONFIDENCE: CONFIRMED 100%].

Flow (one event):
  claim (SKIP LOCKED, status→rendering + claim_token + lease)
    ──► for each url in media_urls:  router.classify → dispatch → handler fills the Chart
    ──► mark_enriched_media (content_blocks + transcript_segments + media_files + audio + url ledger, fenced, ONE txn)
  every url failed/skipped → fail_event('nothing_usable') — an event with no archive is NEVER marked 'enriched'.

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
from ..extract import router                          # url → kind, so each of the event's urls reaches the right handler
from ..extract.chart import Chart                      # the per-event accumulator (fill-and-append + content-hash dedup)
from ..storage import db_media                         # media_agent's normalized-schema writer (all 5 media tables)
from .handlers import dispatch                         # (url, kind) → the handler that owns that kind

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
    # A NON-ZERO value here means work is being thrown away, which is the one failure this pipeline cannot detect from
    # liveness: processes stay up, health checks stay 200, and enrichment output is simply zero. It must be counted.
    "lease_lost": 0,            # heartbeat found the claim gone mid-flight → the in-progress work was abandoned
}
_EMPTY_BACKOFF_S = int(os.environ.get("WATEREVENTS_EMPTY_BACKOFF_S", "10"))

# 0 = NEVER exit; poll forever. Any positive value keeps the old "drain then stop" behaviour for one-off runs.
#
# WHY a sentinel instead of relying on Restart=always: as a systemd service the old behaviour turns into a restart every
# 60s whenever the queue is momentarily empty, and each restart rebuilds the asyncpg pool and a fresh QwenClient —
# throwing away the warm upstream connection this loop deliberately keeps ("One shared QwenClient keeps the RunPod
# connection warm across the whole run"). Restart=always still belongs in the unit, but as a crash net, not as the
# mechanism for staying resident. Those are different jobs and conflating them makes the churn invisible.
#
# The event_agent fleet has no equivalent because its work_queue is refilled by the pacer; media's work arrives from
# stage-1's discovery rate instead, so idle gaps are normal and must be cheap.
# [CONFIDENCE: CONFIRMED — the loop condition and the QwenClient placement are both in this file, a few lines below].
_MAX_IDLE_ROUNDS = int(os.environ.get("WATEREVENTS_MAX_IDLE_ROUNDS", "6"))
_FOREVER = _MAX_IDLE_ROUNDS <= 0
# media wants VISION — the detail-page SCREENSHOT carries chart/table/transcript LAYOUT that text alone loses. But whether
# render even PRODUCES a screenshot is decided by the SHARED WATERCRAWL_NO_SHOT switch (event_agent defaults it ON = no
# shot). So couple use_image to that ONE switch: else use_image=True while render (NO_SHOT=1) hands back an EMPTY shot →
# media SILENTLY degrades to text-only and never knows. To run media in vision mode, launch its worker with
# WATERCRAWL_NO_SHOT=0 (render takes the shot → use_image=True). {port of event_agent's one-switch fix; media is vision-mode}
# [CONFIDENCE: CONFIRMED — worker.py read EVENT_USE_IMAGE while render read WATERCRAWL_NO_SHOT → the disconnect this fixes].
_NO_SHOT = os.environ.get("WATERCRAWL_NO_SHOT", "1") in ("1", "true", "yes")
_USE_IMAGE = not _NO_SHOT                                                          # never claim vision when render gives no shot
# Residential proxy for the document/audio/video fetches. YouTube blocks datacenter IPs outright and several IR CDNs
# rate-limit them, so the handlers need the SAME webshare gateway the render lane already uses. Absent → None (direct).
_PROXY = os.environ.get("WEBSHARE_PROXY") or None
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
        if not media:
            print(f"[enrich] ⛔ event {eid} has NO media urls at all — fail", flush=True)
            await db.fail_event(pool, eid, tok, "no_media_urls")
            return

        # THE FIXED LIST. Every url event_agent found gets dispatched to its handler, in order; nothing is discovered
        # and nothing is added. Work per event is therefore exactly len(media) units — knowable in advance, unlike the
        # frontier this replaced. Production distribution: 1 url for 56% of events, 2 for 20%, 3 for 8%, 4-6 for 16%
        # (mean ~2). {USER 2026-08-03 "let's just use the original list from the event agent"}
        # [CONFIDENCE: CONFIRMED 100% — direct user directive; the discovery path was deleted in the same change].
        chart = Chart({"title": ev["title"], "date": ev["event_date"], "type": ev["event_type"], "urls": media})
        detail = _detail_url(media)                          # the HTML page, for provenance stamping on blocks/segments

        for u in media:
            kind = router.classify(u)
            try:
                await dispatch(u, kind, chart, client=client, use_image=_USE_IMAGE, proxy=_PROXY)
            except Exception as e:                           # noqa: BLE001 — ONE bad url must not lose the other urls'
                chart.set_status(u, f"failed:{type(e).__name__}")   # work; record it loudly and keep going
                print(f"[enrich] ⚠️ event {eid} url {u[:60]} raised {type(e).__name__}: {str(e)[:80]}", flush=True)

        n_blocks = len(chart.basic_info)
        n_segs = len(chart.transcript_segments)
        n_files = sum(len(v) for v in chart.files.values())
        n_audio = len(chart.audio)
        # NOTHING-USABLE is a FAILURE, not an empty success. An event whose every url failed/skipped has no archive to
        # show; marking it 'enriched' would make a dead event indistinguishable from a genuinely content-free one.
        if not (n_blocks or n_segs or n_files or n_audio):
            statuses = {u: s.get("status", "") for s in chart.urls.values() for u in [s.get("url", "")]}
            print(f"[enrich] ⛔ event {eid} NOTHING-USABLE from {len(media)} urls — {statuses} — fail", flush=True)
            await db.fail_event(pool, eid, tok, "nothing_usable")
            return

        # persist into the NORMALIZED media schema — all FIVE tables now (content blocks / transcript segments /
        # media files / audio / url ledger), fenced + transactional. The ledger carries each url's REAL outcome.
        ok = await db_media.mark_enriched_media(
            pool, eid, tok, chart.basic_info, chart.transcript_segments,
            [s["url"] for s in chart.urls.values()], source_url=detail or "",
            files=chart.files, audio=chart.audio,
            url_status={s["url"]: s.get("status", "") for s in chart.urls.values()})
        print(f"[enrich] {'✅' if ok else '⚠️ lost-lease'} event {eid} ← {len(media)} urls → "
              f"{n_blocks} blocks, {n_segs} segments, {n_files} files, {n_audio} audio", flush=True)
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


# Renew well inside the lease so one failed renewal is not fatal: at 5 minutes against a 30-minute lease, five
# consecutive renewals must fail before the claim can actually lapse.
_HEARTBEAT_S = float(os.environ.get("WATEREVENTS_HEARTBEAT_S", "300"))


async def _lease_heartbeat(pool, eid, tok, work: asyncio.Task) -> None:
    """Hold the claim open while `work` runs, and cancel `work` the moment the claim is provably gone.

    WHY it cancels rather than just logging: once the token is lost, every remaining second is spent producing output
    that the fenced write will refuse. The reaper has ALREADY handed this event to another worker, so continuing means
    two workers rendering the same pages and calling the same VLM — the exact duplicate work visible in
    {DOCLING.LOG 2026-08-05 "3470473B → 11079 CHARS, 5 TABLES IN 1476.5S" AND "3470473B → 11079 CHARS, 5 TABLES IN 1205.5S"}
    (one document, two full extractions). Cancelling returns the slot to the pool immediately.
    [CONFIDENCE: CONFIRMED — the duplicate pair is the same byte count and the same table count in the same log.]
    """
    while True:
        await asyncio.sleep(_HEARTBEAT_S)
        try:
            alive = await db.renew_lease(pool, eid, tok)
        except Exception as e:                           # noqa: BLE001 — a DB blip is NOT proof the lease is gone;
            print(f"[enrich] ⚠️ event {eid} lease renewal errored ({type(e).__name__}: {e}) — "  # keep working and
                  f"keeping the work alive, next attempt in {_HEARTBEAT_S:.0f}s", flush=True)     # retry next tick
            continue
        if not alive:
            _STATS["lease_lost"] += 1
            print(f"[enrich] ⛔ event {eid} lease LOST mid-flight — abandoning now instead of finishing work that "
                  f"the fenced write would reject", flush=True)
            work.cancel()
            return


async def _enrich_with_lease(pool, client, ev) -> None:
    """Run one event's enrichment with its lease held open underneath it. Wraps process_event without touching it, so
    the enrichment logic stays unaware of leasing."""
    eid, tok = ev["id"], ev["claim_token"]
    work = asyncio.create_task(process_event(pool, client, ev))
    beat = asyncio.create_task(_lease_heartbeat(pool, eid, tok, work))
    try:
        await work
    except asyncio.CancelledError:
        # Cancelled BY the heartbeat (lease genuinely lost). The event is already back in another worker's hands, so
        # there is nothing to fail_event here — writing a failure would clobber the new owner's claim.
        pass
    finally:
        beat.cancel()                                    # normal completion — stop the heartbeat before returning


async def worker_loop(pool) -> None:
    """Claim-batch → enrich-in-parallel → repeat until the discovered queue is drained (N idle rounds). One shared
    QwenClient keeps the RunPod connection warm across the whole run."""
    client = QwenClient()
    idle = 0
    while _FOREVER or idle < _MAX_IDLE_ROUNDS:
        events = await db.claim_events(pool)             # a batch of discovered/retry-due events (SKIP LOCKED)
        if not events:
            idle += 1
            # In forever mode say so plainly rather than printing a countdown toward an exit that will never happen —
            # a log full of "6/6" that never ends is how a healthy idle service gets mistaken for a stuck one.
            where = "forever" if _FOREVER else f"{idle}/{_MAX_IDLE_ROUNDS}"
            print(f"[enrich] queue empty ({where}) — backoff {_EMPTY_BACKOFF_S}s", flush=True)
            await asyncio.sleep(_EMPTY_BACKOFF_S)
            continue
        idle = 0
        print(f"[enrich] claimed {len(events)} events", flush=True)
        # process_event already self-contains failures (try/except → fail_event); return_exceptions=True is the belt so
        # even an escaped error can't sink the loop + strand the whole batch's claimed rows. {AUDIT 2026-07-23 HIGH}.
        results = await asyncio.gather(*(_enrich_with_lease(pool, client, ev) for ev in events), return_exceptions=True)
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
                  f"fail_event_failed={_STATS['fail_event_failed']} "
                  f"lease_lost={_STATS['lease_lost']}", flush=True)
    print(f"[enrich] {_WORKER_ID} exiting — enrichment queue drained after {_MAX_IDLE_ROUNDS} idle rounds "
          f"(escaped={_STATS['escaped']}, fail_event_failed={_STATS['fail_event_failed']}, "
          f"lease_lost={_STATS['lease_lost']})", flush=True)


async def main() -> None:
    print(f"[enrich] starting {_WORKER_ID}", flush=True)
    pool = await db.connect_pool()
    try:
        await worker_loop(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
