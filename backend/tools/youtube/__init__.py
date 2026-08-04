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

__all__ = ["download_audio", "download_stream", "is_youtube_url", "video_id", "YtResult"]


def _run(url: str, proxy: str | None, vid: str = "") -> YtResult:
    """Shared yt-dlp invocation → YtResult. The two public entries differ ONLY in their gate, not their download."""
    audio, meta = _download_audio(url, proxy=proxy)
    if not audio:                                                 # LOUD: the specific reason (needs-cookies / geo-blocked / …)
        return YtResult(video_id=vid, error=meta.get("reason", "download-failed"))
    return YtResult(audio=audio, title=meta.get("title", ""), duration=meta.get("duration", 0.0),
                    video_id=meta.get("id", "") or vid, ext=meta.get("ext", ""), n_bytes=len(audio))


def download_audio(url: str, proxy: str | None = None) -> YtResult:
    """A YouTube url → YtResult (audio bytes + metadata). proxy: optional proxy url for IP-blocked hosts. Best-effort:
    a non-YouTube / unavailable / yt-dlp-missing url → YtResult(ok=False) with an `error`."""
    if not is_youtube_url(url):
        return YtResult(error="not-youtube-url")
    return _run(url, proxy, vid=video_id(url))


def download_stream(url: str, proxy: str | None = None) -> YtResult:
    """A DIRECT media STREAM url (.m3u8 HLS / .mpd DASH / .mp4 / …) → YtResult. Same yt-dlp, NO YouTube gate.

    用一句话讲完: yt-dlp 对企业 webcast 的**页面**一律 `Unsupported URL`,但它对页面背后那条 **.m3u8/.mpd 流地址**是
    原生支持的(HLS/DASH 下载本来就是它的核心能力)。所以分工是:watercrawl.capture 从浏览器网络日志里抓出真实流
    地址 → 这个入口把它下下来。两者互补,不是二选一。

    {PROBE 2026-08-03 `yt_dlp --simulate` on event.choruscall / event.webcasts / events.q4inc / webcast-eqs /
    irwebcasting PAGES → all "Unsupported URL"} [CONFIDENCE: CONFIRMED — measured; the page is unsupported, the
    stream it loads is not]. Best-effort: never raises, ok=False carries the reason."""
    return _run(url, proxy)
