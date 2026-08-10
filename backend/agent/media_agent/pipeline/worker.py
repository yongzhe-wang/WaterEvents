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

from agent.event_agent.storage import events as db
from agent.event_agent.storage import queue as q      # hub promotion reuses the scheduler's own enqueue                       # the SHARED WaterEvents DB layer (companies + events tables, claim/fail)
from ..extract import router                          # url → kind, so each of the event's urls reaches the right handler
from ..extract.chart import Chart                      # the per-event accumulator (fill-and-append + content-hash dedup)
from ..storage import db_media                         # media_agent's normalized-schema writer (all 5 media tables)
from .handlers import _TRACKER_URL_RE, dispatch        # (url, kind) → its handler; + the tracker pattern dispatch gates on

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
# Verbatim copy of seed_incremental's exclusion — a hub the VLM finds must clear the SAME policy bar a
# hub the scheduler finds has to clear. {SEED.PY hub filter} [CONFIDENCE: CONFIRMED — copied literally.]
_HUB_EXCLUDE_RE = re.compile(r"/(sec-filings|edgar|financials/(sec|quarterly|annual|financial-results)"
                             r"|regulatory)", re.I)


# 一个事件内 url 的处理顺序。html 排第一不是为了整齐 —— 它那次 LLM 调用会**采纳属于本事件的文档链接**
# (SYSTEM_ROUTE 的第三个任务),采纳出来的 url 进 chart 的 pending,由 pass 2 处理。html 排在后面,
# 它采纳的文档就要多等一轮才被抓。文档次之,音视频最后(它们最慢且不产出元数据)。
# {HANDLERS.PY _adopt_documents —— html 路径产出新 url 的唯一来源}
# stage-1 给的列表顺序没有语义,它只是页面上链接出现的次序。
# [CONFIDENCE: CONFIRMED 100% — 采纳逻辑在 handle_html 里,pass 2 在本文件下方。]
_KIND_RANK = {router.KIND_HTML: 0,
              router.KIND_PDF: 1, router.KIND_PPTX: 1, router.KIND_DOCX: 1, router.KIND_XLSX: 1,
              router.KIND_AUDIO: 2, router.KIND_VIDEO: 2}


def _ordered(urls: list) -> list:
    """html → 文档 → 音视频 → 其他。同一档内保持原有相对次序(稳定排序),不引入新的不确定性。"""
    return sorted(urls, key=lambda u: _KIND_RANK.get(router.classify(u), 3))


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
    """Pick the event's DETAIL PAGE to render: the first http url that is neither a media asset (pdf/mp3/…) nor a
    click-tracking redirect. Assets are recorded as urls but not rendered — the HTML detail page is where basic_info
    and newly-linked media live. Trackers are skipped because they resolve somewhere generic, so rendering one yields
    the site's navigation rather than this event (see _TRACKER_URL_RE for the measured case)."""
    for u in media_urls or []:
        if isinstance(u, str) and u.lower().startswith("http") \
                and not _ASSET_RE.search(u) and not _TRACKER_URL_RE.search(u):
            return u
    return None


async def _promote_hub(pool, ev, hub_url: str) -> None:
    """Put a mis-classified listing page into the incremental hub rotation, then let the caller delete the event.

    Dedup is the queue's own UNIQUE(type,url) — re-adding an existing hub is a no-op that keeps its live status and
    due_at. {QUEUE.PY "ON CONFLICT (TYPE, URL) DO NOTHING"} so this can run on every hub verdict without bookkeeping.
    [CONFIDENCE: CONFIRMED 100% — read from the enqueue statement.]

    The seed filter's EXCLUSION is honoured here rather than bypassed. Stage-1 deliberately refuses to monitor filings
    archives, and a model verdict must not overrule a policy decision the user already made:
    {SEED.PY "AND source_url !~* '/(sec-filings|edgar|financials/(sec|quarterly|annual|financial-results)|regulatory)'"}
    {USER 2026-07-25 "big filling hub is def not the ones we should monitor"}
    [CONFIDENCE: CONFIRMED 100% — the exclusion is in seed_incremental and its reason is recorded there.]
    """
    if not hub_url:
        return
    if _HUB_EXCLUDE_RE.search(hub_url):
        print(f"[enrich] ⏭ hub {hub_url[:70]} matches the filings-archive exclusion — not enqueued", flush=True)
        return
    try:
        await q.enqueue(pool, [{"company_id": ev.get("company_id"), "url": hub_url, "type": "incremental"}])
    except Exception as e:                                   # noqa: BLE001 — enqueue failing must not block the delete;
        print(f"[enrich] ⚠️ hub enqueue failed for {hub_url[:60]} ({type(e).__name__}: {e})", flush=True)


