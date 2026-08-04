"""audio_extract — one tool: an audio/video url (or bytes) → its transcript.

用一句话讲完: WaterEvents 的第二个 tool,和 pdf_extract 同一套 pattern —— detect(是不是音视频)→ fetch(curl_cffi
抓 bytes)→ transcribe(本地 faster-whisper 跑 H100)→ 汇成统一的 AudioResult。旧项目那套(Gemini API + webcast/
youtube 平台适配 + glossary)被换成"本地 whisper"以匹配 WaterEvents 的本地/$0 哲学。

组织(infra,和 pdf_extract 对称):
  detect.py     — is_audio_url / maybe_audio_url          (最便宜的第一道门)
  fetch.py      — fetch_bytes(url, proxy=None) -> bytes    (Chrome 指纹 + content-type/magic + SSRF + 300MB cap)
  transcribe.py — transcribe(bytes) -> (text, segments, lang, dur)  (faster-whisper large-v3, GPU float16)
  types.py      — AudioResult                             (统一返回形状)

依赖(全 lazy,缺了就 no-op):curl_cffi(fetch)、faster_whisper + torch(transcribe)。proxy 由调用方提供。
留待后续(结构已对称,加模块即可):HLS/ffmpeg 下载层、youtube/webcast 平台适配、pyannote 说话人 diarization。
"""
from __future__ import annotations

from .detect import is_audio_url, maybe_audio_url
from .fetch import fetch_bytes
from .transcribe import transcribe
from .types import AudioResult

# ── REMOTE WHISPER SPLIT ─────────────────────────────────────────────────────────────────────────────────────────
# Setting WHISPER_REMOTE_URL rebinds transcribe to an HTTP client that runs the SAME function on the RunPod pod's A40.
# extract_bytes() below resolves the name at CALL time, so rebinding this module global is enough.
#
# WHY this one MUST reach a GPU, unlike the Docling split next door: large-v3 at cpu/int8 runs BELOW realtime on a box
# it also shares with the crawl fleet, so an hour-long earnings call holds a worker for over an hour — a 3-event smoke
# over the audio stratum completed 0 events in 22 minutes. That is why transcribe.py carries a 15-minute duration cap
# on CPU at all, and that cap silently drops most earnings calls, which is a direct hit to coverage. On the A40 the
# same file decodes in minutes, and large-v3 runs through CTranslate2 in ~3.1 GB — it fits the 5.1 GB vLLM leaves free.
# {MEASURED 2026-08-03 — A 3-EVENT SMOKE OVER THE AUDIO STRATUM PRODUCED 0 COMPLETED EVENTS IN 22 MINUTES}
# {NVIDIA-SMI 2026-08-04 "NVIDIA A40, 46068 MIB, 40299 MIB USED, 5190 MIB FREE"}
# [CONFIDENCE: CONFIRMED — the stall was observed; the cap in transcribe.py exists because of it].
#
# The client refuses a whisper service that came up on CPU (see tools_remote.whisper_available): degrading to CPU
# quietly would silently restore the exact problem this split exists to remove.
import os as _os                                              # noqa: E402 — deliberately after the local imports above

if _os.environ.get("WHISPER_REMOTE_URL", "").strip():
    from providers.tools_remote import transcribe             # noqa: F811,E402 — intentional rebind, see block comment

__all__ = ["extract", "extract_bytes", "is_audio_url", "maybe_audio_url", "fetch_bytes",
           "transcribe", "AudioResult"]


def extract_bytes(data: bytes) -> AudioResult:
    """Audio bytes → AudioResult (transcript + segments). Runs the whisper large→medium OOM-fallback chain (see
    transcribe); `via` names the winning model, `error` names the SPECIFIC failure reason when the chain fails
    (never a silent empty). Use when you already HAVE the bytes (no network)."""
    if not data:
        return AudioResult(source="bytes", error="empty-bytes")
    transcript, segments, language, duration, reason = transcribe(data)
    via = "whisper" if (transcript and not reason) else (reason if reason.startswith("fallback") else "")
    res = AudioResult(transcript=transcript, segments=segments, language=language,
                      duration=duration, n_bytes=len(data), source="bytes", via=via)
    if not res.ok:
        res.error = reason or "transcribe-failed"               # loud: the specific reason (whisper-missing / OOM / …)
    return res


def extract(url: str, proxy: str | None = None) -> AudioResult:
    """A (maybe-)audio url → AudioResult. Flow: maybe_audio_url gate → fetch_bytes (Chrome fingerprint + residential-
    proxy fallback, media-verified) → transcribe (local whisper, OOM fallback). proxy: residential proxy for fetch's
    fallback leg. FAIL LOUDLY: a fetch failure carries the SPECIFIC reason (http-403 / ssrf-blocked / not-media / …)
    into `error`, never a silent 'fetch-empty'."""
    if not maybe_audio_url(url):
        return AudioResult(source="url", error="not-audio-url")
    data, reason = fetch_bytes(url, proxy=proxy)
    if not data:
        return AudioResult(source="url", error=f"fetch-failed:{reason}")   # loud reason
    res = extract_bytes(data)
    res.source = "url"                                          # the bytes came from the network here
    return res

# ── REMOTE FETCH SPLIT ───────────────────────────────────────────────────────────────────────────────────────────
# FETCH_REMOTE_URL moves the DOWNLOAD itself to ir-render-16: extract(url) becomes one HTTP call, and the file bytes
# never touch this box. Three reasons, memory being the weakest:
#   1. Egress IP coherence — a site currently sees one IP render the page and a DIFFERENT IP download the file it
#      links to, seconds later. That is a bot signature we manufacture ourselves.
#      {GCLOUD 2026-08-04 — ir-media-8 EXTERNAL 35.254.161.69 / ir-render-16 EXTERNAL 136.112.158.156}
#   2. Rate limiting — the download path consults politeness ZERO times while the render path consults it 20 times,
#      so two uncoordinated channels hit the same host. On the render VM both run in ONE process and share one
#      per-host pacing cursor. {GREP 2026-08-04 — render.py 15, capture.py 5, both fetch.py files 0}
#   3. Memory — fetch reads the whole file into RAM before forwarding, capped at 300MB each; 24 slots is 7.2GB worst
#      case, which is what decides how small this box can get. {one choruscall mp3 = 91,723,583 bytes}
# [CONFIDENCE: CONFIRMED — IPs, grep counts and file size all read from live sources].
import os as _os                                              # noqa: E402 — deliberately after the local imports above

if _os.environ.get("FETCH_REMOTE_URL", "").strip():
    from providers.fetch_remote import audio_extract as extract        # noqa: F811,E402 — intentional rebind
