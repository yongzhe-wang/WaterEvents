"""TLS/HTTP2-impersonating fetch — watercrawl's bypass for Akamai/Cloudflare bot-walls that block BOTH headless
Chromium AND plain requests at the fingerprint layer.

用一句话讲完: 用 curl_cffi 的 `impersonate="chrome"` 发一个**跟真 Chrome 完全一致的 TLS 指纹 + HTTP/2 SETTINGS +
cipher/ALPN** 的 GET,骗过 Akamai 的指纹检查,拿回墙后的 HTML → 解析出 text + links。WHY: 实测 investor.gm.com /
investor.colgatepalmolive.com / investors.nxp.com 对 headless Chromium 回 net::ERR_HTTP2_PROTOCOL_ERROR、对 plain
curl 回 HTTP 000(连接层就拒)——都是 TLS/HTTP2 指纹级封锁;curl_cffi impersonate 让三家全 HTTP 200(GM 148KB/61
links)。$0、无代理、无 key。{WEBSEARCH 2026-07-04 "CURL-CFFI USES LIBCURL'S IMPERSONATION MODE TO REPLICATE
CHROME'S TLS FINGERPRINT EXACTLY — INCLUDING HTTP/2 SETTINGS, CIPHER SUITES, AND ALPN"; LOCAL PROBE: GM/CL/NXPI
HEADLESS=ERR → CURL_CFFI=HTTP 200} [CONFIDENCE: CONFIRMED — live-tested all three].

Limit (honest): curl_cffi does NOT execute JS, so a page whose event list is purely XHR-rendered yields only the
SSR HTML + nav (still far past the 0-link wall). The crawl's BFS follows those nav links, and Q4/RSS adapters
(also routed through impersonation) hit the platform's JSON API where the real archive lives. The hardest Akamai
tier (a sensor.js JS challenge) still needs residential proxies — but the fingerprint-only walls (the common
case for IR sites) fall to this. {WEBSEARCH "CURL-CFFI CAN PASS TLS CHECKS ... BUT CANNOT GENERATE SENSOR
PAYLOADS FOR PAGES THAT LOAD SENSOR.JS"} [CONFIDENCE: CONFIRMED — fingerprint-tier yes, sensor-tier no].

Upstream: watercrawl.orchestrator.render_full/render_detail/render_shot call fetch() when the headless render comes
back walled/thin. Downstream: the (text, links) fold into the crawl link set exactly like a rendered page.
"""
from __future__ import annotations

import re
import sys
import urllib.parse

from .. import politeness      # SSRF + robots gate; the SYNC entry point — this lane runs off-loop

# Chrome UA to match the impersonated fingerprint (curl_cffi sets the TLS/HTTP2 layer; UA keeps the app layer consistent).
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_TIMEOUT = 20                                              # per-GET ceiling (s) — a walled host must not hang the crawl
# EVERY href — absolute OR relative. WHY relative too: SSR IR sites (GM is Drupal) embed their event-detail
# links as RELATIVE paths (href="/events/event-details/..."), so an absolute-only regex silently dropped GM's
# whole event list (GM stuck at +15 despite the render passing). We capture all hrefs and urljoin them against
# the page url below. {LOCAL 2026-07-04 "GM HTML has /events/event-details/... relative; absolute-only regex
# missed them"} [CONFIDENCE: CONFIRMED — the events were IN the html, just relative].
_HREF_RE = re.compile(r'href=["\']([^"\'>\s]+)["\']', re.I)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)     # strip script/style before text extraction
_STRIP_RE = re.compile(r"<[^>]+>")                                      # crude tag-strip → visible text


