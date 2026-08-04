"""service — the RunPod pod's HTTP face for the two heavy tools: Docling document extraction and faster-whisper
transcription. One module, launched TWICE with different devices.

用一句话讲完: 在 RunPod 那台机器上起 aiohttp,worker 把文档/音频的原始 bytes POST 过来 → 服务调本地的
docling_extract / transcribe → JSON 吐回结果。**同一个文件起两个进程**: 一个 CUDA_VISIBLE_DEVICES="" 只吃 96 个
空闲 CPU 核跑 Docling,一个 CUDA_VISIBLE_DEVICES=0 吃 A40 跑 whisper —— 一个进程里做不到给两个模型分配不同设备。

WHY 分两个进程而不是一个:
  · Docling 上不了 GPU —— A40 的 46GB 已被 vLLM 占了 40.3GB,只剩 5.1GB,利用率 100%
    {NVIDIA-SMI 2026-08-04 "NVIDIA A40, 46068 MIB, 40299 MIB USED, 5190 MIB FREE, 100% UTILIZATION"}
  · whisper 必须上 GPU —— large-v3 在 CPU 上跑得比实时还慢,一个 60 分钟的电话会议要占住一个 worker 一个多小时,
    这正是 transcribe.py 不得不加 15 分钟上限的原因,而那个上限等于直接丢掉绝大多数财报电话会
  · 那台机器有 96 个 vCPU 基本闲着(503GB RAM 只用了 58GB),而瓶颈一直是 ir-media-8 的 8 个核
    {NVIDIA-SMI 2026-08-04 "96 VCPU; MEM: 503 TOTAL 58 USED 165 FREE"}
[CONFIDENCE: CONFIRMED — both read off the live pod].

上游触发: providers.tools_remote 客户端,由 tools/officeall/__init__.py 和 tools/audio_extract/__init__.py 的
分流决定走本地还是这里。下游连接: 本进程内的 Docling 单例 / faster-whisper 单例。

WIRE FORMAT: 请求体是**裸 bytes**,不是 base64。一个 choruscall 的财报音频是 91.7 MB
{SERVER CONTENT-LENGTH: 91,723,583},base64 会把它涨成 122 MB,而这条链路要穿过一条 SSH 隧道 —— 33% 的膨胀在这里
是实打实的传输时间。参数走 query string,响应才是 JSON。
[CONFIDENCE: CONFIRMED — the file size is the server's own Content-Length].
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from aiohttp import web

PORT = int(os.environ.get("TOOLS_SERVICE_PORT", "8101"))

# fetch.py refuses anything over 300 MB before it ever reaches a tool, so that is the real ceiling on what can arrive
# here. 400 MB leaves margin without inviting an unbounded body.
MAX_BODY = int(os.environ.get("TOOLS_SERVICE_MAX_BODY_MB", "400")) * 1024 * 1024

_stats: dict = {"started": time.time(), "inflight": 0, "docling": 0, "whisper": 0, "errors": 0}


def _loud(msg: str) -> None:
    """Fail-loud channel — a tool failing on the pod must be visible in the pod's log, not inferred from empty results."""
    print(f"[tools.service] {msg}", file=sys.stderr, flush=True)


async def _blocking(fn, *args, **kw):
    """Run one SYNC tool call off the event loop.

    Both tools are heavy blocking CPU/GPU work with no async form: Docling's convert() is a synchronous model pass and
    faster-whisper's transcribe() decodes through CTranslate2. Calling either on the aiohttp loop would freeze every
    other in-flight request for the whole document — measured at up to 735s for a single pdf on shared cores.
    {MEASURED 2026-08-03 — EV_013_DOCX 735.2s WITH OCR ON, 568.6s WITH OCR OFF, CONCURRENCY 4 ON 8 SHARED CORES}
    [CONFIDENCE: CONFIRMED — per-document timings from the dataset run].

    Concurrency is bounded by the executor, sized from TOOLS_SERVICE_CONCURRENCY, because unlike watercrawl these tools
    have NO internal semaphore — nothing else would stop 200 queued pdfs from each grabbing threads at once."""
    _stats["inflight"] += 1
    try:
        return await asyncio.get_running_loop().run_in_executor(_POOL, lambda: fn(*args, **kw))
    finally:
        _stats["inflight"] -= 1


