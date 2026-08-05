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
from .render import render_shot as _render_shot, expand_events_page as _expand_events_page
from .orchestrator import render_full as _render_full, render_detail as _render_detail
from .capture import capture_media as _capture_media
from .runtime import browser_available

PORT = int(os.environ.get("RENDER_SERVICE_PORT", "8100"))

# One counter per entry so /health reports what the box is actually doing. Plain ints are safe here: every mutation
# happens on the aiohttp event loop thread, and the blocking work is what gets pushed to the executor, not the counting.
_stats: dict = {"started": time.time(), "inflight": 0, "total": 0, "errors": 0, "by_method": {}}


# The residential proxy lives HERE, in this box's own environment, and is deliberately never accepted over the wire:
# the client that calls us already knows it should not ship a credential to the machine that already holds it.
# {RENDER.ENV on ir-render-16 carries WEBSHARE_PROXY, inherited from ir-media-8's fleet.env at provisioning}
# [CONFIDENCE: CONFIRMED — written by the provisioning step and read back as "WEBSHARE_PROXY: 1 条"].
_PROXY = os.environ.get("WEBSHARE_PROXY") or None


def _asdict(r) -> dict:
    """A DocResult / AudioResult dataclass → a plain dict for the wire. dataclasses.asdict handles the nested lists and
    dicts; the client rebuilds the SAME dataclass on the far side, so every call site keeps its original type."""
    from dataclasses import asdict
    return asdict(r)


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


async def h_expand_events_page(request: web.Request) -> web.Response:
    """POST /expand_events_page {url} → {inline}

    The events-page expansion: drive the year filter and the load-more control to reveal the full historical list
    instead of the 3-5 entries a static render sees. This is the SECOND browser-driven entry the crawl uses, and it was
    missed in the first cut of the split — engine.py calls `watercrawl.expand_events_page` directly, and because that
    name was not rebound it kept running IN THE WORKER, launching a local Chromium there and burning 251% CPU across
    130 processes on the very box this split exists to free.
    {OBSERVED 2026-08-04 AFTER CUTOVER — ir-media-8: "130 个进程, 合计 251.1% CPU", ALL STARTED AFTER THE RESTART,
     cgroup=waterevents-worker@1.service, WHILE RENDER_REMOTE_URL WAS SET IN THAT SAME PROCESS}
    [CONFIDENCE: CONFIRMED — process ages, parent chain and cgroup all read off the live box].

    It is expensive on purpose — up to 6 per-year navigations plus load-more rounds, ~60-80s — which is exactly why it
    belongs on the render box rather than next to the DB writes. It is also not optional: it is the fix for pages that
    show only upcoming events, which was 51% of the low-event-count companies."""
    body = await request.json()
    url, _ = _args(body, 0)
    if not url:
        return web.json_response({"error": "missing url"}, status=400)
    inline = await _call(_expand_events_page, url)
    if not inline:
        _loud(f"expand_events_page({url}) → nothing expanded (no year-bar / load-more control, or both self-skipped)")
    return web.json_response({"inline": inline or ""})


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


async def _gated_fetch(url: str, fn, *args):
    """Run one FETCH-and-extract behind the same politeness gate every render already goes through.

    THIS is the point of moving downloads here, more than the memory it frees on ir-media-8. Before this, the two paths
    were uncoordinated: renders consulted politeness, downloads did not, and both hammered the same host from two
    different machines with neither aware of the other.
    {GREP 2026-08-04 — providers/watercrawl/render.py 15 politeness references, capture.py 5,
     tools/officeall/fetch.py 0, tools/audio_extract/fetch.py 0}
    [CONFIDENCE: CONFIRMED — counted across the whole backend].
    Running the download in THIS process means it shares one per-host pacing cursor with the renders, so a page render
    followed by its own pdf download is now two paced requests to one host, not two racing ones from two IPs.

    url_allowed_async first (scheme + SSRF + robots), then wait_turn_async, which RESERVES this host's next slot rather
    than sleeping a fixed interval — the reservation is what stops N concurrent callers from waking together and hitting
    the host as one burst {POLITENESS.PY "A NAIVE SLEEP(INTERVAL) WOULD LET N COROUTINES WAKE SIMULTANEOUSLY AND HIT THE
    HOST TOGETHER — WHICH IS THE PILE-UP THIS MODULE EXISTS TO PREVENT"}."""
    from . import politeness
    ok, why = await politeness.url_allowed_async(url)
    if not ok:
        return None, why                                    # 'bad-scheme' / 'ssrf-blocked' / 'robots-denied'
    await politeness.wait_turn_async(url)
    return await _call(fn, *args), ""


