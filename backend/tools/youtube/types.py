"""YtResult — the single return shape for the youtube tool.

用一句话讲完: 给一个 YouTube URL → download 出音频 bytes + 元数据(标题/时长/video_id/容器格式)→ 汇成这一个
YtResult。audio 是最优音频流的原始 bytes,可直接喂给 audio_extract.extract_bytes 转写(youtube → 转录 一条龙)。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class YtResult:
    """The verdict for ONE YouTube url. `ok` = we downloaded real audio bytes.

    audio     — the raw bestaudio stream bytes (m4a/webm; whisper/ffmpeg decode it directly), b'' on failure.
    title     — the video title ('' on failure).
    duration  — length in seconds (0 on failure), for cost accounting.
    video_id  — the 11-char YouTube id ('' on failure).
    ext       — the audio container extension ('m4a'|'webm'|…), '' on failure.
    n_bytes   — size of `audio` (0 on failure).
    error     — a short reason when ok=False ('not-youtube-url', 'yt-dlp-missing', 'download-failed'); '' on success.
    """
    audio: bytes = b""
    title: str = ""
    duration: float = 0.0
    video_id: str = ""
    ext: str = ""
    n_bytes: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when we got real audio bytes."""
        return bool(self.audio)