async def h_docling(request: web.Request) -> web.Response:
    """POST /docling_extract?fmt=pdf&structured=0, body = raw document bytes
       → {markdown, tables, n_pages, n_tables, via, warnings, structured?}

    Returns the local function's dict untouched so the client stays a pure transport. `via` tells the caller afterwards
    whether the text path was enough (docling:text) or the scanned-pdf OCR retry had to fire (docling:ocr)."""
    from tools.officeall.extract import docling_extract        # lazy: importing docling pulls torch + layout models

    data = await request.read()
    fmt = request.query.get("fmt", "pdf")
    structured = request.query.get("structured", "0") == "1"
    if not data:
        return web.json_response({"error": "empty-body"}, status=400)
    t0 = time.time()
    out = await _blocking(docling_extract, data, fmt, want_structured=structured)
    _stats["docling"] += 1
    dt = time.time() - t0
    via = (out or {}).get("via", "?")
    if not (out or {}).get("markdown") and not (out or {}).get("tables"):
        _stats["errors"] += 1
        _loud(f"docling {fmt} {len(data)}B → EMPTY in {dt:.1f}s (via={via}, warnings={(out or {}).get('warnings')})")
    else:
        _loud(f"docling {fmt} {len(data)}B → {len((out or {}).get('markdown') or '')} chars, "
              f"{len((out or {}).get('tables') or [])} tables in {dt:.1f}s (via={via})")
    return web.json_response(out or {})


async def h_transcribe(request: web.Request) -> web.Response:
    """POST /transcribe, body = raw audio bytes → {text, segments, language, duration, reason}

    transcribe() returns a 5-tuple; JSON has no tuple, so the fields are named on the wire and the client rebuilds it.
    `reason` is the fail-loud channel and must survive the round trip verbatim — 'too-long:…' (a policy refusal) and
    'transcribe-failed:…' (a defect) mean different things to the caller and must not be collapsed."""
    from tools.audio_extract.transcribe import transcribe       # lazy: loads faster-whisper + the large-v3 weights

    data = await request.read()
    if not data:
        return web.json_response({"error": "empty-body"}, status=400)
    t0 = time.time()
    text, segments, language, duration, reason = await _blocking(transcribe, data)
    _stats["whisper"] += 1
    dt = time.time() - t0
    # A realtime factor is the number worth logging: it is what decides whether this box can keep up with the queue.
    rtf = (duration / dt) if dt > 0 and duration else 0.0
    if reason:
        _stats["errors"] += 1
        _loud(f"whisper {len(data)}B → reason={reason!r} in {dt:.1f}s")
    else:
        _loud(f"whisper {len(data)}B → {len(text)} chars, {len(segments)} segs, "
              f"{duration/60:.1f}min audio in {dt:.1f}s ({rtf:.1f}x realtime)")
    return web.json_response({"text": text, "segments": segments, "language": language,
                              "duration": duration, "reason": reason})


async def h_health(_request: web.Request) -> web.Response:
    """GET /health → liveness + which device this process actually got.

    `device` is the load-bearing field: the whole point of running two processes is that one is on CPU and one is on
    CUDA, and a whisper process that silently fell back to CPU would still answer 200 while being ~100x too slow."""
    dev = "unknown"
    try:
        import torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:                                           # noqa: BLE001 — torch absent → nothing to report
        pass
    return web.json_response({
        "ok": True,
        "device": dev,
        "cuda_visible": os.environ.get("CUDA_VISIBLE_DEVICES", "<unset>"),
        "concurrency": _CONC,
        "uptime_s": round(time.time() - _stats["started"], 1),
        "inflight": _stats["inflight"],
        "docling": _stats["docling"],
        "whisper": _stats["whisper"],
        "errors": _stats["errors"],
    })


# Sized per process, not per box: the Docling process wants many threads (96 idle cores, each document is mostly
# single-threaded model work), the whisper process wants few (one GPU, concurrent decodes contend for the same 5.1 GB
# of free VRAM and would OOM).
_CONC = int(os.environ.get("TOOLS_SERVICE_CONCURRENCY", "8"))
_POOL = None                                                    # built in main(), after _CONC is read


def build_app() -> web.Application:
    """Wire the routes. Both endpoints are registered in both processes, but each process only ever RECEIVES one kind —
    the client routes docling to one port and whisper to the other — and both tools are lazy singletons, so the unused
    model is never loaded."""
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_post("/docling_extract", h_docling)
    app.router.add_post("/transcribe", h_transcribe)
    app.router.add_get("/health", h_health)
    return app


def main() -> None:
    """Entry point — `python -m tools.service`. Device selection is EXTERNAL (CUDA_VISIBLE_DEVICES in the unit file),
    not a flag here, so it applies to every library in the process including ones we do not call directly."""
    global _POOL
    from concurrent.futures import ThreadPoolExecutor
    _POOL = ThreadPoolExecutor(max_workers=_CONC, thread_name_prefix="tool")
    _loud(f"starting on :{PORT} (concurrency={_CONC}, CUDA_VISIBLE_DEVICES="
          f"{os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>')!r}, max_body={MAX_BODY // 1024 // 1024}MB)")
    web.run_app(build_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
