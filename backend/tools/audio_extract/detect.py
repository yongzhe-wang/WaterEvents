"""detect — is this URL an audio/video file we can transcribe? (the cheapest first gate)

用一句话讲完: 给一个 URL → 看是不是音视频扩展名(mp3/m4a/mp4/webm/wav/... 或 .m3u8 HLS 播放列表);不是的话,
看它是不是"无扩展名但可能是音频"的端点(IR 平台的 webcast 常常是 `/media/<id>`、`/download` 这种无扩展名)。
判 maybe=True 只让 fetch 去试;fetch 会用文件 magic/content-type 兜底,猜错=零成本 no-op。
"""
from __future__ import annotations

import os.path
import re
from urllib.parse import urlparse

# Extensions we can transcribe: audio + video containers (whisper/ffmpeg decode the audio track) + HLS playlists.
_AUDIO_RE = re.compile(r"\.(mp3|m4a|wav|aac|ogg|opus|flac|mp4|mov|webm|mkv|avi|flv|m3u8)($|\?|#)", re.I)
# Obvious NON-audio extensions — exclude from the extensionless guess.
_NOT_AUDIO_RE = re.compile(
    r"\.(html?|aspx|jsp|php|pdf|pptx?|docx?|xlsx?|csv|json|xml|txt|zip"
    r"|jpe?g|png|gif|svg|webp|ico)($|\?|#)", re.I)


def is_audio_url(url: str) -> bool:
    """True iff the URL points directly at an audio/video/HLS file (ignoring ?query / #fragment)."""
    return bool(_AUDIO_RE.search(url or ""))


def maybe_audio_url(url: str) -> bool:
    """is_audio_url OR an EXTENSIONLESS path that COULD be audio (IR webcast endpoints: `/media/<id>`, `/download`,
    `/audio/<uuid>`). A wrong guess is cheap — fetch verifies the media container magic/content-type and returns b''
    on a non-audio body, so the caller just falls through."""
    if is_audio_url(url):
        return True
    path = urlparse((url or "").split("#")[0].split("?")[0]).path
    ext = os.path.splitext(os.path.basename(path))[1]              # '' for /media/<uuid>
    return ext == "" and bool(path) and not _NOT_AUDIO_RE.search(url or "")
