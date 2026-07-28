"""media_agent.render_retry — the ONE bounded retry-with-backoff wrapper around watercrawl.render_shot for stage 2.

用一句话讲完: 调 render_shot 一次拿不到 text/links 就等 2s、4s 再试(默认 3 次),只有当每次都空才判定这个页面真的死了 —
因为在 fleet 并发下重页面会 TimeoutError 返回空壳,而同一个页面单独跑就成功,所以"第一次空"根本不等于"页面不可用"。
唯一的例外是 method=="walled":那代表 render_shot 内部四个 tier 全被 bot 墙打穿了,外层重试只会原样重跑一条已知失败
的链、零新战术,所以立刻返回。

WHY this module exists at all — the measured failure it fixes: media_agent's两个 render 调用点 (handlers.handle_html /
worker.process_event) 原本各自 bare-call render_shot 一次,空了就直接 `failed:empty-render` / `fail_event`。叠加
`fail_event` 的 3-strike → dead_letter 升级,一个纯粹的瞬时超时会被永久转成 dead letter。event 侧的同一条 render 路径
早就不这么做了,并把实测原因写进了注释。
{ENGINE.PY:191-194 "RETRIES AN EMPTY RENDER UP TO _RENDER_TRIES TIMES WITH A SHORT BACKOFF: A LOAD-INDUCED TIMEOUTERROR
COMES BACK EMPTY, AND A RETRY ONCE THE BROWSER POOL HAS FREED UP USUALLY LANDS THE PAGE."}
[CONFIDENCE: CONFIRMED 100% — event_agent runs the identical render stack against the identical IR hosts under the
identical fleet concurrency; the only difference was that media_agent treated attempt #1's empty as terminal.]

WHY a private copy instead of importing event_agent.crawl.engine._render_one: engine._render_one is welded to the
event-side crawl (it calls watercrawl.should_expand / expand_events_page to drive year-filter + "Load More" controls on
an events LISTING page). Stage 2 renders an event DETAIL page where that expansion is meaningless work. Importing it
would also make media_agent depend on a module owned by another agent, against the repo's stated dependency direction
{DB_MEDIA.PY:6 "DEPENDENCY 方向: MEDIA_AGENT 依赖 EVENT_AGENT.DB 的 POOL 和自己的 CHART/ROUTER HELPERS, EVENT_AGENT 永不
反依赖 MEDIA_AGENT"}. [CONFIDENCE: CONFIRMED 100% — engine.py:213-220 is the events-page expansion block, detail-page-irrelevant.]

TODO(unify): render_retry.render_with_retry and event_agent.crawl.engine._render_one are now two implementations of the
same bounded-retry policy. They should collapse into ONE shared utility (e.g. providers/watercrawl/retry.py) with the
events-page expansion passed in as an optional post-render hook, so the backoff schedule and the walled-is-terminal rule
can never drift apart between the two stages. Kept separate here only because engine.py is owned by another agent.
"""
from __future__ import annotations

import asyncio
import os

from providers import watercrawl

# Attempts + backoff mirror the event side EXACTLY so both stages give a slow IR host the same number of chances; a
# media-only override exists because stage 2 renders one detail page per event (a different load shape than the crawl's
# fan-out) and may need separate tuning. {ENGINE.PY:170 "_RENDER_TRIES = INT(OS.ENVIRON.GET(\"EVENT_RENDER_TRIES\", \"3\"))"}
# [CONFIDENCE: CONFIRMED 100% — value read from the event-side source; same default keeps the two stages comparable.]
_RENDER_TRIES = int(os.environ.get("MEDIA_RENDER_TRIES", os.environ.get("EVENT_RENDER_TRIES", "3")))
# Linear backoff 2s, 4s — long enough for the shared browser pool to drain the burst that caused the timeout, short
# enough that a genuinely dead page costs ~6s not minutes. {ENGINE.PY:230 "AWAIT ASYNCIO.SLEEP(2.0 * (ATTEMPT + 1))
# # 2S, 4S — LET THE BROWSER POOL DRAIN BEFORE RE-FIRING"} [CONFIDENCE: CONFIRMED 100% — verbatim from the event side.]
_BACKOFF_BASE_S = float(os.environ.get("MEDIA_RENDER_BACKOFF_S", "2.0"))