def available() -> bool:
    """True if curl_cffi is importable in this process (it's an optional dep; absent → this lane is skipped)."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:                                      # noqa: BLE001 — not installed / broken → lane unavailable
        return False


def get(url: str, *, want_json: bool = False):
    """Raw impersonated GET → the curl_cffi Response, or None on any failure. `want_json` is a hint only (the
    caller decides how to read .text/.json()). Used by the platform adapters to reach Q4/RSS JSON APIs through
    the wall. WHY a thin get(): the adapters need the Response (status + json), while fetch() below returns the
    parsed (text, links) shape the render path wants."""
    # POLICY GATE for the NON-BROWSER lane. Adding the gate to page.goto covered every browser navigation and left this
    # one open: curl_cffi issues a raw GET that never touches Playwright, so it had no scheme check, no SSRF check and no
    # per-host pacing. It is also the lane most able to do damage — no browser sandbox between the fetch and the reply,
    # and the whole reason it exists is to reach hosts that actively resist us.
    # The SYNC gate is correct here: get() is a plain function called from a worker thread, not from the render loop.
    # {SHELL 2026-07-29 "grep -n 'politeness|host_is_public|url_allowed' engines/impersonate.py → no matches"}
    # [CONFIDENCE: CONFIRMED 100% — the absence was grepped, and the sync/async choice follows from get() having no
    #  running loop of its own.]
    ok, why = politeness.url_allowed(url)
    if not ok:
        print(f"[impersonate] refused {url[:70]} — {why}", file=sys.stderr, flush=True)
        return None
    politeness.wait_turn(url)                             # sync pacing: correct off-loop, and this lane is off-loop
    try:
        from curl_cffi import requests as creq            # import here: optional dep must not break module import
        _hdrs = {"User-Agent": _UA, "Accept": "*/*"}

        def _try(proxies, timeout):
            r = creq.get(url, impersonate="chrome", timeout=timeout, headers=_hdrs,
                         **({"proxies": proxies} if proxies else {}))
            return r if r.status_code == 200 else None    # only a 200 carries usable content

        # ALWAYS attempt the DIRECT curl_cffi Chrome-fingerprint GET first — it is the WHOLE POINT of get(): it cracks
        # the Akamai/Imperva TLS+HTTP2 fingerprint wall to reach a Q4/RSS JSON API RENDER-FREE (GM/AKAM: plain requests
        # HTTP 000 → curl_cffi 200; platform_adapters._get:60-68 relies on exactly this). Then, only on failure, the
        # residential-proxy fallback for a genuine datacenter block.
        #
        # ROOT-CAUSE REVERT (2026-07-21): a host_health circuit-breaker (is_dead / skip_direct) was added here and it
        # ZEROED every Akamai-fronted Q4 company (AKAM 413→0). The breaker is FED BY RENDER-PATH failures — Chromium
        # blocked on the event-detail PAGES of ir.akamai.com — but that is a DIFFERENT fetch method than this fingerprint
        # GET to the SAME host's Q4 JSON API. Marking the host DIRECT_BLOCKED/DEAD from render failures then made get()
        # SKIP the very curl_cffi fingerprint fetch that cracks the wall → the Q4 adapter got nothing → 0 events. Render
        # blocking ≠ fingerprint blocking; the circuit-breaker MUST NOT gate the adapter path. {LOG 2026-07-21 AKAM
        # "impersonate ALSO blocked/empty (got 0 links)" while _arch_054229 AKAM=413} [CONFIDENCE: CONFIRMED — the
        # skip_direct gate conflated the two fetch methods; get() must always try its own fingerprint direct].
        r = _try(None, min(_TIMEOUT, 8))                  # direct (datacenter IP), short ceiling — fails fast if truly walled
        if r is None:
            from ... import webshare           # lazy: the residential-proxy provider (env-configured; providers/webshare)
            if webshare.enabled():
                r = _try(webshare.curl_proxies(), _TIMEOUT)   # datacenter-blocked → residential fallback, full timeout
        return r
    except Exception:                                      # noqa: BLE001 — fingerprint miss / network → no data here
        return None


def fetch(url: str) -> tuple[str, list, str]:
    """Impersonated fetch → (text, links, html). Empty ("", [], "") when the wall still blocks us (sensor.js
    tier) or curl_cffi is absent. The HTML is returned too so the caller can gather_controls on it, exactly
    like a headless render."""
    r = get(url)
    if r is None:
        return "", [], ""
    html = r.text or ""
    # Absolutize EVERY href against the page url (relative → absolute), keep only http(s), drop in-page anchors.
    base = url if "://" in url else "https://" + url
    seen: set = set()
    links: list = []
    for href in _HREF_RE.findall(html):
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):   # not a navigable page link
            continue
        absu = urllib.parse.urljoin(base, href)            # relative (/events/...) → absolute; absolute stays as-is
        if absu.startswith("http") and absu not in seen:
            seen.add(absu)
            links.append(absu)
    text = _STRIP_RE.sub(" ", _TAG_RE.sub(" ", html))      # script/style-stripped, tag-stripped visible text
    text = " ".join(text.split())                          # collapse whitespace
    return text, links, html
