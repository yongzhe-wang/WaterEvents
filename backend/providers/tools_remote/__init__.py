"""tools_remote — the client half of the heavy-tool split: docling_extract and transcribe, executed on the RunPod pod
instead of on the worker's own cores.

用一句话讲完: 两个函数的签名跟本地版**一模一样**,内部把原始 bytes POST 到 pod 上的 tools.service,再把 JSON 还原成
本地版返回的形状 —— 所以 media_agent 的调用点一行不改,只靠 DOCLING_REMOTE_URL / WHISPER_REMOTE_URL 两个环境变量
切换(分流点在 tools/officeall/__init__.py 和 tools/audio_extract/__init__.py)。

WHY 这两个工具要搬走, 以及为什么搬去**不同的设备**:

  · Docling → CPU。它在 ir-media-8 上跟 Chromium 抢 8 个核,一个文档中位数 118 秒(关掉 OCR 之后;开着是 152.7 秒)。
    按这个速度,146,254 个 PDF URL 要 50 天才能过一遍。而 A40 的显存已经被 vLLM 占满(46GB 里只剩 5.1GB,利用率
    100%),上不了 GPU。RunPod 那台却有 96 个 vCPU 基本闲着 —— 并发从 4 提到 24 就是 6 倍吞吐,零新增成本。
    {MEASURED 2026-08-03 A/B, SAME 8 DOCUMENTS, CONCURRENCY 4: OCR-ON MEDIAN 152.7s / OCR-OFF MEDIAN 118.0s;
     TOTAL 1731.9s → 1269.7s = 1.36x ONLY}
    [CONFIDENCE: CONFIRMED — matched-pair A/B on identical events, both runs on ir-media-8].

  · whisper → GPU。这个没得选。large-v3 在 CPU 上跑得**比实时还慢**,一个 60 分钟的财报电话会议要占住一个 worker
    一个多小时 —— 这正是 transcribe.py 不得不加 15 分钟上限的原因,而那个上限等于直接丢掉绝大多数电话会,砍在
    coverage 上。GPU 上是几十倍实时,而 large-v3 走 CTranslate2 只要约 3.1GB,塞得进剩下的 5.1GB。
    {MEASURED 2026-08-03 — A 3-EVENT SMOKE OVER THE AUDIO STRATUM PRODUCED 0 COMPLETED EVENTS IN 22 MINUTES}
    [CONFIDENCE: CONFIRMED — the stall was observed; the 15-minute cap exists because of it].

上游触发: tools/officeall/__init__.py 与 tools/audio_extract/__init__.py 的分流。下游连接: pod 上的
tools.service,两个进程(CUDA_VISIBLE_DEVICES="" 跑 Docling,CUDA_VISIBLE_DEVICES=0 跑 whisper)。

FAIL-LOUD CONTRACT: 跟本地版一样 never raises,但传输失败带**独有的** reason —— docling 走
`via="transport-error"`,whisper 走 `reason="transport-error:…"`。绝不复用本地已有的失败词汇: `via="none"` 的意思是
"Docling 和 pypdf 两条链都读不出这个文档",`reason="transcribe-failed"` 的意思是"whisper 解码这段音频失败",两者
都是**内容的问题**;传输失败是**我们自己的服务不可达**。混成一个值,一次 pod 重启就会被记成几百个文档损坏。
同一个坑 render_remote 已经在渲染层踩过并封掉了。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

_DOCLING_URL = os.environ.get("DOCLING_REMOTE_URL", "").rstrip("/")
_WHISPER_URL = os.environ.get("WHISPER_REMOTE_URL", "").rstrip("/")

# A single document measured 735.2s with OCR on and 568.6s with it off, on 8 shared cores. The pod has 96 cores, so the
# per-document time should fall — but the timeout must be sized for the WORST case we have actually seen, not the
# expected one, or a slow document turns into a false transport failure and gets retried at full cost.
# {MEASURED 2026-08-03 — EV_013_DOCX 735.2s (OCR ON) / 568.6s (OCR OFF), CONCURRENCY 4}
# [CONFIDENCE: CONFIRMED — worst observed single-document time in the dataset run].
_DOC_TIMEOUT = float(os.environ.get("DOCLING_REMOTE_TIMEOUT_S", "1200"))

# Whisper on GPU runs at many times realtime, so even the 2-hour cap decodes in minutes — but the upload of a large
# audio file crosses the SSH tunnel and is counted inside this timeout. A choruscall earnings mp3 is 91.7 MB.
# {SERVER CONTENT-LENGTH 2026-08-03: 91,723,583}
# [CONFIDENCE: CONFIRMED — the server's own header].
_AUD_TIMEOUT = float(os.environ.get("WHISPER_REMOTE_TIMEOUT_S", "1800"))

_RETRIES = int(os.environ.get("TOOLS_REMOTE_RETRIES", "2"))
_BACKOFF_S = float(os.environ.get("TOOLS_REMOTE_BACKOFF_S", "3"))


def _loud(msg: str) -> None:
    """Fail-loud channel — a pod that is down must be visible in the worker log, not inferred from empty documents."""
    print(f"[tools_remote] {msg}", file=sys.stderr, flush=True)


def _post_bytes(url: str, data: bytes, timeout: float) -> dict | None:
    """POST raw bytes → decoded json, or None when the TRANSPORT failed.

    Body is raw octet-stream, NOT base64: the payloads here are documents and audio, and a 91.7 MB mp3 would become
    122 MB under base64 — a 33% penalty paid on every single call, over an SSH tunnel.

    None is reserved for "could not reach the service". A successful call that produced an empty document returns a
    dict. The caller must be able to tell those apart — that is this module's whole fail-loud contract."""
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/octet-stream",
                                          "Content-Length": str(len(data))}, method="POST")
    last = ""
    for attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500:                                   # our bug (bad fmt, oversized body) — do not retry it
                _loud(f"{url} → {last} (client error, not retried): {e.read()[:200]!r}")
                return None
        except Exception as e:                                 # noqa: BLE001 — URLError, timeout, reset, bad json
            last = f"{type(e).__name__}: {str(e)[:120]}"
        if attempt < _RETRIES:
            time.sleep(_BACKOFF_S * (attempt + 1))             # 3s, 6s — covers a service restart, not a capacity wait
    _loud(f"{url} FAILED after {_RETRIES + 1} attempts ({last}) — pod unreachable")
    return None


