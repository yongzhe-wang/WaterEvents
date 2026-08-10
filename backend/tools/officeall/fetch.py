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
from urllib.parse import parse_qs, unquote, urljoin, urlparse

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT_S = int(os.environ.get("OFFICE_FETCH_TIMEOUT_S", "40"))
_MAX_BYTES = int(os.environ.get("OFFICE_FETCH_MAX_BYTES", "60000000"))   # 60MB
# Bounded so a redirect loop costs 6 requests, not a hang. Browsers use 20; documents do not need it.
_MAX_REDIRECTS = int(os.environ.get("OFFICE_FETCH_MAX_REDIRECTS", "5"))


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
    # OLE2 — the 1997 compound-document container behind legacy .xls / .doc / .ppt. It was missing from this table, so
    # every legacy workbook was rejected HERE, at the fetch layer, as `wrong-magic` and never reached a parser.
    # {LIVE 2026-08-09 vodafone h1-12-spreadsheet.xls → "direct FAILED (WRONG-MAGIC-B'\XD0\XCF\X11\XE0\XA1\XB1\X1A\XE1')"}
    # The fallback for exactly these files already exists and was unreachable because of it:
    # {EXTRACT.PY _xls_tables "IF NOT DATA OR DATA[:4] != B'\XD0\XCF\X11\XE0': RETURN NONE  # OLE2 MAGIC — NOT A LEGACY XLS"}
    # — a pandas/xlrd reader written precisely because Docling's MsExcelDocumentBackend only speaks OOXML, and the ledger
    # had already priced the gap {DB 2026-08-05 "KIND='XLSX': 23 FAILED, 1 DONE"}.
    # Like the zip branch above, the container does not name its payload (.xls, .doc and .ppt share it), so the url's own
    # extension decides and an unrecognised one still returns '' rather than a guess.
    # [CONFIDENCE: CONFIRMED 100% — the magic quoted in the rejection is byte-identical to the one _xls_tables tests for.]
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return url_guess if url_guess in ("pptx", "xlsx", "docx") else ""
    head = data[:512].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<head" in head:
        return "html"
    return ""


