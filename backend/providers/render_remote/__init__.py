"""render_remote — the client half of the render split: the same four watercrawl entries, served over HTTP by
ir-render-16 instead of by Chromium on the caller's own cores.

用一句话讲完: 每个函数签名跟 providers.watercrawl 里的本地版**一模一样**,内部换成 POST 到 render VM,再把 JSON
还原成本地版返回的那个形状 —— 所以 event_agent / media_agent 的调用点一行都不用改,只靠 `RENDER_REMOTE_URL` 这一个
环境变量在本地和远程之间切换(分流点在 providers/watercrawl/__init__.py 末尾)。

WHY 这个模块存在: 渲染是纯 CPU + 内存的活,占单元时间预算的 50.9%,却跟 Docling 文档抽取、whisper 语音转写一起
挤在 ir-media-8 的 8 个核上 {MEASURED n=7,655 units — RENDER 17.4s (50.9%) vs DB 0.14s}。三种负载互相抢核,谁都
跑不快。拆开之后 worker 那台只发请求 + 写 DB。
[CONFIDENCE: CONFIRMED — per-stage timing collected over the live fleet].

上游触发: providers.watercrawl 的 __init__ 分流。下游连接: ir-render-16:8100 的 providers.watercrawl.service。

FAIL-LOUD CONTRACT: 跟本地版一样 **never raises** —— 但传输层失败返回 method="transport-error",绝不返回 ""。
这个区分是有意的: "" 的意思是"四层渲染链全被这个网站打穿了",是对方的问题;传输失败是**我们自己的服务不可达**,
如果混成同一个值,一次 render VM 重启会被整条流水线记成几百个网站渲染失败,然后 unit 落到 status='failed' ——
那个状态的意思是"这里坏了,去看一眼",而去看只会发现网站好好的。同一个坑 render.py 的 robots-denied 已经踩过一次。
{RENDER.PY "RETURNING A DISTINCT METHOD MAKES THE REFUSAL TERMINAL AND FREE, AND KEEPS 'FAILED' MEANING WHAT IT SAYS."}
[CONFIDENCE: CONFIRMED — same reasoning, same file, applied to the transport layer].
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

_URL = os.environ.get("RENDER_REMOTE_URL", "").rstrip("/")

# A render_shot walks up to four engine tiers (chromium → residential patchright → camoufox → impersonate), each with
# its own nav timeout, so the SERVER-side worst case is minutes. The client timeout must sit ABOVE that or it would cut
# off renders the server was going to complete — turning a slow success into a false failure.
# {CONFIG.PY:42 "NAV_TIMEOUT_MS = INT(OS.ENVIRON.GET(\"IR_WATERCRAWL_NAV_TIMEOUT_MS\", \"22000\"))" — ×4 tiers + settle}
# [CONFIDENCE: INFERRED — 22s nav × 4 tiers + settle + screenshot ≈ 110s worst case; 240s leaves headroom].
_TIMEOUT = float(os.environ.get("RENDER_REMOTE_TIMEOUT_S", "240"))

# Retries cover ONLY transport-level failures (connection refused / reset while the service restarts). A render that
# came back empty is NOT retried here — media_agent.render_retry already owns that policy with backoff, and retrying in
# two layers would multiply the attempt count silently.
_RETRIES = int(os.environ.get("RENDER_REMOTE_RETRIES", "2"))
_BACKOFF_S = float(os.environ.get("RENDER_REMOTE_BACKOFF_S", "2"))

_SHOT_EMPTY = {"text": "", "links": [], "html": "", "shot_b64": "", "method": "", "inline": ""}


def _loud(msg: str) -> None:
    """Fail-loud channel — a render VM that is down must be visible in the worker log, not inferred from empty results."""
    print(f"[render_remote] {msg}", file=sys.stderr, flush=True)


def _post(path: str, payload: dict) -> dict | None:
    """POST one json body to the render service → the decoded dict, or None when the TRANSPORT failed.

    None is reserved for "we could not reach the service / it returned garbage". A successful call that produced an
    empty render returns a dict — the caller must be able to tell those two apart, which is the whole point of this
    module's fail-loud contract (see module docstring).

    Retries only the transport classes: URLError (dns/connect/reset) and 5xx. A 4xx is our own bug and is not retried —
    retrying a malformed request just delays the loud failure."""
    if not _URL:
        _loud("RENDER_REMOTE_URL is empty — client called with no endpoint configured")
        return None
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{_URL}{path}", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    last = ""
    for attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500:                                  # our bug, not a transient — fail loudly right now
                _loud(f"{path} → {last} (client error, not retried): {e.read()[:200]!r}")
                return None
        except Exception as e:                                # noqa: BLE001 — URLError, timeout, socket reset, bad json
            last = f"{type(e).__name__}: {str(e)[:120]}"
        if attempt < _RETRIES:
            time.sleep(_BACKOFF_S * (attempt + 1))            # linear backoff: 2s, 4s — the service restart window
    _loud(f"{path} FAILED after {_RETRIES + 1} attempts ({last}) — render vm unreachable at {_URL}")
    return None


def render_shot(url: str, wait_ms: int = 0) -> dict:
    """Remote twin of watercrawl.render_shot → {text, links, html, shot_b64, method, inline}.

    wait_ms=0 is forwarded as "unset" so the server applies its own tuned SETTLE_FIXED_MS default rather than a 0 that
    would skip the settle entirely {CONFIG.PY:53 "SETTLE_FIXED_MS ... (WAS RENDER_SHOT'S 3000)"}."""
    out = _post("/render_shot", {"url": url, "wait_ms": wait_ms})
    if out is None:
        return {**_SHOT_EMPTY, "method": "transport-error"}
    out.setdefault("links", [])
    return {**_SHOT_EMPTY, **out}                              # fill any key the server omitted; caller sees a full dict


def _tuple_call(path: str, url: str, wait_ms: int) -> tuple[str, list, str]:
    """Shared body for render_full / render_detail, which return a tuple the wire has to name and rebuild."""
    out = _post(path, {"url": url, "wait_ms": wait_ms})
    if out is None:
        return "", [], "transport-error"
    return out.get("text", "") or "", list(out.get("links") or []), out.get("method", "") or ""


def render_full(url: str, wait_ms: int = 0) -> tuple[str, list, str]:
    """Remote twin of watercrawl.render_full → (text, links, method). ir_url_agent's IR-homepage nav harvest."""
    return _tuple_call("/render_full", url, wait_ms)