def docling_extract(data: bytes, fmt: str, want_structured: bool = False) -> dict:
    """Remote twin of officeall.extract.docling_extract → {markdown, tables, n_pages, n_tables, via, warnings, structured?}.

    `fmt` and `want_structured` ride in the query string so the body stays pure bytes. The response is passed through
    untouched — including `via`, which still reports whether the server needed the scanned-pdf OCR retry
    (docling:text vs docling:ocr), so that split stays measurable from the caller's side."""
    if not _DOCLING_URL:
        _loud("DOCLING_REMOTE_URL is empty — client called with no endpoint configured")
        return {"markdown": "", "tables": [], "n_pages": 0, "n_tables": 0,
                "via": "transport-error", "warnings": ["docling-remote-unconfigured"]}
    q = f"?fmt={fmt or 'pdf'}&structured={'1' if want_structured else '0'}"
    out = _post_bytes(f"{_DOCLING_URL}/docling_extract{q}", data, _DOC_TIMEOUT)
    if out is None:
        # NOT via="none" — that means both local chains read the document and found nothing, which is a statement about
        # the DOCUMENT. This is a statement about our own pod. Keeping them distinct is what stops a pod restart from
        # being recorded as hundreds of unreadable filings.
        return {"markdown": "", "tables": [], "n_pages": 0, "n_tables": 0,
                "via": "transport-error", "warnings": ["docling-remote-unreachable"]}
    out.setdefault("tables", [])
    out.setdefault("warnings", [])
    return out


def transcribe(data: bytes) -> tuple[str, list[dict], str, float, str]:
    """Remote twin of audio_extract.transcribe.transcribe → (text, segments, language, duration, reason).

    The 5-tuple has no JSON form, so the fields are named on the wire and rebuilt here. `reason` survives verbatim
    because its VALUES carry meaning the caller acts on: '' = success, 'too-long:…' = a policy refusal (and therefore
    NOT retried with a smaller model), 'transcribe-failed:…' = a defect. Collapsing them would erase the distinction
    the local implementation deliberately introduced."""
    if not _WHISPER_URL:
        _loud("WHISPER_REMOTE_URL is empty — client called with no endpoint configured")
        return "", [], "", 0.0, "transport-error:unconfigured"
    out = _post_bytes(f"{_WHISPER_URL}/transcribe", data, _AUD_TIMEOUT)
    if out is None:
        return "", [], "", 0.0, "transport-error:pod-unreachable"
    return (out.get("text", "") or "",
            list(out.get("segments") or []),
            out.get("language", "") or "",
            float(out.get("duration") or 0.0),
            out.get("reason", "") or "")


def _health(url: str) -> dict:
    """One service's /health, or {} when unreachable. Shared by the two probes below."""
    if not url:
        return {}
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:                                     # noqa: BLE001 — unreachable = unavailable, say so
        _loud(f"health check failed for {url} ({type(e).__name__})")
        return {}


def docling_available() -> bool:
    """True when the Docling service answers. Device is not asserted here: Docling is SUPPOSED to be on CPU — the GPU
    has 5.1 GB free and vLLM owns it."""
    return bool(_health(_DOCLING_URL).get("ok"))


def whisper_available() -> bool:
    """True when the whisper service answers AND actually got the GPU.

    device is load-bearing, not decoration: a whisper process that silently fell back to CPU still answers 200 while
    running slower than realtime, which is precisely the failure this whole split exists to eliminate. A CPU whisper is
    reported as UNAVAILABLE on purpose — degrading to it quietly would restore the original problem invisibly."""
    h = _health(_WHISPER_URL)
    if h.get("ok") and h.get("device") != "cuda":
        _loud(f"whisper service is UP but device={h.get('device')!r} — expected 'cuda'; treating as unavailable")
        return False
    return bool(h.get("ok"))