def _try_get(url: str, proxy: str | None, fmt_guess: str = "") -> tuple[bytes, str]:
    """ONE curl_cffi GET attempt → (bytes, "") on a usable document, or (b'', reason) with a SPECIFIC loud reason.
    Reasons: dep-missing / http-<code> / oversized-<MB> / wrong-magic-<kind> / <ExcType>. `proxy` None = direct.

    Redirects are FOLLOWED one hop at a time, re-checking the SSRF guard at every hop (see the loop below)."""
    try:
        from curl_cffi import requests as cffi_requests
    except Exception:                                            # noqa: BLE001
        return b"", "dep-missing:curl_cffi"
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        # FOLLOW REDIRECTS. `allow_redirects=False` made every 3xx a terminal failure, and a 3xx is the NORMAL way an IR
        # site serves a document — a CDN hand-off, an http→https upgrade, a /files/x.pdf → signed-url rewrite. Because a
        # redirect is a property of the URL and not of the egress, the residential-proxy fallback replayed the request
        # and collected the identical 3xx, so each one burned BOTH legs to learn nothing.
        # {DB 2026-08-09 waterevents.event_media_urls status='failed': "DIRECT[HTTP-302]+PROXY[HTTP-302] 51 |
        #  DIRECT[HTTP-302]+PROXY[CONNECTIO… 47 | DIRECT[HTTP-301]+PROXY[HTTP-301] 22 | DIRECT[HTTP-303]+… 12 |
        #  DIRECT[HTTP-307]+PROXY[HTTP-307] 9 | DIRECT[HTTP-301]+PROXY[TIMEOUT:F… 5" — 169 pdfs, every one a bare hop}
        # [CONFIDENCE: CONFIRMED 100% — the reason strings are this function's own output, read back from the ledger.]
        # NOT `allow_redirects=True`: fetch_bytes validates only the URL it was HANDED, so letting the client chase hops
        # on its own would let a public host redirect us onto 169.254.169.254 or a private address — the exact hole
        # `_host_is_public` exists to close. Every hop is therefore re-validated here before it is taken.
        cur, resp = url, None
        for _ in range(_MAX_REDIRECTS + 1):
            resp = cffi_requests.get(
                cur, impersonate="chrome", timeout=_TIMEOUT_S, allow_redirects=False,
                headers={"User-Agent": _BROWSER_UA,
                         "Accept": "application/pdf,application/vnd.openxmlformats-officedocument.*,*/*"},
                **({"proxies": proxies} if proxies else {}),
            )
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            loc = (resp.headers or {}).get("Location") or ""
            if not loc:
                return b"", f"http-{resp.status_code}-no-location"   # a 3xx with nowhere to go is a broken server
            nxt = urljoin(cur, loc)                                  # a relative Location is legal and common
            p = urlparse(nxt)
            if p.scheme not in ("http", "https"):
                return b"", f"redirect-bad-scheme-{p.scheme or 'none'}"
            if not _host_is_public(p.hostname or ""):
                _loud(f"REFUSED redirect to non-public host {p.hostname} from {cur[:60]}")
                return b"", "redirect-ssrf-blocked"                  # the guard that made allow_redirects unsafe
            cur = nxt
        else:
            return b"", f"too-many-redirects-{_MAX_REDIRECTS}"       # for/else: never broke = still redirecting
        if resp.status_code != 200:
            return b"", f"http-{resp.status_code}"               # loud: the exact status (403 wall, 404 gone)
        data = resp.content
        if not data:
            return b"", "empty-body"
        if len(data) > _MAX_BYTES:
            return b"", f"oversized-{len(data) // 1_000_000}MB"  # loud: the size, not a silent drop
        # PASS THE URL'S OWN GUESS INTO THE SNIFFER. This argument was hard-coded to "" and that single empty string
        # made the OLE2 support in _sniff_format dead code from the day it shipped: the 1997 compound-document container
        # is shared by .xls, .doc and .ppt, so its magic identifies the CONTAINER and only the url's extension can name
        # the payload — which is exactly why that branch ends `return url_guess if url_guess in (...) else ""`. Handed
        # "", it returned "", the caller read that as "not a document", and every legacy workbook was rejected here with
        # the very magic the branch tests for. The same emptiness disarms the PK-zip branch's own extension fallback.
        # {LIVE 2026-08-10 five ledger urls re-fetched with the shipped code — vodafone financial-results .xls,
        #  mb.cision.com/…/b314189899f16a52.xls, orkla quarterly-figures, group.ntt fy2018q1hosoku0807.xls — all five
        #  returned "WRONG-MAGIC-B'\XD0\XCF\X11\XE0\XA1\XB1\X1A\XE1'", i.e. OLE2 recognised and then discarded}
        # {DB 2026-08-10 event_media_urls "FAILED:FETCH-FAILED:DIRECT[WRONG-MAGIC-B'\XD0\XCF\… 54" — unchanged while
        #  the redirect fix in the same deploy took its own bucket from 278 down to 230}
        # [CONFIDENCE: CONFIRMED 100% — the two buckets moved differently under one deploy, and the five re-fetches
        #  reproduce the rejection against the running code.]
        fmt = _sniff_format(data, fmt_guess)
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
    data, reason = _try_get(url, None, fmt_guess)
    if data:
        fmt = _sniff_format(data, fmt_guess)
        # HTML WHERE A DOCUMENT WAS ASKED FOR IS A WALL, NOT A RESULT. Every caller reaches this through
        # maybe_office_url, so fmt_guess is always pdf/xlsx/docx/pptx and html can only mean the site served an
        # interstitial — a consent page, a login, a "your download will begin shortly" stub. Following redirects made
        # this reachable: the URL used to die at the 3xx with a loud `http-302`, and now it arrives at whatever the
        # last hop serves. Landing a 572-byte stub in event_documents as a real document would trade a visible failure
        # for an invisible one, which is the opposite of what this module is for.
        # {LIVE 2026-08-09 goldmansachs.com/pressroom/.../2026-q2-results.pdf → 572 bytes, sniffed "html"}
        # [CONFIDENCE: CONFIRMED 100% — observed in the redirect-recovery test, 7/8 real pdfs and this one stub.]
        # Falls through to the proxy leg rather than returning: a wall is exactly the failure a different egress fixes.
        if fmt == "html" and fmt_guess != "html":
            reason = f"wall-html-{len(data)}B"
            data = b""
        else:
            return data, fmt
    _loud(f"direct FAILED ({reason}) for {url[:70]}")

    # Attempt 2: RESIDENTIAL PROXY fallback — only when a proxy is available and direct failed on something a
    # different egress could fix (a datacenter block / TLS reset), not a definitive 404/oversized.
    if proxy and not reason.startswith(("http-404", "oversized")):
        data2, reason2 = _try_get(url, proxy, fmt_guess)
        if data2:
            fmt2 = _sniff_format(data2, fmt_guess)
            if fmt2 == "html" and fmt_guess != "html":            # same wall through a different egress — still a wall
                reason2 = f"wall-html-{len(data2)}B"
            else:
                _loud(f"RECOVERED via residential proxy for {url[:70]}")
                return data2, fmt2
        _loud(f"proxy fallback ALSO FAILED ({reason2}) for {url[:70]}")
        return b"", f"direct[{reason}]+proxy[{reason2}]"          # loud: the WHOLE chain's failure

    return b"", reason                                            # loud: the specific direct-leg reason