def render_detail(url: str, wait_ms: int = 0) -> tuple[str, list, str]:
    """Remote twin of watercrawl.render_detail → (text, links, method). The deep-page variant."""
    return _tuple_call("/render_detail", url, wait_ms)


def capture_media(url: str, wait_ms: int = 6000) -> dict:
    """Remote twin of watercrawl.capture_media → {media, method, n_requests, error}. Webcast player page → the media
    urls the browser itself requested."""
    out = _post("/capture_media", {"url": url, "wait_ms": wait_ms})
    if out is None:
        return {"media": [], "method": "capture", "n_requests": 0, "error": "transport-error"}
    out.setdefault("media", [])
    return out


def browser_available() -> bool:
    """True when the render service answers AND reports a live browser.

    A service that is up but whose browser failed to launch returns empty results for every url — indistinguishable
    downstream from "every site is broken". So `browser` from /health, not merely a 200, is the availability signal.
    Short timeout: this is a liveness probe, callers use it to decide whether to bother."""
    if not _URL:
        return False
    try:
        with urllib.request.urlopen(f"{_URL}/health", timeout=10) as r:
            return bool(json.loads(r.read().decode()).get("browser"))
    except Exception as e:                                     # noqa: BLE001 — unreachable = unavailable, say so
        _loud(f"health check failed ({type(e).__name__}) — treating render vm as DOWN")
        return False


available = browser_available                                  # friendlier alias, mirrors watercrawl's own export
