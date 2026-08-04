"""service — the render VM's HTTP face: exposes watercrawl's four browser entries over the VPC so the crawl workers
stop paying for Chromium on their own cores.

用一句话讲完: 在 ir-render-16 上起一个 aiohttp 服务 → worker POST 一个 url 过来 → 服务在线程里调本地的
render_shot / render_full / render_detail / capture_media(还是那份代码,还是那个常驻浏览器)→ 把结果原样 JSON
吐回去。WHY: 渲染、文档抽取、语音转写三种负载本来全挤在 ir-media-8 的 8 个核上互相抢 CPU —— 渲染占了单元时间预算
的 50.9%,而它是纯 CPU + 内存的活,天然该独占一台机器。把它搬走,worker 那台就只剩发请求和写 DB。
{MEASURED n=7,655 units — render 17.4s = 51% of the per-unit budget, vs DB 0.14s}
[CONFIDENCE: CONFIRMED — per-stage timing collected over the live fleet].

上游触发: event_agent.crawl.engine 和 media_agent.pipeline.render_retry / handlers,经由 providers.render_remote
客户端(见 providers/watercrawl/__init__.py 的分流)。下游连接: 本机的常驻 Chromium / patchright / camoufox。

BOUNDARY: this service is VPC-internal ONLY. It is never given a public firewall rule — `default-allow-internal`
already opens 10.128.0.0/9 to every port, which is exactly the reach the workers need and no more. It therefore does
NOT authenticate: anything that can route to it is already inside the project's network.
{GCLOUD 2026-08-04 "DEFAULT-ALLOW-INTERNAL  DEFAULT  10.128.0.0/9  TCP:0-65535,UDP:0-65535,ICMP"}
[CONFIDENCE: CONFIRMED — read from the live firewall list; no rule targets tag `ir-render`].
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from aiohttp import web

# Import the LOCAL implementations directly from their modules, NOT through the package __init__. The __init__ carries
# the remote/local dispatch, and on a box where RENDER_REMOTE_URL happened to be exported that dispatch would hand this
# service the HTTP client — which would then call itself. Importing the modules bypasses the question entirely.
from .render import render_shot as _render_shot
from .orchestrator import render_full as _render_full, render_detail as _render_detail
from .capture import capture_media as _capture_media
from .runtime import browser_available

PORT = int(os.environ.get("RENDER_SERVICE_PORT", "8100"))

# One counter per entry so /health reports what the box is actually doing. Plain ints are safe here: every mutation
# happens on the aiohttp event loop thread, and the blocking work is what gets pushed to the executor, not the counting.
_stats: dict = {"started": time.time(), "inflight": 0, "total": 0, "errors": 0, "by_method": {}}


def _loud(msg: str) -> None:
    """The fail-loud channel. A render failing here is a QUALITY signal for the whole pipeline and must be visible in
    the service log, never swallowed into an empty 200."""
    print(f"[render.service] {msg}", file=sys.stderr, flush=True)


async def _call(fn, *args):
    """Run one SYNC watercrawl entry off the event loop.

    WHY a thread: every watercrawl entry is sync and marshals internally to the browser's OWN asyncio loop thread
    {WATERCRAWL/__INIT__.PY "一个进程内常驻 PLAYWRIGHT CHROMIUM(懒启动)+ 专属 ASYNCIO LOOP 线程 + SEMAPHORE 限并发"}.
    Calling it directly from the aiohttp handler would block this loop for the entire render — tens of seconds — and
    stall every other in-flight request on the box.
    [CONFIDENCE: CONFIRMED — the same to_thread wrapper is what every existing caller uses, e.g.
     engine.py:196 "R = AWAIT ASYNCIO.TO_THREAD(WATERCRAWL.RENDER_SHOT, URL)"].

    Concurrency is NOT bounded here on purpose: watercrawl's own semaphores (IR_WATERCRAWL_BROWSERS,
    WATERCRAWL_SHOT_CONCURRENCY, CAMOUFOX_CAP) are the real gate, and adding a second limit above them would only make
    the effective ceiling harder to reason about.
    """
    _stats["inflight"] += 1
    _stats["total"] += 1
    try:
        return await asyncio.to_thread(fn, *args)
    finally:
        _stats["inflight"] -= 1


def _args(body: dict, default_wait: int) -> tuple[str, int]:
    """Pull (url, wait_ms) out of a request body. Boundary validation — this is where untrusted input enters."""
    url = str(body.get("url") or "").strip()
    try:
        wait = int(body.get("wait_ms") or default_wait)
    except (TypeError, ValueError):
        wait = default_wait
    return url, wait


async def h_render_shot(request: web.Request) -> web.Response:
    """POST /render_shot {url, wait_ms?} → {text, links, html, shot_b64, method, inline}.

    The hot path: event_agent's crawl engine and media_agent's html handler both come through here. The response is the
    LOCAL function's dict passed through untouched, so the client can stay a pure transport with no shape knowledge.
    `shot_b64` is a full-page JPEG and is the only large field; it crosses the VPC, not the internet."""
    body = await request.json()
    url, wait = _args(body, 0)
    if not url:
        return web.json_response({"error": "missing url"}, status=400)
    # wait_ms=0 means "use the module default" — forward the caller's value only when they actually set one, so the
    # tuned SETTLE_FIXED_MS default keeps applying {CONFIG.PY:53 "SETTLE_FIXED_MS = ... WATERCRAWL_SETTLE_FIXED_MS, 1500"}.
    out = await (_call(_render_shot, url, wait) if wait else _call(_render_shot, url))
    m = (out or {}).get("method") or "empty"
    _stats["by_method"][m] = _stats["by_method"].get(m, 0) + 1
    if m in ("", "empty", "walled"):
        _loud(f"render_shot({url}) → method={m!r}")
    return web.json_response(out or {})


async def _h_tuple(request: web.Request, fn, name: str) -> web.Response:
    """Shared handler for the two orchestrator entries, which return a TUPLE (text, links, method) rather than a dict.
    JSON has no tuple, so it is named on the wire and the client re-tuples it — keeping the local call signature
    byte-identical for callers {ORCHESTRATOR.PY:51 "DEF RENDER_FULL(URL: STR, WAIT_MS: INT = 0) -> TUPLE[STR, LIST, STR]"}."""
    body = await request.json()
    url, wait = _args(body, 0)
    if not url:
        return web.json_response({"error": "missing url"}, status=400)
    text, links, method = await _call(fn, url, wait)
    if not text:
        _loud(f"{name}({url}) → EMPTY (method={method!r})")
    return web.json_response({"text": text, "links": list(links or []), "method": method or ""})


async def h_render_full(request: web.Request) -> web.Response:
    """POST /render_full {url, wait_ms?} → {text, links, method}. ir_url_agent's IR-homepage nav-link harvest."""
    return await _h_tuple(request, _render_full, "render_full")


