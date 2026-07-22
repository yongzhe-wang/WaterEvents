"""fetch — download a .pptx deck's raw bytes, bot-wall-resistant, or b'' on any failure.

用一句话讲完: 和 pdf/audio 的 fetch 同一套 —— 先把 Office 查看器链接(view.officeapps.live.com/op/view.aspx?src=…)
拆出里面真正的 .pptx URL,再用 curl_cffi Chrome 指纹 GET → 拿 bytes → 用 ZIP magic(PK\\x03\\x04,pptx 是个 zip)
确认 → 返回。可选走住宅代理。任何失败一律 b''。

WHY unwrap the Office viewer: IR decks are often linked through the Office Online viewer whose PAGE is just a
"We're fetching your file…" splash — the real .pptx sits in the `?src=` query param. Decoding it (pure URL parsing,
no network) gives the downloadable url.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import parse_qs, unquote, urlparse

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT_S = int(os.environ.get("PPTX_FETCH_TIMEOUT_S", "40"))         # a deck is bigger than a page but smaller than audio
_MAX_BYTES = int(os.environ.get("PPTX_FETCH_MAX_BYTES", "60000000"))   # 60MB — an image-heavy deck


def _host_is_public(host: str) -> bool:
    """SSRF guard: True only when `host` resolves to a PUBLIC IP (identical policy to pdf/audio fetch)."""
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


def _unwrap_office_viewer(url: str) -> str:
    """view.officeapps.live.com/op/view.aspx?src=<encoded real url> → the decoded real .pptx url. Pure URL parsing,
    no network. Returns the input unchanged when it isn't an Office-viewer link."""
    if "officeapps.live.com" not in (url or "").lower():
        return url
    src = parse_qs(urlparse(url).query).get("src", [""])[0]
    return unquote(src) if src else url


def fetch_bytes(url: str, proxy: str | None = None) -> bytes:
    """curl_cffi-GET the deck with a Chrome fingerprint → .pptx bytes, or b'' on ANY failure. Unwraps an Office-viewer
    `?src=` link first. proxy: optional residential proxy url; None = direct. The caller owns proxy selection."""
    url = _unwrap_office_viewer(url)                             # resolve the viewer wrapper to the real downloadable url
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not _host_is_public(parsed.hostname or ""):
        return b""
    try:
        from curl_cffi import requests as cffi_requests          # lazy: missing dep → b'' no-op
    except Exception:                                            # noqa: BLE001
        return b""
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            timeout=_TIMEOUT_S,
            allow_redirects=False,                              # SSRF: we vetted THIS host; don't chase a redirect
            headers={"User-Agent": _BROWSER_UA,
                     "Accept": "application/vnd.openxmlformats-officedocument.presentationml.presentation,*/*"},
            **({"proxies": proxies} if proxies else {}),
        )
        data = resp.content
        if not data or len(data) > _MAX_BYTES:
            return b""
        # ZIP magic gate: a .pptx is an OOXML ZIP → 'PK\x03\x04'. maybe_pptx_url admits extensionless urls, so verify
        # the body is really a zip before returning; python-pptx (in slides.py) then confirms it's a PRESENTATION zip.
        return data if data[:4] == b"PK\x03\x04" else b""
    except Exception:                                           # noqa: BLE001 — TLS / timeout / network → no-op
        return b""