async def process_event(pool, client: QwenClient, ev) -> None:
    """Enrich ONE claimed event: render its detail page + VLM → basic_info, write back fenced. Every failure path is
    LOUD (fail_event with a reason) so a page we couldn't render / the VLM couldn't parse is NEVER marked enriched. The
    WHOLE body is wrapped so an UNEXPECTED exception (mark_enriched conn error / bad data) fails JUST this event via
    fail_event — it must NOT propagate out of gather and sink the batch (leaving the batch's rows stuck in 'rendering'
    until lease-expiry). {AUDIT 2026-07-23 HIGH: gather(return_exceptions=False) + no try/except}."""
    eid, tok = ev["id"], ev["claim_token"]
    try:
        # UNWRAP DOCUMENT-VIEWER URLS BEFORE ANYTHING ELSE SEES THEM. A `/pdf-viewer.aspx?src=…report.pdf` is a page
        # whose only content is the PDF it embeds; left as-is it classifies as html, gets rendered, and what lands in
        # event_documents is the PDF.js toolbar. Rewriting the list HERE rather than inside dispatch is what keeps the
        # three records consistent: Chart is seeded from this list, the url ledger is keyed off Chart, and the document
        # row carries the url the handler was given — unwrap later and the ledger would file the wrapper while the
        # document filed the target, so neither table could answer "did we get this document".
        # {DB 2026-08-10 event_documents kind='html' from viewer wrappers → 539 rows, 479 under 1000 chars, avg 1002;
        #  vodafone alone 421. Body of the modal, verbatim: "SKIP TO MAIN CONTENT PDF.JS VIEWER FIND 11 PREVIOUS NEXT
        #  … ZOOM OUT ZOOM IN PAGE FIT AUTOMATIC ZOOM ACTUAL SIZE PAGE WIDTH 0% 50% 75% 100% … SAVE"}
        # [CONFIDENCE: CONFIRMED 100% — the same wrapper url in a browser shows a 68-page results deck, so the routing
        #  was the whole of the problem; unwrap_viewer refuses any target without a document extension.]
        media = [router.unwrap_viewer(u) if isinstance(u, str) else u for u in _as_list(ev["media_urls"])]
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

        async def _run(u: str) -> None:
            kind = router.classify(u)
            try:
                # event_urls 让 office handler 判断「这个事件有没有网页可读」—— 全是文档时才为它单独取
                # 一次标题/日期,有网页时那件事归 SYSTEM_ROUTE,不重复花调用。
                await dispatch(u, kind, chart, client=client, use_image=_USE_IMAGE, proxy=_PROXY,
                               event_urls=media)
            except Exception as e:                           # noqa: BLE001 — ONE bad url must not lose the other urls'
                chart.set_status(u, f"failed:{type(e).__name__}")   # work; record it loudly and keep going
                print(f"[enrich] ⚠️ event {eid} url {u[:60]} raised {type(e).__name__}: {str(e)[:80]}", flush=True)

        # PASS 1 — the urls event_agent gave us. Rendering an html page here may ALSO make the model name document
        # links that belong to this event; those land in the chart's ledger as new pending urls.
        for u in _ordered(media):
            await _run(u)

        # PAGE VERDICT — decided before pass 2, because a hub has nothing worth fetching and a dead page has nothing
        # at all. Acting here rather than inside the handler is deliberate: dropping an event and promoting a hub are
        # EVENT-level acts that need the claim token, which the per-url handler does not have.
        verdict = chart.verdict()
        if verdict == "hub":
            # Not a failure — a MISCLASSIFICATION by stage-1, and a useful one. The page is a listing, so it belongs in
            # the incremental hub rotation; the event minted from it does not exist and is deleted. Net effect of the
            # mistake is one more monitored listing, not one more dead row.
            # Measured before this shipped: on 40 urls carrying exactly one event, 8 were listings, and every one of
            # those 8 was unambiguous (one url literally ends `?page=3`).
            # {PROBE 2026-08-06 "SINGLE_EV N=40 {'EVENT': 32, 'HUB': 8}"} with 95% recall on 20 confirmed hubs
            # {PROBE 2026-08-06 "KNOWN_HUB N=20 {'HUB': 19, 'EVENT': 1}"}
            # [CONFIDENCE: CONFIRMED 100% — live run; each of the 8 was read individually and none was a real event.]
            await _promote_hub(pool, ev, detail or (media[0] if media else ""))
            gone = await db.delete_event(pool, eid, tok)
            print(f"[enrich] 🗂 event {eid} is a HUB not an event — url promoted to incremental, "
                  f"event {'deleted' if gone else 'NOT deleted (lease lost)'}", flush=True)
            return
        if verdict == "dead":
            # A dead page is the site's problem and may be temporary, so this goes through the retry path rather than
            # the delete path. The two verdicts must not share a disposition: one means "never an event", the other
            # means "not readable right now".
            print(f"[enrich] 💀 event {eid} page is DEAD (error/wall/empty) — failing for retry", flush=True)
            await db.fail_event(pool, eid, tok, "page_dead")
            return

        # PASS 2 — documents the model picked off the page. They are dispatched but discover nothing further: a file is
        # not a page, so it yields no links. The depth of this whole mechanism is 2 BY CONSTRUCTION, not by a budget.
        fresh = [u for u in chart.pending_urls() if u not in set(media)]
        for u in _ordered(fresh):
            await _run(u)
        if fresh:
            print(f"[enrich] ↳ event {eid} pass-2 on {len(fresh)} adopted document url(s)", flush=True)

        # ONE (md, blocks) pair per source url — html pages and office documents in one list, no branch on origin.
        docs = chart.build_documents()
        n_docs = len(docs)
        n_chars = sum(d.get("n_chars", 0) for d in docs)       # total prose extracted, the honest "did we get content"
        n_segs = len(chart.transcript_segments)
        n_audio = len(chart.audio)
        # NOTHING-USABLE is a FAILURE, not an empty success. An event whose every url failed/skipped has no archive to
        # show; marking it 'enriched' would make a dead event indistinguishable from a genuinely content-free one.
        if not (n_docs or n_segs or n_audio):
            statuses = {u: s.get("status", "") for s in chart.urls.values() for u in [s.get("url", "")]}
            # Separate OUR choice from the event's problem. When every url was gated off by MEDIA_KINDS, this event was
            # never attempted — calling that `nothing_usable` would blame the source for our configuration AND, because
            # failures are retried, would burn the retry budget re-deciding not to look. `deferred:kind-disabled` says
            # the work is still owed, so re-enabling a lane and requeueing picks these up as a clean batch.
            # {HANDLERS.PY "_ENABLED_KINDS" — the gate that produced these statuses}
            # [CONFIDENCE: CONFIRMED 100% — the status string is written by that gate and nothing else emits it.]
            # OUR OWN choices, as opposed to anything the source did. An event every one of whose urls we declined to
            # fetch was never attempted, so calling it nothing_usable both mislabels it and burns its retry budget.
            # The first version of this check listed only kind-disabled, and the omission was measurable: a mixed event
            # (one url on a disabled lane, one filtered as an SEC filing) failed the all() and landed in the failure
            # path — 812 rows in the first prioritised batch.
            # {psql 2026-08-06 over enrich_priority=100 "FAILED | NOTHING_USABLE | 812" beside "DEFERRED |
            #  DEFERRED:KIND-DISABLED | 1106", with the ledger showing both skip reasons interleaved on those events}
            # [CONFIDENCE: CONFIRMED 100% — status/reason breakdown read from the live database.]
            _OURS = ("skipped:kind-disabled", "skipped:sec-filing")
            vals = list(statuses.values())
            if vals and all(s.startswith(_OURS) for s in vals):
                why = "deferred:sec-filing" if all(s.startswith("skipped:sec-filing") for s in vals) \
                    else "deferred:kind-disabled"
                print(f"[enrich] ⏸ event {eid} DEFERRED — all {len(media)} urls were declined by us "
                      f"({why}); not a failure, work still owed", flush=True)
                await db.defer_event(pool, eid, tok, why)
                return
            print(f"[enrich] ⛔ event {eid} NOTHING-USABLE from {len(media)} urls — {statuses} — fail", flush=True)
            await db.fail_event(pool, eid, tok, "nothing_usable")
            return

        # persist into the NORMALIZED media schema — FOUR tables (documents / transcript segments / audio / url
        # ledger), fenced + transactional. The ledger carries each url's REAL outcome.
        ok = await db_media.mark_enriched_media(
            pool, eid, tok, docs, chart.transcript_segments,
            [s["url"] for s in chart.urls.values()], source_url=detail or "",
            # The metadata task's output finally reaches the row it describes. It was computed on every ROUTE call and
            # then discarded: the writer's UPDATE never named title/date/event_type.
            meta={"title": chart.title, "date": chart.date, "type": chart.type},
            audio=chart.audio,
            url_status={s["url"]: s.get("status", "") for s in chart.urls.values()})
        print(f"[enrich] {'✅' if ok else '⚠️ lost-lease'} event {eid} ← {len(media)} urls → "
              f"{n_docs} docs ({n_chars:,} chars), {n_segs} segments, {n_audio} audio", flush=True)
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


