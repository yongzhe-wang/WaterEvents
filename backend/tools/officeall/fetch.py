"""fetch — download an office document's bytes + confirm its format. FAIL LOUDLY: every failure returns a SPECIFIC
reason string (never a silent b'') and logs it, and a blocked direct GET FALLS BACK to a residential proxy.

用一句话讲完: 不再"任何失败静默 return b''"。改成:每个失败路径给一个**具体原因**(ssrf-blocked / dep-missing /
http-403 / timeout / oversized-30MB / wrong-magic),**大声 log**,并返回 (b'', reason)。fetch 有 fallback 链:
先 direct curl_cffi,被墙/超时就自动重试住宅代理 —— 两条都失败才返回,reason 记录整条链的失败。质量问题不再被藏。

返回: (bytes, format) 成功;(b'', reason) 失败 —— reason 非空即"大声"的失败信号,调用方必须看。
"""
from __future__ import annotations

import ipaddress
import io
import os
import socket
import sys
import zipfile
from urllib.parse import parse_qs, unquote, urlparse

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT_S = int(os.environ.get("OFFICE_FETCH_TIMEOUT_S", "40"))
_MAX_BYTES = int(os.environ.get("OFFICE_FETCH_MAX_BYTES", "60000000"))   # 60MB


def _loud(msg: str) -> None:
    """Emit a failure/fallback line to stderr — the 'fail loudly' channel. Every non-success path logs here so a
    quality problem is VISIBLE, never swallowed."""
    print(f"[officeall.fetch] {msg}", file=sys.stderr, flush=True)


def _host_is_public(host: str) -> bool:
    """True only when `host` resolves to a PUBLIC IP (SSRF guard). Blocks localhost / private / loopback / link-local
    (169.254.169.254 metadata) / reserved. Resolution failure → False."""
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


def _unwrap_office_viewer(url: str) -> str:
    """view.officeapps.live.com/op/view.aspx?src=<encoded real url> → the decoded real doc url (pure parsing)."""
    if "officeapps.live.com" not in (url or "").lower():
        return url
    src = parse_qs(urlparse(url).query).get("src", [""])[0]
    return unquote(src) if src else url


def _sniff_format(data: bytes, url_guess: str) -> str:
    """Confirm the real format from the bytes' magic: %PDF→pdf; PK zip→pptx/xlsx/docx (by inner folder); HTML→html;
    else '' (not a document)."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                if any(n.startswith("ppt/") for n in names):
                    return "pptx"
                if any(n.startswith("xl/") for n in names):
                    return "xlsx"
                if any(n.startswith("word/") for n in names):
                    return "docx"
        except Exception:                                        # noqa: BLE001
            pass
        return url_guess if url_guess in ("pptx", "xlsx", "docx") else ""
    head = data[:512].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<head" in head:
        return "html"
    return ""


def _try_get(url: str, proxy: str | None) -> tuple[bytes, str]:
    """ONE curl_cffi GET attempt → (bytes, "") on a usable document, or (b'', reason) with a SPECIFIC loud reason.
    Reasons: dep-missing / http-<code> / oversized-<MB> / wrong-magic-<kind> / <ExcType>. `proxy` None = direct."""
    try:
        from curl_cffi import requests as cffi_requests
    except Exception:                                            # noqa: BLE001
        return b"", "dep-missing:curl_cffi"
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = cffi_requests.get(
            url, impersonate="chrome", timeout=_TIMEOUT_S, allow_redirects=False,
            headers={"User-Agent": _BROWSER_UA,
                     "Accept": "application/pdf,application/vnd.openxmlformats-officedocument.*,*/*"},
            **({"proxies": proxies} if proxies else {}),
        )
        if resp.status_code != 200:
            return b"", f"http-{resp.status_code}"               # loud: the exact status (403 wall, 404 gone, 302 redirect)
        data = resp.content
        if not data:
            return b"", "empty-body"
        if len(data) > _MAX_BYTES:
            return b"", f"oversized-{len(data) // 1_000_000}MB"  # loud: the size, not a silent drop
        fmt = _sniff_format(data, "")
        if not fmt:
            return b"", f"wrong-magic-{data[:8]!r}"              # loud: got bytes but not a document (an HTML wall etc.)
        return data, ""
    except Exception as e:                                       # noqa: BLE001 — loud: the exception type + message
        return b"", f"{type(e).__name__}:{str(e)[:80]}"


def fetch_bytes(url: str, proxy: str | None = None, fmt_guess: str = "pdf") -> tuple[bytes, str]:
    """A doc url → (bytes, real_format) on success, or (b'', reason) on failure (LOUD, specific). FALLBACK CHAIN:
    direct curl_cffi → residential proxy (only when direct fails and a proxy is available). Every failed attempt is
    logged; the returned reason names the FIRST thing that went wrong / the accumulated chain. proxy: the residential
    proxy url to use for the fallback leg (None → no proxy fallback, direct-only)."""
    url = _unwrap_office_viewer(url)
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        _loud(f"REFUSED non-http scheme: {url[:80]}")
        return b"", "bad-scheme"
    if not _host_is_public(parsed.hostname or ""):
        _loud(f"REFUSED non-public/unresolvable host: {parsed.hostname}")
        return b"", "ssrf-blocked"

    # Attempt 1: DIRECT (no proxy). The common path.
    data, reason = _try_get(url, None)
    if data:
        return data, _sniff_format(data, fmt_guess)
    _loud(f"direct FAILED ({reason}) for {url[:70]}")

    # Attempt 2: RESIDENTIAL PROXY fallback — only when a proxy is available and direct failed on something a
    # different egress could fix (a datacenter block / TLS reset), not a definitive 404/oversized.
    if proxy and not reason.startswith(("http-404", "oversized")):
        data2, reason2 = _try_get(url, proxy)
        if data2:
            _loud(f"RECOVERED via residential proxy for {url[:70]}")
            return data2, _sniff_format(data2, fmt_guess)
        _loud(f"proxy fallback ALSO FAILED ({reason2}) for {url[:70]}")
        return b"", f"direct[{reason}]+proxy[{reason2}]"          # loud: the WHOLE chain's failure

    return b"", reason                                            # loud: the specific direct-leg reason
