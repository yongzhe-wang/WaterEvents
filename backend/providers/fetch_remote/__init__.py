"""fetch_remote — move the DOWNLOAD itself onto the render VM, so ir-media-8 never touches the bytes.

用一句话讲完: officeall.extract(url) / audio_extract.extract(url) 的签名不变, 内部改成 POST 一个 url 给
ir-render-16, 由它去下载 + 转发给 RunPod 抽取, 只把 DocResult / AudioResult 送回来。ir-media-8 从此只发 URL、收结果,
文件字节一次都不经过它。切换开关是 FETCH_REMOTE_URL 一个环境变量(分流点在两个 tools 包的 __init__.py)。

WHY 搬 —— 三条理由, 内存是最弱的那条:

  1. **出口 IP 不一致, 是我们自己制造的反爬签名。** 网站看到的是: 一个 IP 用浏览器打开了页面, 几秒后另一个 IP
     来下载页面里的文件。真人不会这样。
     {GCLOUD 2026-08-04 — ir-media-8 EXTERNAL 35.254.161.69 / ir-render-16 EXTERNAL 136.112.158.156}
     [CONFIDENCE: CONFIRMED — read from the live instance list].

  2. **下载完全绕过限速。** render 路径查 politeness, 下载路径一次都不查 —— 两条通道各自往同一个主机打, 谁都不
     知道对方打了多少。搬过去之后两者在**同一个进程**里共享同一份 per-host 节流状态, 这才是真正的修复。
     {GREP 2026-08-04 — render.py politeness 引用 15 处, capture.py 5 处,
      tools/audio_extract/fetch.py 0 处, tools/officeall/fetch.py 0 处}
     [CONFIDENCE: CONFIRMED — counted across the whole backend].

  3. 内存: fetch 是**先把整个文件读进内存**再转发的, 上限 300MB/个。24 个并发槽的最坏情况 7.2GB, 这决定了
     ir-media-8 能缩到多小。
     {SERVER CONTENT-LENGTH 2026-08-03 — 一个 choruscall 财报音频 91,723,583 字节}
     [CONFIDENCE: CONFIRMED — the e2e test logged "118.8s ... 91.7MB" downloaded on ir-media-8].

WHY 形状必须是"render VM 下载**并直接转发**给 RunPod", 而不是"下载后还给 ir-media-8":
    源站 → ir-media-8 → RunPod            现在: 2 段, ir-media-8 吃内存
    源站 → render VM → ir-media-8 → RunPod  做错: 3 段, 更差
    源站 → render VM → RunPod             做对: 2 段, ir-media-8 只过 URL 和结果
所以 render VM 自己要有一条到 RunPod 的隧道, 并设 DOCLING_REMOTE_URL / WHISPER_REMOTE_URL —— 它上面的
officeall.extract 于是变成"本地 fetch + 远端抽取", 也就是 ir-media-8 原本在做的事, 原样搬了个位置。

上游触发: tools/officeall/__init__.py 与 tools/audio_extract/__init__.py 的分流。
下游连接: ir-render-16:8100 的 /fetch_doc 和 /fetch_audio。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

_URL = os.environ.get("FETCH_REMOTE_URL", "").rstrip("/")

# Download + extract end to end. The download alone can be 91.7 MB over a slow origin, and the extraction behind it is
# Docling (worst observed 735s on one document) or whisper (1370s for a 63.7-minute call at 2.8x realtime on a shared
# A40). This timeout covers BOTH legs, so it has to clear the sum of the two worst cases, not either one.
# {MEASURED 2026-08-04 — whisper 63.7min audio in 1370.3s; docling worst single document 735.2s}
# [CONFIDENCE: CONFIRMED — both from live runs].
_TIMEOUT = float(os.environ.get("FETCH_REMOTE_TIMEOUT_S", "2400"))
_RETRIES = int(os.environ.get("FETCH_REMOTE_RETRIES", "1"))       # ONE retry: these calls are expensive, and a repeat
                                                                  # re-downloads the whole file. Transport blips only.
_BACKOFF_S = float(os.environ.get("FETCH_REMOTE_BACKOFF_S", "5"))


def _loud(msg: str) -> None:
    """Fail-loud channel — a render VM that cannot fetch must be visible here, not inferred from empty documents."""
    print(f"[fetch_remote] {msg}", file=sys.stderr, flush=True)


def _post(path: str, payload: dict) -> dict | None:
    """POST json → decoded dict, or None when the TRANSPORT failed (distinct from a fetch that legitimately found
    nothing). Retries only transport classes; a 4xx is our own bug and fails loudly at once."""
    if not _URL:
        _loud("FETCH_REMOTE_URL is empty — client called with no endpoint configured")
        return None
    req = urllib.request.Request(f"{_URL}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    last = ""
    for attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500:
                _loud(f"{path} → {last} (client error, not retried): {e.read()[:200]!r}")
                return None
        except Exception as e:                                    # noqa: BLE001 — URLError, timeout, reset, bad json
            last = f"{type(e).__name__}: {str(e)[:120]}"
        if attempt < _RETRIES:
            time.sleep(_BACKOFF_S)
    _loud(f"{path} FAILED after {_RETRIES + 1} attempts ({last}) — render vm unreachable at {_URL}")
    return None


def office_extract(url: str, proxy: str | None = None, want_structured: bool = False):
    """Remote twin of tools.officeall.extract → a real DocResult, not a dict, so call sites are untouched.

    `proxy` is accepted and IGNORED on purpose: the residential proxy lives in the render VM's own environment
    {RENDER.ENV on ir-render-16 carries WEBSHARE_PROXY}, and shipping a credential across the wire on every call to
    hand it back to the machine that already has it would be both pointless and a leak surface."""
    from tools.officeall.types import DocResult                   # lazy: tools imports providers, so a top-level
                                                                  # import here would close an import cycle
    out = _post("/fetch_doc", {"url": url, "structured": bool(want_structured)})
    if out is None:
        # NOT 'no-content' and NOT 'fetch-empty' — both of those are statements about the DOCUMENT. This is a statement
        # about our own render VM. Same distinction render_remote and tools_remote already draw.
        return DocResult(source="url", error="transport-error", via="transport-error")
    return DocResult(**{k: out.get(k, v) for k, v in {
        "format": "", "text": "", "tables": [], "structured": {}, "n_pages": 0,
        "n_tables": 0, "n_bytes": 0, "source": "", "via": "", "warnings": [], "error": "",
    }.items()})


def audio_extract(url: str, proxy: str | None = None):
    """Remote twin of tools.audio_extract.extract → a real AudioResult. `proxy` ignored for the same reason as above."""
    from tools.audio_extract.types import AudioResult             # lazy: see office_extract
    out = _post("/fetch_audio", {"url": url})
    if out is None:
        return AudioResult(source="url", error="transport-error", via="")
    return AudioResult(**{k: out.get(k, v) for k, v in {
        "transcript": "", "segments": [], "language": "", "duration": 0.0,
        "n_bytes": 0, "source": "", "via": "", "error": "",
    }.items()})