async def h_fetch_doc(request: web.Request) -> web.Response:
    """POST /fetch_doc {url, structured?} → the DocResult fields.

    Downloads on THIS box and forwards the bytes to the pod, so ir-media-8 never holds them. The forwarding half is
    already configured here as DOCLING_REMOTE_URL, which makes this handler literally the code ir-media-8 used to run —
    same fetch, same remote extract — just relocated to the machine whose IP the site has already seen rendering."""
    from tools.officeall import extract as office_extract   # lazy: pulls the officeall package + its lazy docling client

    body = await request.json()
    url = str(body.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "missing url"}, status=400)
    want = bool(body.get("structured"))
    r, refused = await _gated_fetch(url, office_extract, url, _PROXY, want)
    if r is None:
        _loud(f"fetch_doc({url}) → refused:{refused}")
        return web.json_response({"source": "url", "error": f"refused:{refused}", "via": ""})
    if not r.ok:
        _loud(f"fetch_doc({url}) → NOT OK: {r.error!r} (via={r.via!r})")
    return web.json_response(_asdict(r))


async def h_fetch_audio(request: web.Request) -> web.Response:
    """POST /fetch_audio {url} → the AudioResult fields. Same shape as /fetch_doc; the audio leg is the one that makes
    the memory argument concrete — a single choruscall earnings mp3 is 91.7 MB {SERVER CONTENT-LENGTH 91,723,583}."""
    from tools.audio_extract import extract as audio_extract  # lazy: pulls the audio package + its lazy whisper client

    body = await request.json()
    url = str(body.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "missing url"}, status=400)
    r, refused = await _gated_fetch(url, audio_extract, url, _PROXY)
    if r is None:
        _loud(f"fetch_audio({url}) → refused:{refused}")
        return web.json_response({"source": "url", "error": f"refused:{refused}", "via": ""})
    if not r.ok:
        _loud(f"fetch_audio({url}) → NOT OK: {r.error!r}")
    return web.json_response(_asdict(r))


def _host_stats() -> dict | None:
    """Read this machine's CPU + memory vitals and return them as the 'host' sub-dict for /health.

    WHY this function exists: the dashboard used to label ir-media-8's own loadavg as "render", which was wrong the
    moment the render workload moved to ir-render-16 (this box). The 'host' key lets the frontend read ir-render-16's
    true figures via its own /health endpoint so the card is labelled and sourced correctly.
    {CONTRACT 2026-08-05 "A) providers/watercrawl/service.py GET /health gains a top-level 'host' key:
     {'cores': <int>, 'load1': <float 2dp>, 'load_pct': <int>, 'mem_used_mb': <int>, 'mem_total_mb': <int>}
     Read from os.cpu_count() and os.getloadavg()[0]; memory from /proc/meminfo (MemTotal, MemAvailable)"}
    [CONFIDENCE: CONFIRMED 100% — direct user instruction in this session; contract is frozen].

    Returns None on ANY failure — the caller omits the key rather than 500ing. Health endpoints must never crash.
    """
    try:
        # os.cpu_count() returns the logical CPU count (threads, not physical cores), which matches what `nproc`
        # and /proc/cpuinfo report and is the right denominator for load_pct.
        # {PYTHON DOCS "os.cpu_count() — return the number of CPUs in the system; None if undetermined"}
        # [CONFIDENCE: CONFIRMED — standard library, unambiguous].
        cores = os.cpu_count() or 1

        # getloadavg()[0] is the 1-minute load average — same value `uptime` shows in the first column.
        # Dividing by cores gives the per-core fraction; clamp to [0, 100] before converting to int so a momentary
        # spike above 100% doesn't confuse the dashboard gauge.
        # {PYTHON DOCS "os.getloadavg() — return the number of processes in the system run queue averaged over the last
        #  1, 5, and 15 minutes; OSError on platforms that do not support this (e.g. Windows)"}
        # [CONFIDENCE: CONFIRMED — Linux / GCP VM; safe on this box].
        load1 = round(os.getloadavg()[0], 2)
        load_pct = min(100, int(load1 / cores * 100))

        # /proc/meminfo gives MemTotal and MemAvailable in kB; used = total − available mirrors what `free -m` shows
        # as the "available" column (real available, not free+cached) — the most operationally meaningful metric.
        # {LINUX KERNEL DOCS "MemAvailable: an estimate of how much memory is available for starting new applications,
        #  without swapping. Calculated from MemFree, plus memory reclaimable from buffers + page cache + slabs."}
        # [CONFIDENCE: CONFIRMED — /proc/meminfo format is stable across kernel ≥3.14].
        mem_total_kb = mem_avail_kb = 0
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail_kb = int(line.split()[1])
                # Both found → stop early; /proc/meminfo has ~50 lines and these two appear near the top.
                if mem_total_kb and mem_avail_kb:
                    break

        return {
            "cores": cores,
            "load1": load1,
            "load_pct": load_pct,
            "mem_used_mb": (mem_total_kb - mem_avail_kb) // 1024,   # integer division; kB → MB
            "mem_total_mb": mem_total_kb // 1024,
        }
    except Exception as exc:
        # Any failure (OSError on getloadavg, IOError on /proc/meminfo, unexpected parse error) → return None.
        # The caller drops the 'host' key entirely rather than emitting nulls, which is cleaner for the dashboard.
        _loud(f"_host_stats() failed: {exc!r}")
        return None


