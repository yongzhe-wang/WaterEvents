"""watercrawl.walls.platforms — per-PLATFORM webcast registration/login-wall handlers (matched by host, not company).

用一句话讲完: 每个子模块处理一个 webcast 平台(按 host 匹配)—— 它驱动浏览器过掉那个平台特有的注册/登录/launch 墙(Q4
多屏 guest-path、veracast 'Not registered?' 自助注册、kvgo 晚加载 iframe、open-exchange disabled 提交按钮),好让真内容/
player 露出。generic register(walls/login.py)是没有专属 handler 匹配时的兜底。WHY 按 platform 不按 company: 一个 webcast
平台(KnowledgeVision/Q4/open-exchange/veracast)服务成百上千家公司 —— 按平台 host 分是结构性的、非硬编码,任何 IR crawl
都会撞上这几个平台。{OLD webcast_platforms/__init__.py handler_for} [CONFIDENCE: CONFIRMED — 直接迁移].

Handler contract (each submodule exports):
    NAME: str
    matches(url: str) -> bool
    async def register(page, frames, reg, dbg, fill_in) -> bool   # True if it reached/loaded the player
"""
from __future__ import annotations

from . import kvgo, q4inc, open_exchange, veracast

_HANDLERS = [kvgo, q4inc, open_exchange, veracast]


def handler_for(url: str):
    """Return the platform handler whose matches(url) is True, else None (→ walls.login.register_generic fallback)."""
    for h in _HANDLERS:
        try:
            if h.matches(url):
                return h
        except Exception:                                 # noqa: BLE001 — a matcher that errors must not block the others
            pass
    return None
