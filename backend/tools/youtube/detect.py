"""detect — is this a YouTube URL? + pull out the 11-char video id (the cheapest first gate).

用一句话讲完: 认 YouTube 的各种链接形态 —— youtube.com/watch?v=ID、youtu.be/ID、/embed/ID、/shorts/ID、/live/ID —
并抠出那个 11 位 video_id。判 True 只是让 download 去试(yt-dlp 真正处理各种边缘形态)。
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
             "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")                        # a YouTube video id is 11 url-safe chars
_PATH_ID_RE = re.compile(r"/(?:embed|shorts|live|v)/([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str:
    """The 11-char YouTube video id from any YouTube url form, or '' if not found. Handles watch?v=, youtu.be/<id>,
    /embed/<id>, /shorts/<id>, /live/<id>."""
    p = urlparse(url or "")
    host = (p.netloc or "").lower()
    if host in ("youtu.be",):                                     # youtu.be/<id>
        cand = p.path.lstrip("/").split("/")[0]
        return cand if _ID_RE.match(cand) else ""
    if host.replace("www.", "").replace("m.", "") in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        v = parse_qs(p.query).get("v", [""])[0]                   # watch?v=<id>
        if _ID_RE.match(v):
            return v
        m = _PATH_ID_RE.search(p.path)                            # /embed|shorts|live|v/<id>
        return m.group(1) if m else ""
    return ""


def is_youtube_url(url: str) -> bool:
    """True iff the URL is a YouTube video link we can pull audio from (has a resolvable 11-char video id)."""
    host = (urlparse(url or "").netloc or "").lower()
    return host in _YT_HOSTS and bool(video_id(url))