async def h_health(_request: web.Request) -> web.Response:
    """GET /health → liveness + what the box is doing. `browser` is the real signal: watercrawl self-skips to empty
    results when the browser is unavailable, so a service that answers 200 with browser=false is answering with
    garbage and the client must treat it as down.

    `host` is the new key (2026-08-05) that reports THIS machine's CPU + memory so the frontend dashboard can show
    ir-render-16's true loadavg under the "render" card rather than ir-media-8's figures.
    {CONTRACT 2026-08-05 "A) GET /health gains a top-level 'host' key ... /health must never 500"}
    [CONFIDENCE: CONFIRMED 100% — frozen contract, direct user instruction]."""
    # The break log, surfaced rather than left in the log file. `robots_override` is host → count of urls this process
    # took past an explicit Disallow. Empty when the escape hatch is shut, which is the default. Reporting it HERE is
    # the point: an override nobody can see afterwards is the failure mode, and a health endpoint is checked, whereas
    # a log line scrolls away. {POLITENESS.PY "AN OVERRIDE YOU CANNOT SEE AFTERWARDS IS THE THING TO AVOID"}
    from . import politeness                              # lazy: keeps the health route free of import-order coupling
    ovr = politeness.override_report()

    # Build the base payload first, then conditionally add 'host' — omitting it (rather than null) when unreadable,
    # which is cleaner than a null block for dashboard code that does `r.host?.cores`.
    payload: dict = {
        "ok": True,
        "browser": bool(browser_available()),
        "uptime_s": round(time.time() - _stats["started"], 1),
        "inflight": _stats["inflight"],
        "total": _stats["total"],
        "by_method": _stats["by_method"],
        "browsers": int(os.environ.get("IR_WATERCRAWL_BROWSERS", "3")),
        "shot_concurrency": int(os.environ.get("WATERCRAWL_SHOT_CONCURRENCY", "4")),
        "ignore_robots": politeness.IGNORE_ROBOTS,
        "robots_override": ovr,
        "robots_override_total": sum(ovr.values()),
    }

    # Attempt to read host vitals; omit the key on failure rather than crashing the endpoint.
    host = _host_stats()
    if host is not None:
        payload["host"] = host

    return web.json_response(payload)


def build_app() -> web.Application:
    """Wire the routes. client_max_size is raised because a render POST is tiny but nothing stops a caller from sending
    a long url list later; the RESPONSE (shot_b64) is the big direction and is not bounded by this."""
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_post("/render_shot", h_render_shot)
    app.router.add_post("/render_full", h_render_full)
    app.router.add_post("/render_detail", h_render_detail)
    app.router.add_post("/expand_events_page", h_expand_events_page)
    app.router.add_post("/capture_media", h_capture_media)
    app.router.add_post("/fetch_doc", h_fetch_doc)
    app.router.add_post("/fetch_audio", h_fetch_audio)
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
