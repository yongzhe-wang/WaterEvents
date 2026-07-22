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

__all__ = ["extract", "extract_bytes", "is_audio_url", "maybe_audio_url", "fetch_bytes",
           "transcribe", "AudioResult"]


def extract_bytes(data: bytes) -> AudioResult:
    """Audio bytes → AudioResult (transcript + segments). Use when you already HAVE the bytes (no network)."""
    if not data:
        return AudioResult(source="bytes", error="empty-bytes")
    transcript, segments, language, duration = transcribe(data)
    res = AudioResult(transcript=transcript, segments=segments, language=language,
                      duration=duration, n_bytes=len(data), source="bytes")
    if not res.ok:
        res.error = "transcribe-failed"
    return res


def extract(url: str, proxy: str | None = None) -> AudioResult:
    """A (maybe-)audio url → AudioResult. Flow: maybe_audio_url gate → fetch_bytes (Chrome fingerprint, media-verified)
    → transcribe (local whisper on GPU). proxy: pass a residential proxy url when a host datacenter-blocks the GET.
    Best-effort: a non-audio / unreachable / untranscribable url → AudioResult(ok=False) with an `error`."""
    if not maybe_audio_url(url):
        return AudioResult(source="url", error="not-audio-url")
    data = fetch_bytes(url, proxy=proxy)
    if not data:
        return AudioResult(source="url", error="fetch-empty")
    res = extract_bytes(data)
    res.source = "url"                                          # the bytes came from the network here
    return res
