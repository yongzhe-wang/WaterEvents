"""watercrawl.walls — INTERACTIVE wall breakers: get a page past a consent banner / registration / login gate.

用一句话讲完: bot-wall(Cloudflare/Akamai 指纹墙)由 engines/ 的 fallback 链破;这里破的是**"人类门"** —— cookie/GDPR 同意
条、webcast 的 guest 注册墙、平台特有的多屏/iframe/disabled-button 登录墙。`break_walls(page, url)` 一步做完:①总是先关
consent 条(cheap+safe);②URL 命中已知 webcast 平台(Q4/veracast/kvgo/open-exchange)→ 跑该平台的 guest-path handler;
③否则若**检测到是注册墙**(有 email 输入 + register 按钮 + 内容稀薄)→ 跑 generic 填表兜底。WHY 这个门控顺序: consent 无害
可无脑关;platform handler 按 host 匹配安全;但 generic register 会填表+提交,**绝不能对任意页盲跑**(会误填搜索/订阅框),
所以只在注册墙 heuristic 命中时才 fire。{OLD _capture.py 的 handler_for + register_generic dispatch;移植 + sync→async +
加 consent + 加 gate heuristic} [CONFIDENCE: CONFIRMED — 直接迁移 + 安全门控].

Upstream: render.py 的渲染协程在 settle 后调 break_walls;若它 acted(点穿了一道墙)→ caller 再 settle 一次让真内容加载。
Downstream: page 上真实内容露出 → EXTRACT_JS 抽到的是内容而非表单/banner。
"""
from __future__ import annotations

import os

from . import consent, deadpage, login, registrant
from . import platforms

# generic-register 全局开关(默认开,用户要 login-wall-break)。关掉只保留 consent + platform-matched handler(最安全档)。
_BREAK_LOGIN = os.environ.get("WATERCRAWL_BREAK_LOGIN", "1") == "1"

# 注册墙 heuristic:一页是"注册 gate"当且仅当 —— 有可见 email 输入 + 有 register/continue 类提交按钮 + 内容链接稀薄
# (真内容页即使有 newsletter 框也有很多链接;gate 页几乎只有那个表单)。返回 bool。避免对任意含表单的页盲填。
_GATE_JS = r"""() => {
  const vis = el => el && el.offsetParent !== null;                       // 只看可见元素
  const emailIn = [...document.querySelectorAll('input[type="email"], input[name*="mail" i], input[id*="mail" i], input[placeholder*="mail" i]')].some(vis);
  if (!emailIn) return false;                                             // 没 email 输入 → 不是注册墙
  const RX = /register|continue|sign\s?in|log\s?in|watch|view|join|submit|enter|attend/i;
  const btn = [...document.querySelectorAll('button, [role="button"], input[type="submit"], a')].some(b => vis(b) && RX.test((b.innerText||b.textContent||b.value||'')));
  if (!btn) return false;                                                 // 没提交类按钮 → 不是注册墙
  const links = document.querySelectorAll('a[href^="http"]').length;      // 内容链接数:gate 页很少
  return links < 25;                                                      // 稀薄 → 判为 gate(内容页链接远多于此)
}"""


async def _looks_registration_gate(page) -> bool:
    """True when the page looks like a dedicated registration/login GATE (email input + submit-verb button + few
    content links) — the safe precondition for running the generic form-fill. Never raises."""
    try:
        return bool(await page.evaluate(_GATE_JS))
    except Exception:                                     # noqa: BLE001 — eval hiccup → treat as not-a-gate (skip generic)
        return False


async def break_walls(page, url: str, dbg: dict | None = None) -> bool:
    """Get `page` past a consent banner / registration / login gate IN PLACE. Returns True if it clicked through a
    wall (→ the caller should re-settle so post-click content loads). Order: consent (always) → platform handler
    (host-matched) → generic register (only if the page is a registration gate). Best-effort, never raises into render."""
    dbg = dbg if dbg is not None else {}
    acted = False
    # 1) consent banner — always, cheap + safe (only clicks known-CMP / consent-scoped accept buttons)
    if await consent.dismiss_consent(page, dbg):
        acted = True
    if not _BREAK_LOGIN:                                  # login-wall break disabled → consent-only mode
        return acted
    # 2) per-platform webcast handler — fires only when the URL matches a known platform host (safe by construction)
    h = platforms.handler_for(url)
    if h is not None:
        try:
            dbg["platform"] = getattr(h, "NAME", getattr(h, "__name__", "?"))
            reg = registrant.guest()
            if bool(await h.register(page, list(page.frames), reg, dbg, login.fill_in)):
                return True                              # platform handler reached the player/content → done
        except Exception as _e:                          # noqa: BLE001 — a platform handler error must not sink the render
            dbg["platform_err"] = str(_e)[:140]
    # 3) generic register — ONLY when the page is a registration gate (guards against filling a random search/signup box)
    if not h and await _looks_registration_gate(page):
        reg = registrant.guest()
        for scope in [page] + list(page.frames):         # try the main frame + any embedded registration iframe
            try:
                if await login.register_generic(scope, reg, dbg):
                    return True
            except Exception:                            # noqa: BLE001
                pass
    return acted
