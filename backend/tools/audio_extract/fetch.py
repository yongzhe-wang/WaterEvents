"""fetch — download an audio/video file's bytes + confirm it's media. FAIL LOUDLY: every failure returns a SPECIFIC
reason (never a silent b'') and logs it; a blocked direct GET FALLS BACK to a residential proxy.

用一句话讲完: 不再"任何失败静默 return b''"。curl_cffi Chrome 指纹 GET → 用 content-type/magic 确认真是音视频 →
返回 (bytes, "")。每个失败给具体原因(ssrf-blocked / dep-missing / http-403 / not-media / oversized-200MB /
hls-playlist)+ 大声 log,并有 direct→住宅代理 fallback。质量问题不再被藏。

返回: (bytes, "") 成功;(b"", reason) 失败 —— reason 非空即"大声"的失败信号。
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
from urllib.parse import urlparse

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT_S = int(os.environ.get("AUDIO_FETCH_TIMEOUT_S", "120"))       # audio is big; a webcast download takes longer
_MAX_BYTES = int(os.environ.get("AUDIO_FETCH_MAX_BYTES", "300000000"))  # 300MB
_MAGIC = (b"ID3", b"OggS", b"RIFF", b"fLaC", b"\x1aE\xdf\xa3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


def _loud(msg: str) -> None:
    """The 'fail loudly' channel — every non-success path logs here so a quality problem is VISIBLE, never swallowed."""
    print(f"[audio_extract.fetch] {msg}", file=sys.stderr, flush=True)


def _host_is_public(host: str) -> bool:
    """True only when `host` resolves to a PUBLIC IP (SSRF guard). Same policy across all tools' fetch."""
    h = (host or "").lower().strip()
    if not h or h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        return False
    try:
        for info in socket.getaddrinfo(h, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:                                            # noqa: BLE001
        return False


def _looks_like_media(data: bytes, content_type: str) -> bool:
    """True when the body is really an audio/video container — content-type audio/*|video/*, a magic signature, or an
    ISO-BMFF 'ftyp' box (mp4/m4a/mov)."""
    ct = (content_type or "").lower()
    if ct.startswith("audio/") or ct.startswith("video/"):
        return True
    if data[:3] == b"ID3" or any(data.startswith(m) for m in _MAGIC):
        return True
    return data[4:8] == b"ftyp"


def _try_get(url: str, proxy: str | None) -> tuple[bytes, str]:
    """ONE curl_cffi GET → (bytes, "") on real media, or (b'', reason) with a SPECIFIC loud reason
    (dep-missing / http-<code> / empty-body / oversized-<MB> / not-media / <ExcType>)."""
    try:
        from curl_cffi import requests as cffi_requests
    except Exception:                                            # noqa: BLE001
        return b"", "dep-missing:curl_cffi"
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = cffi_requests.get(
            url, impersonate="chrome", timeout=_TIMEOUT_S, allow_redirects=False,
            headers={"User-Agent": _BROWSER_UA, "Accept": "audio/*,video/*,*/*"},
            **({"proxies": proxies} if proxies else {}),
        )
        if resp.status_code != 200:
            return b"", f"http-{resp.status_code}"
        data = resp.content
        if not data:
            return b"", "empty-body"
        if len(data) > _MAX_BYTES:
            return b"", f"oversized-{len(data) // 1_000_000}MB"
        ct = resp.headers.get("content-type", "") if hasattr(resp, "headers") else ""
        if not _looks_like_media(data, ct):
            return b"", f"not-media-ct:{ct[:30]}"                 # loud: got a body but it isn't audio (an HTML wall)
        return data, ""
    except Exception as e:                                       # noqa: BLE001
        return b"", f"{type(e).__name__}:{str(e)[:80]}"


def fetch_bytes(url: str, proxy: str | None = None) -> tuple[bytes, str]:
    """An audio/video url → (bytes, "") on success, or (b'', reason) on failure (LOUD, specific). FALLBACK: direct
    curl_cffi → residential proxy (when direct fails on a recoverable reason and a proxy is available). proxy: the
    residential proxy url for the fallback leg (None → direct only)."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        _loud(f"REFUSED non-http scheme: {url[:80]}")
        return b"", "bad-scheme"
    if not _host_is_public(parsed.hostname or ""):
        _loud(f"REFUSED non-public/unresolvable host: {parsed.hostname}")
        return b"", "ssrf-blocked"
    if url.split("?")[0].lower().endswith(".m3u8"):              # HLS playlist ≠ a file → the ffmpeg layer's job
        _loud(f"HLS playlist not a direct file (needs ffmpeg layer): {url[:70]}")
        return b"", "hls-playlist"

    data, reason = _try_get(url, None)                          # DIRECT
    if data:
        return data, ""
    _loud(f"direct FAILED ({reason}) for {url[:70]}")
    if proxy and not reason.startswith(("http-404", "oversized")):
        data2, reason2 = _try_get(url, proxy)                  # RESIDENTIAL PROXY fallback
        if data2:
            _loud(f"RECOVERED via residential proxy for {url[:70]}")
            return data2, ""
        _loud(f"proxy fallback ALSO FAILED ({reason2}) for {url[:70]}")
        return b"", f"direct[{reason}]+proxy[{reason2}]"
    return b"", reason