def _has_content(r: dict | None) -> bool:
    """A render counts as SUCCESS iff it produced page text or a link harvest — the exact same emptiness test both
    media call sites already used, kept in one place so retry and caller can never disagree about what 'empty' means.
    links-only still counts: the impersonate/camoufox tiers routinely return links with thin html, and extract_html's
    harvest consumes links[] on its own {EXTRACT_HTML.PY:167 "FOR U IN (LINKS OR []): # RENDER['LINKS'] — COVERS
    IMPERSONATE/CAMOUFOX TIERS (HTML THIN)"}. [CONFIDENCE: CONFIRMED 100% — matches handlers.py + worker.py's own test.]"""
    if not r:
        return False
    return bool(r.get("text") or r.get("links"))


async def render_with_retry(url: str, tries: int | None = None) -> dict:
    """Render ONE url with watercrawl, retrying an EMPTY result with backoff, and return the render dict.

    Upstream trigger: called by handlers.handle_html (close-loop page fetch) and worker.process_event (detail-page
    fetch) — the two places stage 2 touches the browser. Downstream: the returned dict feeds extract_html + the VLM;
    a still-empty result after every attempt makes the caller fail the url/event LOUDLY, exactly as before.

    Core logic / before-after workflow:
      BEFORE: `r = await asyncio.to_thread(watercrawl.render_shot, url)` → one shot; empty ⇒ terminal failure ⇒ (in the
              worker) fail_event ⇒ 3 strikes ⇒ dead_letter. A transient TimeoutError became a permanent dead letter.
      AFTER:  attempt 1 → content? return. empty + method=="walled"? return immediately (retry is provably useless).
              else sleep 2s → attempt 2 → sleep 4s → attempt 3 → return the LAST render dict either way.

    Returns the render dict ALWAYS (never None, never raises): on total failure it is the last attempt's dict, which is
    falsy-on-text-and-links, so an existing `if not r.get("text") and not r.get("links")` check at the call site keeps
    working verbatim and keeps emitting its own fail-loud message with the real `method`."""
    n = tries if tries is not None else _RENDER_TRIES     # per-call override exists for tests; env default otherwise
    n = max(1, n)                                         # tries=0 would skip the render entirely — clamp to one shot
    r: dict = {}
    for attempt in range(n):
        # render_shot is SYNC and marshals to the browser loop internally, so it must run off the event loop or it
        # blocks every other coroutine in this worker's batch. {WORKER.PY:87 "RENDER_SHOT IS SYNC + MARSHALS TO THE
        # BROWSER LOOP"} [CONFIDENCE: CONFIRMED 100% — both original call sites already used to_thread for this reason.]
        r = await asyncio.to_thread(watercrawl.render_shot, url) or {}
        if _has_content(r):                               # real content → done, never burn the remaining attempts
            return r
        # method=="walled" is TERMINAL WITHIN ONE ATTEMPT: render_shot only returns it after its own 4-tier chain
        # (render→residential→impersonate→camoufox) has already failed, so an outer retry re-runs a known-failed chain
        # with zero new tactics. STRICTLY "walled" — a nav timeout returns method=="" and MUST keep retrying, which is
        # the entire point of this module. {ENGINE.PY:227 "IF R.GET(\"METHOD\") == \"WALLED\": RETURN NONE"}
        # {RENDER.PY:362 "\"WALLED\" (A BOT-CHALLENGE BODY THAT BEAT EVERY TIER → EMPTY)"}
        # [CONFIDENCE: CONFIRMED 100% — the event side documents that widening this to method=="" kills pages that
        #  legitimately revive on attempt 3.]
        if r.get("method") == "walled":
            return r
        if attempt < n - 1:                               # not the last attempt → back off, then re-fire
            wait_s = _BACKOFF_BASE_S * (attempt + 1)      # 2s, 4s — linear, matching the event side's schedule
            print(f"[media] ↻ empty render {url[:70]} (attempt {attempt + 1}/{n}, method={r.get('method','')!r}) "
                  f"— retrying in {wait_s:.0f}s", flush=True)   # loud: a retry is a real cost, never hide it
            await asyncio.sleep(wait_s)
    return r                                              # every attempt empty → hand back the last dict; caller fails loud
