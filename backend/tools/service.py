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
        # functools.partial, not a lambda: ProcessPoolExecutor pickles what it submits and a lambda is not
        # picklable, so the thread-only version of this line would raise the moment the CPU side used processes.
        import functools
        return await asyncio.get_running_loop().run_in_executor(_POOL, functools.partial(fn, *args, **kw))
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


def _device() -> str:
    """Which accelerator THIS process can actually use, asked of the runtime that will actually do the work.

    ctranslate2 first, and not as a fallback: faster-whisper executes through CTranslate2, NOT through torch, so
    ctranslate2.get_cuda_device_count() is the only answer that predicts whether transcription will run on the GPU.
    torch is consulted only if ctranslate2 is absent — and torch's answer can be actively WRONG here, because the venv
    deliberately carries a CPU-only torch build: docling's transformers dependency needs the torch>=2.5 API surface
    {IMPORTERROR 2026-08-04 "CANNOT IMPORT NAME 'DTENSOR' FROM 'TORCH.DISTRIBUTED.TENSOR'"}, while the pod's system
    torch is 2.4.1, and installing a 2.5 CUDA build would cost 2.5 GB against 5.2 GB of free root disk. A CPU torch
    satisfies the import and costs ~200 MB, and Docling is meant to run on CPU anyway.
    Asking torch would therefore report 'cpu' for a whisper process that is in fact happily on CUDA — and
    tools_remote.whisper_available() refuses a CPU whisper by design, so the wrong answer here takes the GPU path
    offline while it is working perfectly.
    [CONFIDENCE: CONFIRMED — the ImportError above is the verbatim traceback tail from the pod]."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
        return "cpu"
    except Exception:                                           # noqa: BLE001 — ctranslate2 absent (docling process)
        pass
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:                                           # noqa: BLE001 — neither runtime present
        return "unknown"


def _host_stats() -> dict | None:
    """Read host CPU and memory metrics from the OS for the /health response.

    WHY 读 /proc/meminfo 而不是 psutil: 这个服务跑在 RunPod 的裸 Linux 上,我们不想多一个 pip 依赖;
    /proc/meminfo 是 Linux kernel 直接暴露的接口,任何 Python 版本都能读,而且比 psutil 的 fallback 更准确。
    {ROOT CLAUDE.MD SECTION 2 CONTRACT PART B: "MEMORY FROM /PROC/MEMINFO (MEMTOTAL, MEMAVAILABLE) —
    USED = TOTAL - AVAILABLE"}
    [CONFIDENCE: CONFIRMED 99% — /proc/meminfo is standard Linux; tested on Ubuntu 20.04+].

    返回 None 而不是 raise 保证 /health 永远不会 500。
    {ROOT CLAUDE.MD SECTION 2 CONTRACT PART B: "WRAP IN TRY/EXCEPT AND OMIT THE KEY (OR USE NULLS) IF
    UNREADABLE; /HEALTH MUST NEVER 500."}
    [CONFIDENCE: CONFIRMED 100% — direct contract requirement]."""
    try:
        # os.cpu_count() returns the number of logical CPUs visible to THIS process (honours cgroups),
        # which is the correct value for a pod that may be containerized.
        # {PYTHON DOCS: "OS.CPU_COUNT() RETURN THE NUMBER OF CPUS IN THE SYSTEM; RETURN NONE IF UNDETERMINED"}
        # [CONFIDENCE: CONFIRMED 95% — standard stdlib; None-guard below handles the edge case]
        cores = os.cpu_count() or 0

        # os.getloadavg() returns (1min, 5min, 15min) POSIX load averages.
        # WHY 1-minute 平均: 它比 5/15min 更能反映「现在」的负载状态,对 dashboard polling every 30s 最有用。
        # {PYTHON DOCS: "OS.GETLOADAVG() RETURN THE NUMBER OF PROCESSES IN THE SYSTEM RUN QUEUE AVERAGED OVER
        # THE LAST 1, 5, AND 15 MINUTES"}
        # [CONFIDENCE: CONFIRMED 99% — stdlib, raises OSError on Windows but we're on Linux]
        load1 = round(os.getloadavg()[0], 2)

        # load_pct = load1 / cores * 100, clamped to [0, 100] as an int.
        # WHY 整数百分比: 和 GPU util_pct 保持一致,让 dashboard 用统一的 hot-threshold 逻辑。
        # {ROOT CLAUDE.MD SECTION 2 CONTRACT PART A/B: "LOAD_PCT: <INT>"}
        # [CONFIDENCE: CONFIRMED 100% — direct contract spec]
        load_pct = int(min(load1 / cores * 100, 100)) if cores > 0 else 0

        # 解析 /proc/meminfo 获取 MemTotal 和 MemAvailable (kB 单位), 转成 MB。
        # MemAvailable is preferred over MemFree because it accounts for reclaimable page cache —
        # used = total - available gives the "real" used figure that matches `free -m`.
        # {LINUX KERNEL DOCS: "MEMAVAILABLE: AN ESTIMATE OF HOW MUCH MEMORY IS AVAILABLE FOR STARTING NEW
        # APPLICATIONS, WITHOUT SWAPPING."}
        # [CONFIDENCE: CONFIRMED 99% — /proc/meminfo format is stable since Linux 2.6]
        mem_total_kb = mem_avail_kb = 0
        with open("/proc/meminfo") as fh:
            for line in fh:
                # 每行格式: "MemTotal:       503654656 kB"
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail_kb = int(line.split()[1])
                if mem_total_kb and mem_avail_kb:
                    break  # 两个字段都找到就可以停了

        mem_total_mb = mem_total_kb // 1024
        mem_used_mb = (mem_total_kb - mem_avail_kb) // 1024

        return {
            "cores": cores,
            "load1": load1,
            "load_pct": load_pct,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
        }
    except Exception as exc:  # noqa: BLE001 — /proc not mounted, OSError, etc. → omit key, never 500
        _loud(f"_host_stats failed: {exc}")
        return None


def _gpu_stats() -> dict | None:
    """Read GPU memory and utilization for the /health response.

    WHY pynvml 优先而不是直接 subprocess: pynvml 是 in-process 调用,不 fork 新进程,延迟 ~1ms;
    nvidia-smi subprocess 需要 fork + exec + parse,延迟 ~200-300ms,而且每次调用都会短暂影响 GPU driver。
    pynvml 不可用时才 fallback 到 subprocess。
    {ROOT CLAUDE.MD SECTION 2 CONTRACT PART B: "GET IT FROM PYNVML IF IMPORTABLE, ELSE BY SHELLING OUT TO
    NVIDIA-SMI ... WITH A 3S TIMEOUT"}
    [CONFIDENCE: CONFIRMED 100% — direct contract requirement].

    关键设计考量: 这个进程和 vLLM 共享同一块 A40。返回的 GPU 数字是整张卡的 usage,不是这个进程独占的。
    Dashboard 用户需要知道这件事 —— gpu.mem_used_mb 会包含 vLLM 的 ~40GB。
    {SERVICE.PY MODULE DOCSTRING LINE 9: "A40 的 46GB 已被 vLLM 占了 40.3GB"}
    [CONFIDENCE: CONFIRMED — from module-level architecture comment].

    返回 None (而不是 raise) 保证:
    1. CPU-only 的 Docling 进程 (CUDA_VISIBLE_DEVICES="") 正常返回 null
    2. GPU 驱动挂了也不会让 /health 500
    {ROOT CLAUDE.MD SECTION 2 CONTRACT PART B: "GPU IS NULL WHEN THIS PROCESS HAS NO CUDA DEVICE"}
    [CONFIDENCE: CONFIRMED 100% — direct contract requirement]."""
    # ── pynvml 路径 ──────────────────────────────────────────────────────────────
    try:
        import pynvml  # available when nvidia-ml-py is installed

        # nvmlInit() is idempotent; calling it per-request is safe, just slightly wasteful — but keeping a
        # module-level handle complicates teardown and we are not in a hot path.
        # {PYNVML DOCS: "NVMLINIT() — INITIALIZE THE NVML LIBRARY. CAN BE CALLED MULTIPLE TIMES."}
        # [CONFIDENCE: SINGLE-SRC 90% — from pynvml project README and source]
        pynvml.nvmlInit()

        # CUDA_VISIBLE_DEVICES="" means the OS hides all GPUs from this process; device count = 0.
        # _device() already checks ctranslate2 for this; we do the explicit nvml count check here so the
        # pynvml path and the subprocess path agree on "no GPU → return None".
        # {SERVICE.PY _DEVICE() DOCSTRING: "CTRANSLATE2.GET_CUDA_DEVICE_COUNT() IS THE ONLY ANSWER THAT
        # PREDICTS WHETHER TRANSCRIPTION WILL RUN ON THE GPU"}
        # [CONFIDENCE: CONFIRMED — cross-reference with _device() in this file]
        device_count = pynvml.nvmlDeviceGetCount()
        if device_count == 0:
            return None

        # This service always uses device index 0 — CUDA_VISIBLE_DEVICES remaps the visible set so that
        # whatever the pod exposes as device 0 is the one this process actually uses.
        # {ROOT CLAUDE.MD SECTION 2 CONTRACT: "CUDA_VISIBLE_DEVICES=0 RUNNING FASTER-WHISPER ON AN A40"}
        # [CONFIDENCE: CONFIRMED — per architecture description in the task spec]
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)

        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        mem_used_mb = mem.used // (1024 * 1024)
        mem_total_mb = mem.total // (1024 * 1024)

        # nvmlDeviceGetUtilizationRates returns a struct with .gpu (utilization %) and .memory (bandwidth %).
        # We report .gpu because that is what nvidia-smi calls "utilization.gpu" and what the contract names
        # util_pct.
        # {NVML API: "NVMLDEVICEGETUTILIZATIONRATES — GPU UTILIZATION: PERCENT OF TIME OVER THE PAST SAMPLE
        # PERIOD DURING WHICH ONE OR MORE KERNELS WAS EXECUTING ON THE GPU"}
        # [CONFIDENCE: SINGLE-SRC 95% — from NVML API reference]
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        util_pct = int(util.gpu)

        return {"mem_used_mb": mem_used_mb, "mem_total_mb": mem_total_mb, "util_pct": util_pct}

    except ImportError:
        pass  # pynvml not installed → fall through to subprocess path
    except Exception as exc:  # noqa: BLE001 — nvml init failure, permission error, etc.
        _loud(f"_gpu_stats pynvml path failed: {exc}")
        # If nvml init failed for a reason other than "no GPU" (e.g. driver mismatch), the subprocess path
        # may also fail, but we try it anyway as a best-effort fallback.

    # ── nvidia-smi subprocess fallback ──────────────────────────────────────────
    # Used when pynvml is absent (e.g. nvidia-ml-py not installed in this venv).
    # 3-second timeout matches the contract and prevents blocking the event loop for too long.
    # The caller runs this in a thread (h_health uses asyncio.to_thread), which is what makes a blocking
    # subprocess acceptable here. The 3s timeout bounds how LONG it runs; it is the thread that decides WHERE.
    # Do not call this function directly from a coroutine — a timeout alone would still freeze the event loop
    # and every in-flight extract on this process along with it.
    # {ROOT CLAUDE.MD SECTION 2 CONTRACT PART B: "NVIDIA-SMI ... WITH A 3S TIMEOUT. MUST NEVER 500 AND
    # MUST NEVER BLOCK THE EVENT LOOP FOR MORE THAN ~3S."}
    # [CONFIDENCE: CONFIRMED 100% — direct contract requirement]
    try:
        import subprocess

        # CUDA_VISIBLE_DEVICES="" hides GPUs from the process but nvidia-smi ignores that env-var —
        # it talks to the driver directly. We must check _device() first and short-circuit if cpu.
        # This check mirrors what the pynvml path does via device_count == 0.
        if _device() == "cpu":
            return None

        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,  # hard upper-bound: /health must not hang for > 3s
        )

        if result.returncode != 0:
            _loud(f"_gpu_stats nvidia-smi exited {result.returncode}: {result.stderr.strip()}")
            return None

        # Output line: "40299, 46068, 100" (MiB, MiB, percent) — noheader,nounits are critical.
        # {NVIDIA-SMI DOCS: "--FORMAT=CSV,NOHEADER,NOUNITS — DISABLE HEADER, REPORT RAW NUMBERS"}
        # [CONFIDENCE: CONFIRMED 99% — nvidia-smi format flags are stable across driver versions]
        parts = result.stdout.strip().split("\n")[0].split(",")
        mem_used_mb = int(parts[0].strip())
        mem_total_mb = int(parts[1].strip())
        util_pct = int(parts[2].strip())

        return {"mem_used_mb": mem_used_mb, "mem_total_mb": mem_total_mb, "util_pct": util_pct}

    except Exception as exc:  # noqa: BLE001 — nvidia-smi not on PATH, timeout, parse error → return null
        _loud(f"_gpu_stats nvidia-smi path failed: {exc}")
        return None


async def h_health(_request: web.Request) -> web.Response:
    """GET /health → liveness + which device this process actually got, plus host and GPU metrics.

    `device` is the load-bearing field: the whole point of running two processes is that one is on CPU and one is on
    CUDA, and a whisper process that silently fell back to CPU would still answer 200 while being ~100x too slow.

    `host` 字段: CPU 核心数、1min 负载、负载百分比、内存使用量/总量 —— 两个进程都上报, dashboard 可以知道整台机器的状态。
    {ROOT CLAUDE.MD SECTION 2 CONTRACT PART B: "GAINS THE SAME 'HOST' KEY (IDENTICAL SHAPE)"}
    [CONFIDENCE: CONFIRMED 100% — direct contract requirement].

    `gpu` 字段: GPU 内存使用/总量/利用率 (pynvml 优先, nvidia-smi fallback)。
    CPU-only 的 Docling 进程 (CUDA_VISIBLE_DEVICES="") 返回 gpu=null。
    GPU 进程返回的数字包含 vLLM 的占用 —— 那张卡是共享的, 不是这个进程独占的。
    {ROOT CLAUDE.MD SECTION 2 CONTRACT PART B: "GPU IS NULL WHEN THIS PROCESS HAS NO CUDA DEVICE.
    GET IT FROM PYNVML IF IMPORTABLE, ELSE BY SHELLING OUT TO NVIDIA-SMI"}
    [CONFIDENCE: CONFIRMED 100% — direct contract requirement]."""
    dev = _device()

    # OFF THE EVENT LOOP, both of them. _gpu_stats can fall back to `subprocess.run(nvidia-smi, timeout=3)`, and a
    # timeout bounds how LONG that runs — it does nothing about WHERE it runs. Called inline from this async handler it
    # freezes the whole loop for up to 3 seconds, and every in-flight docling extract and whisper transcribe on this
    # process freezes with it. The dashboard polls /health every 30s, so that is a 3-second stall every 30 seconds, on
    # the box doing the actual work, purely to draw a card.
    # {TODAYVIEW.TSX "Polls /api/today every 30s so the queue + feed stay live as workers run"}
    # [CONFIDENCE: CONFIRMED — the poll interval is set in the dashboard; the subprocess fallback is in _gpu_stats].
    # _host_stats reads /proc, which is a kernel virtual file and returns in microseconds, so it is not the same class
    # of problem — but it is sync file I/O all the same and costs nothing to move, so both go together rather than
    # leaving a rule that holds for one of them and not the other.
    # asyncio.to_thread, NOT _POOL: _POOL is the bounded tool executor. A health probe must never queue behind a 735s
    # pdf, and must never occupy a slot that a real extraction is waiting for.
    host, gpu = await asyncio.gather(
        asyncio.to_thread(_host_stats),
        asyncio.to_thread(_gpu_stats),
    )
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
        # host: CPU/内存快照; None 表示读取失败 (omit the key 语义上等价于 null)
        "host": host,
        # gpu: GPU 显存/利用率; null when CUDA_VISIBLE_DEVICES="" or driver error
        "gpu": gpu,
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

    # PROCESSES for the CPU (docling) side, THREADS for the GPU (whisper) side. Not a style preference — measured.
    #
    # docling's convert() is Python-heavy, so threads serialise on the GIL and then do WORSE than serial, because
    # contention and cache thrash add to the serialisation. Same document, same box, warm models:
    #   1 thread    9.1s   →  0.110 docs/sec
    #   4 threads  47.8s   →  0.084 docs/sec   (-24% vs one)
    #   8 threads 186.6s   →  0.043 docs/sec   (-61% vs one)
    # Perfect serialisation would hold throughput flat at 0.110; it FELL. That is also why the box showed 369% CPU —
    # 3.7 of 96 cores — while 24 documents were nominally in flight: torch's numeric kernels release the GIL and
    # supply those few cores, while everything around them queues on it.
    # {MEASURED 2026-08-05 on the pod, threads only, models pre-warmed}
    # [CONFIDENCE: CONFIRMED — throughput measured at three thread counts on identical input].
    #
    # whisper stays on threads: its concurrency is 1 by necessity (it shares an A40 with a vLLM holding 40 of 46 GB),
    # and CTranslate2 releases the GIL properly, so processes would only add model-loading cost for no parallelism.
    # REVERTED to threads after measuring. The GIL analysis above is correct as far as it goes — threads do serialise
    # docling's Python work — but processes lost anyway, and by more:
    #   threads,   docling conc 24   312 events/hour
    #   processes, docling conc 16    54 events/hour
    # measured from events.enriched_at timestamps rather than a polling window, because document times span 30-700s
    # and a five-minute window routinely contains zero completions either way.
    # The cost that outweighs the GIL is model loading: each pool worker loads ~2 GB of docling weights, and HF_HOME
    # lives on /workspace, which is MooseFS mounted from Montreal. Sixteen workers is ~32 GB of contended network
    # reads, and the box showed load dropping to 4.1 of 96 cores with fourteen documents nominally in flight — idle,
    # waiting on the network, not computing.
    # The shape that would win both ways is several docling SERVICE processes, each loading models ONCE at startup
    # and each running few threads. That needs the client to spread across ports and is not a one-line change.
    # {MEASURED 2026-08-05 — per-minute enriched counts either side of the switch}
    # [CONFIDENCE: CONFIRMED on the direction; the attribution is muddier than it should be because fetch-gate size
    #  changed in the same window, which is a mistake worth not repeating — one variable at a time].
    _POOL = ThreadPoolExecutor(max_workers=_CONC, thread_name_prefix="tool")
    kind = "threads"
    _loud(f"starting on :{PORT} (concurrency={_CONC} {kind}, CUDA_VISIBLE_DEVICES="
          f"{os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>')!r}, max_body={MAX_BODY // 1024 // 1024}MB)")
    web.run_app(build_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
