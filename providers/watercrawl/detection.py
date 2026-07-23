"""watercrawl.detection — the "did we actually get the real page?" predicates + the dead-host registry.

用一句话讲完: 三个纯判定函数决定 fallback 链要不要升级 —— `looks_walled`(链接太少 or 命中挑战页 marker → headless
被墙,该换 impersonate/residential/camoufox)、`render_thin`(渲染结果还是导航壳、正文 JS 没加载 → 值得等更久重试)、
`dead_host`(host 本身 DNS/证书挂了 → 别把空渲染当基础设施失败)。WHY 独立成层: firecrawl 把"内容够不够好"做成显式判定
函数而非埋在渲染代码的 if/else 里 —— 我们照抄,让 render/orchestrator 只负责"抓",detection 只负责"判",升级决策一处可读。
{RESEARCH firecrawl `isLongEnough`/status-code 判定 + crawl4ai `antibot_detector.py` 独立文件} [CONFIDENCE: CONFIRMED —
verbatim 迁移自 pool.py:732-771].
"""
from __future__ import annotations

import re

# Challenge-interstitial body markers across EN/JP/CN/KR — a page whose text contains any of these is a bot-wall
# challenge page, NOT the real content. {POOL.PY:732-736}.
_WALL_MARKERS = ("just a quick security check", "verifying the security", "ray id", "request unsuccessful",
                 "access denied", "attention required", "checking your browser", "enable javascript and cookies",
                 "challenge-platform", "cf-chl", "__cf_chl", "cf-turnstile", "turnstile", "_incapsula_",
                 "確認しています", "しばらくお待ち", "セキュリティチェック", "アクセスが拒否",
                 "安全验证", "请稍候", "正在验证", "拒绝访问", "보안 확인", "잠시만 기다")


def looks_walled(text: str, links: list) -> bool:
    """True when the browser render did NOT get the real page — either near-empty (a bot-wall that ERR'd/timed out →
    we got nothing) or a challenge interstitial (wall-marker body). Both mean 'the headless-from-datacenter render was
    blocked → try the impersonate/webshare/camoufox fallback'. GENERAL, no per-site logic. {POOL.PY:739-746}."""
    if len(links) < 5:                                    # a real IR listing/nav always has ≥5 links; fewer = blocked
        return True
    low = (text or "").lower()
    return any(m in low for m in _WALL_MARKERS)


def is_challenge(text: str) -> bool:
    """True when the BODY itself is a bot-challenge / block page (Incapsula / Cloudflare / Akamai …), independent of
    link count. WHY separate from looks_walled: at the END of the fallback chain we must know whether the page is a
    DEFINITE block page (→ never return its body as a successful render) vs merely link-sparse (→ maybe a legit small
    page). {DEBUG 2026-07-23 pepsico: body was "request unsuccessful. incapsula incident id ..." yet render_shot
    returned it as method='render' → 0 events, failed_render=0} [CONFIDENCE: CONFIRMED 100% — the block body hits a marker]."""
    low = (text or "").lower()
    return any(m in low for m in _WALL_MARKERS)


def render_thin(text: str) -> bool:
    """True when a render RESULT is still a NAV/JS-APP SHELL — the JS/AJAX content has not loaded yet, so a longer
    wait is worth trying. Signal: near-empty OR no sentence-terminating punctuation (a nav menu is Title-Case labels;
    real press-release/filing content is prose with sentences). {POOL.PY:763-771; 2026-07-22 PFS .aspx: wait=0 = 2033
    chars of pure menu, 0 sentence terminators; the filing content was JS-loaded} [CONFIDENCE: CONFIRMED]."""
    if not text or len(text.strip()) < 200:
        return True
    # sentence terminator followed by a space/quote/bracket OR a period jammed against a capital ("...end.Next")
    return len(re.findall(r"[.。][ \t\"'”)\]]|\.[A-Z]", text)) < 2


# ── dead-host registry: hosts whose render failed with a DEAD/BAD-host browser error (DNS/cert/aborted). The caller
# uses dead_host() to NOT count an empty render as an infra failure. Written by orchestrator._render_with_wait_retries.
_DEAD_HOST_ERRORS = ("ERR_NAME_NOT_RESOLVED", "ERR_CERT_", "ERR_SSL", "ERR_ABORTED")
_DEAD_URLS: set = set()


def is_dead_error(error_str: str) -> bool:
    """True if a browser error string names a DEAD-host condition (DNS/cert/SSL/aborted) — orchestrator uses this to
    decide whether to record the url as dead. {POOL.PY:749 "_DEAD_HOST_ERRORS"}."""
    return any(m in error_str for m in _DEAD_HOST_ERRORS)


def mark_dead(url: str) -> None:
    """Record url as a dead host so dead_host(url) returns True hereafter (called by the render ladder on a DEAD err)."""
    _DEAD_URLS.add(url)


def dead_host(url: str) -> bool:
    """True if url's last render failed with a DEAD/BAD-host browser error (see _DEAD_HOST_ERRORS) — the caller uses
    this to NOT count the empty render as an infra failure. {POOL.PY:753-756}."""
    return url in _DEAD_URLS
