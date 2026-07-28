"""youtube — one tool, one job: a YouTube url → its audio bytes. DOWNLOAD ONLY (no transcription — that's
audio_extract's job; keep this tool single-responsibility).

用一句话讲完: 和其他 tool 同风格 —— detect(是不是 YouTube)→ download(yt-dlp 抓 bestaudio bytes)→ 统一 YtResult。
就到音频为止;要文本就把 YtResult.audio 交给 audio_extract,本工具不碰转写。

组织:
  detect.py    — is_youtube_url / video_id           (认各种 YouTube 链接形态 + 抠 11 位 id)
  download.py  — download_audio(url) -> (bytes, meta) (yt-dlp bestaudio,cookies/proxy 可选)
  types.py     — YtResult                            (audio bytes + title/duration/video_id)

依赖(全 lazy):yt-dlp。缺则优雅 ok=False,不 break。
"""
from __future__ import annotations

from .detect import is_youtube_url, video_id
from .download import download_audio as _download_audio
from .types import YtResult

__all__ = ["download_audio", "is_youtube_url", "video_id", "YtResult"]


def download_audio(url: str, proxy: str | None = None) -> YtResult:
    """A YouTube url → YtResult (audio bytes + metadata). proxy: optional proxy url for IP-blocked hosts. Best-effort:
    a non-YouTube / unavailable / yt-dlp-missing url → YtResult(ok=False) with an `error`. This is the ONLY entry —
    the tool downloads audio and nothing more; transcription is a separate step (audio_extract)."""
    if not is_youtube_url(url):
        return YtResult(error="not-youtube-url")
    audio, meta = _download_audio(url, proxy=proxy)
    if not audio:                                                 # LOUD: the specific reason (needs-cookies / geo-blocked / …)
        return YtResult(video_id=video_id(url), error=meta.get("reason", "download-failed"))
    return YtResult(audio=audio, title=meta.get("title", ""), duration=meta.get("duration", 0.0),
                    video_id=meta.get("id", ""), ext=meta.get("ext", ""), n_bytes=len(audio))
