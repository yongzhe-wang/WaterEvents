"""watercrawl.walls.deadpage — detect a PERMANENTLY-gone page (hard 4xx/5xx OR soft-404 body) so we don't retry it.

用一句话讲完: 一个 webcast/event 页可能是永久失效的 —— 要么服务器直接 4xx/5xx,要么更阴的 soft-404(返回 200 但正文写
"no longer available"/"webcast has ended",骗过只看状态码的检查)。这里一个纯函数判定"这页是不是死了",让 caller 早退、
别对永久没内容的 URL 空烧整条 fallback 链(camoufox 一次几十秒)。WHY 纯文本函数: 判定只需 text + 可选 status,不碰浏览器 —
可在渲染后、升级前廉价一判。[CONFIDENCE: CONFIRMED — soft-404=200+'no longer
available' 是行业标准;could-not-fetch audit 实测 event.webcasts.com/cc.webcasts].
"""
from __future__ import annotations

# soft-404 body phrases: a page that returns 200 but whose rendered text says the content is gone. Keys on the phrase,
# not any company/host, so it's general.
_DEAD_PHRASES = (
    "no longer available", "presentation has ended", "presentation is not available",
    "webcast has ended", "event has ended", "event has expired", "has expired",
    "no longer accepting", "this content is not available", "video is not available",
    "page not found", "404 not found", "content you requested could not be found",
)


def looks_dead(text: str, status: int = 0) -> str:
    """Return a non-empty REASON string when the page is permanently gone (hard 4xx/5xx status OR a soft-404 body
    phrase), else "". The reason feeds the trace + tells the caller to stop escalating fallbacks on this url."""
    if status and status >= 400:                          # hard dead — server says the page is gone
        return "http_%d" % status
    low = (text or "").lower()[:8000]                     # scan the head of the rendered body (soft-404 lives up top)
    for ph in _DEAD_PHRASES:
        if ph in low:
            return "expired/removed: " + ph
    return ""
