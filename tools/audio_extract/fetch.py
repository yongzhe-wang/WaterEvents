"""fetch — download an audio/video file's raw bytes, bot-wall-resistant, or b'' on any failure.

用一句话讲完: 和 pdf_extract 的 fetch 同一套 —— curl_cffi Chrome 指纹 GET → 拿 bytes → 用 content-type(audio/*、
video/*)+ 容器 magic(ID3/ftyp/RIFF/OggS/...)确认真是音视频 → 返回。可选走住宅代理。任何失败一律 b''。
音频文件大(一场业绩电话 30-60 分钟 ~50-150MB),所以 size cap 比 PDF 大得多。

WHY content-type + magic (not just extension): maybe_audio_url admits extensionless webcast endpoints, so we must
verify the body really is a media container before handing it to the transcriber — never feed an HTML error page to
whisper. HLS `.m3u8` is a PLAYLIST not a file → returned as-is only when the caller's ffmpeg layer handles it (a
follow-up); the direct-file path here rejects it.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT_S = int(os.environ.get("AUDIO_FETCH_TIMEOUT_S", "120"))       # audio is big; a real webcast download takes longer than a PDF
_MAX_BYTES = int(os.environ.get("AUDIO_FETCH_MAX_BYTES", "300000000"))  # 300MB — a long HD earnings-call video

# Container magic signatures at/near the start of the file — the reliable "this is really media" gate.
_MAGIC = (b"ID3", b"OggS", b"RIFF", b"fLaC", b"\x1aE\xdf\xa3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


def _host_is_public(host: str) -> bool:
    """SSRF guard: True only when `host` resolves to a PUBLIC IP. Blocks localhost / private / loopback / link-local
    (169.254.169.254 metadata) / reserved. Resolution failure → False (refuse). Identical policy to pdf_extract.fetch."""
    h = (host or "").lower().strip()
    if not h or h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        return False
    try:
        infos = socket.getaddrinfo(h, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return bool(infos)
    except Exception:                                            # noqa: BLE001
        return False


def _looks_like_media(data: bytes, content_type: str) -> bool:
    """True when the body is really an audio/video container — by content-type header OR a known magic signature.
    mp4/m4a/mov carry 'ftyp' at byte offset 4, so check that separately."""
    ct = (content_type or "").lower()
    if ct.startswith("audio/") or ct.startswith("video/"):
        return True
    if data[:3] in (b"ID3",) or any(data.startswith(m) for m in _MAGIC):
        return True
    return data[4:8] == b"ftyp"                                  # ISO-BMFF (mp4/m4a/mov): 'ftyp' box at offset 4


def fetch_bytes(url: str, proxy: str | None = None) -> bytes:
    """curl_cffi-GET `url` with a Chrome fingerprint → audio/video bytes, or b'' on ANY failure. proxy: optional
    residential proxy url for datacenter-blocked hosts; None = direct. The caller owns proxy selection."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not _host_is_public(parsed.hostname or ""):
        return b""
    if url.split("?")[0].lower().endswith(".m3u8"):              # HLS playlist ≠ a file → the ffmpeg layer's job, not here
        return b""
    try:
        from curl_cffi import requests as cffi_requests          # lazy: missing dep → b'' no-op, not an ImportError
    except Exception:                                            # noqa: BLE001
        return b""
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            timeout=_TIMEOUT_S,
            allow_redirects=False,                              # SSRF: don't chase a redirect to an internal address
            headers={"User-Agent": _BROWSER_UA, "Accept": "audio/*,video/*,*/*"},
            **({"proxies": proxies} if proxies else {}),
        )
        data = resp.content
        if not data or len(data) > _MAX_BYTES:                  # empty or oversized → skip
            return b""
        ct = resp.headers.get("content-type", "") if hasattr(resp, "headers") else ""
        return data if _looks_like_media(data, ct) else b""     # verify it's really media before returning
    except Exception:                                           # noqa: BLE001 — TLS / timeout / network → no-op
        return b""