async def _batch_heartbeat(pool, pairs: list[tuple], tasks: dict) -> None:
    """ONE heartbeat for the whole claimed batch: renew every live claim in a single statement, and cancel exactly
    those events whose claim the database says is gone.

    WHY one task for the batch instead of one per event: the per-event shape cost one pooled connection per concurrent
    event, and a worker runs ENRICH_BATCH=16 of them against a pool of 4. Twelve of every sixteen renewals timed out —
    {MEDIA@2 2026-08-05 "08:44:58 ⚠️ EVENT 5E2ECCC0 LEASE RENEWAL ERRORED (TIMEOUTERROR: )" — EXACTLY FOUR SUCH LINES
     PER TICK, THE POOL SIZE PRINTED THROUGH AS THE GROUP SIZE} — so the leases lapsed anyway and the reaper took the
    rows back mid-flight. media@2/@3/@5 sat in that state for 40 minutes and committed nothing while media@1/@4/@6,
    which happened to win the connection race, committed 13/14/11 events.
    [CONFIDENCE: CONFIRMED — grouping of four per tick matches MAX_SIZE=4; the split in committed counts across the six
     workers is in the same journal window.]

    Cancelling a genuinely-lost event is deliberate: the reaper has already reissued it to another worker, so finishing
    would produce output the fenced write must refuse AND duplicate a document another worker is already extracting —
    {DOCLING.LOG 2026-08-05 "3470473B → 11079 CHARS, 5 TABLES IN 1476.5S" AND "... IN 1205.5S"} is one document
    extracted twice concurrently, which is exactly that waste.
    """
    while True:
        await asyncio.sleep(_HEARTBEAT_S)
        # Only renew what is still running — a finished event has already written (or failed) under its own token, and
        # renewing its lease would hold a row nobody is working on.
        live = [(eid, tok) for eid, tok in pairs if not tasks[eid].done()]
        if not live:
            return                                       # whole batch finished; nothing left to hold open
        try:
            still_ours = await db.renew_leases(pool, live)
        except Exception as e:                           # noqa: BLE001 — a DB blip is NOT proof the leases are gone.
            # This branch is why the batch form matters: the per-event version took this path 12 times per tick under
            # pool exhaustion, which LOOKS like resilience while the leases quietly expire underneath it.
            print(f"[enrich] ⚠️ batch lease renewal errored ({type(e).__name__}: {e}) for {len(live)} events — "
                  f"keeping the work alive, next attempt in {_HEARTBEAT_S:.0f}s", flush=True)
            continue
        for eid, _tok in live:
            if eid not in still_ours and not tasks[eid].done():
                _STATS["lease_lost"] += 1
                print(f"[enrich] ⛔ event {eid} lease LOST mid-flight — abandoning now instead of finishing work "
                      f"the fenced write would reject", flush=True)
                tasks[eid].cancel()


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
        # Tasks keyed by event id so the batch heartbeat can cancel ONE event without touching its siblings, and so
        # it can skip renewing events that have already finished.
        tasks = {ev["id"]: asyncio.create_task(process_event(pool, client, ev)) for ev in events}
        pairs = [(ev["id"], ev["claim_token"]) for ev in events]
        beat = asyncio.create_task(_batch_heartbeat(pool, pairs, tasks))
        try:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        finally:
            beat.cancel()                                # the batch is settled — stop holding its leases open
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