async def h_render_detail(request: web.Request) -> web.Response:
    """POST /render_detail {url, wait_ms?} → {text, links, method}. The deep-page variant of the same chain."""
    return await _h_tuple(request, _render_detail, "render_detail")


async def h_capture_media(request: web.Request) -> web.Response:
    """POST /capture_media {url, wait_ms?} → {media, method, n_requests, error}.

    The webcast path: open a player page and harvest the media urls the browser itself requested. This exists because
    yt-dlp cannot parse the corporate webcast platforms at all — they are 83.5% of every video/webcast url we hold.
    {PROBE 2026-08-03 yt-dlp over 12 platforms: CHORUSCALL / VIAVID.WEBCASTS / EVENT.WEBCASTS / WEBCAST-EQS /
     IRWEBCASTING / Q4INC ALL RETURNED "UNSUPPORTED URL"; ONLY YOUTUBE AND HITACHI.COM RESOLVED}
    [CONFIDENCE: CONFIRMED — probe run against live urls; 23,690 of 28,385 video urls are these platforms]."""
    body = await request.json()
    url, wait = _args(body, 6000)
    if not url:
        return web.json_response({"error": "missing url"}, status=400)
    out = await _call(_capture_media, url, wait)
    if not (out or {}).get("media"):
        _loud(f"capture_media({url}) → 0 streams (error={(out or {}).get('error')!r})")
    return web.json_response(out or {})


async def h_health(_request: web.Request) -> web.Response:
    """GET /health → liveness + what the box is doing. `browser` is the real signal: watercrawl self-skips to empty
    results when the browser is unavailable, so a service that answers 200 with browser=false is answering with
    garbage and the client must treat it as down."""
    return web.json_response({
        "ok": True,
        "browser": bool(browser_available()),
        "uptime_s": round(time.time() - _stats["started"], 1),
        "inflight": _stats["inflight"],
        "total": _stats["total"],
        "by_method": _stats["by_method"],
        "browsers": int(os.environ.get("IR_WATERCRAWL_BROWSERS", "3")),
        "shot_concurrency": int(os.environ.get("WATERCRAWL_SHOT_CONCURRENCY", "4")),
    })


def build_app() -> web.Application:
    """Wire the routes. client_max_size is raised because a render POST is tiny but nothing stops a caller from sending
    a long url list later; the RESPONSE (shot_b64) is the big direction and is not bounded by this."""
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_post("/render_shot", h_render_shot)
    app.router.add_post("/render_full", h_render_full)
    app.router.add_post("/render_detail", h_render_detail)
    app.router.add_post("/capture_media", h_capture_media)
    app.router.add_get("/health", h_health)
    return app


def main() -> None:
    """Entry point — `python -m providers.watercrawl.service`. Binds 0.0.0.0 so the VPC can reach it; see the module
    docstring for why that is not an exposure."""
    _loud(f"starting on :{PORT} (browsers={os.environ.get('IR_WATERCRAWL_BROWSERS', '3')}, "
          f"shot_conc={os.environ.get('WATERCRAWL_SHOT_CONCURRENCY', '4')})")
    web.run_app(build_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
