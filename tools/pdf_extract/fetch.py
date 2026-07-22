"""fetch — download a PDF's raw bytes, bot-wall-resistant, or b'' on any failure.

用一句话讲完: 用 curl_cffi 发一个"跟真 Chrome 完全一致的 TLS/JA3/HTTP2 指纹"的 GET → 拿到 bytes → 用 `%PDF-` magic
确认真是 PDF → 返回。Chrome 指纹是关键:很多 CDN 会掐普通 curl 的 HTTP/2 流(rc=92 INTERNAL_ERROR),但对真 Chrome
指纹放行同一个文件。可选走住宅代理(datacenter IP 被封的外国 IR 站靠它救回)。任何失败一律 b'' —— 绝不让抓取抛异常。

WHY b'' on everything: this is best-effort infra. A missing curl_cffi dep, a TLS error, a timeout, a non-PDF body —
all degrade to an empty result the caller treats as "no pdf here", never a crash.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

# Browser UA for normal hosts; SEC's EDGAR requires a declared "name email" UA instead.
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_SEC_UA = os.environ.get("SEC_USER_AGENT", "WaterEvents Research admin@focusalpha.io")
_TIMEOUT_S = int(os.environ.get("PDF_FETCH_TIMEOUT_S", "25"))       # a real IR PDF returns in <5s; past 25s the host is dead
_MAX_BYTES = int(os.environ.get("PDF_FETCH_MAX_BYTES", "18000000"))  # >18MB is almost always an image-only scan or a broken xref


def _host_is_public(host: str) -> bool:
    """SSRF guard: True only when `host` resolves to a PUBLIC IP. Blocks localhost / *.local / and any private,
    loopback, link-local (169.254.169.254 cloud-metadata!), or reserved address — a url is untrusted input and
    must never make us fetch an internal address. Resolution failure → False (refuse, don't guess)."""
    h = (host or "").lower().strip()
    if not h or h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        return False
    try:
        # Check EVERY A/AAAA record — a hostname can resolve to a mix of public + private IPs.
        infos = socket.getaddrinfo(h, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return bool(infos)
    except Exception:                                          # noqa: BLE001 — unresolvable / malformed → refuse
        return False


def fetch_bytes(url: str, proxy: str | None = None) -> bytes:
    """curl_cffi-GET `url` with a Chrome JA3/HTTP2 fingerprint → its PDF bytes, or b'' on ANY failure.

    proxy — optional residential proxy url ('http://user:pass@host:port'); pass one when a host datacenter-blocks
    the direct GET (foreign IR sites). None = direct. The caller owns proxy selection (this tool stays provider-agnostic).
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not _host_is_public(parsed.hostname or ""):
        return b""                                             # non-http scheme or a private/unresolvable host → refuse
    try:
        # Import INSIDE the function so a machine WITHOUT curl_cffi degrades to b'' here instead of an ImportError
        # at module load — keeps the tool importable everywhere.
        from curl_cffi import requests as cffi_requests
    except Exception:                                          # noqa: BLE001 — curl_cffi absent → no-op
        return b""
    try:
        ua = _SEC_UA if "sec.gov" in url.lower() else _BROWSER_UA   # SEC wants a declared UA; everyone else a browser UA
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = cffi_requests.get(
            url,
            impersonate="chrome",                              # the Chrome JA3/HTTP2 fingerprint — the whole point
            timeout=_TIMEOUT_S,                                # fail fast so a hanging host can't clog a parallel pool slot
            allow_redirects=False,                             # SSRF: don't follow a 302 → internal address (we vetted THIS host)
            headers={"User-Agent": ua, "Accept": "application/pdf,*/*"},
            **({"proxies": proxies} if proxies else {}),
        )
        data = resp.content                                    # curl_cffi buffers the whole body to memory
        if len(data) > _MAX_BYTES:                             # oversized → skip (image scan / broken xref; pypdf yields nothing anyway)
            return b""
        # %PDF magic gate: maybe_pdf_url admits extensionless urls, so a non-PDF/HTML body can land here. Accept ONLY
        # real PDFs; anything else → b'' so we never feed HTML to pypdf.
        return data if data[:5] == b"%PDF-" else b""
    except Exception:                                          # noqa: BLE001 — TLS / timeout / non-200 / network → no-op
        return b""
