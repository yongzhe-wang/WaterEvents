"""watercrawl.walls.login — GENERIC registration/login-wall break: fill the guest form + click through.

用一句话讲完: 一页如果是"填 First/Last/Email/Company 再点 Register/Continue 才给看内容"的软注册墙,这里通用地把字段
按 label/placeholder 填上 guest 身份、再点 Register/Continue/Accept/Agree 之类的按钮/链接过掉它 → 真内容露出来。WHY 通用
兜底: 具体 webcast 平台(Q4/veracast/kvgo/open-exchange)有各自的 guest-path 怪癖(多屏、iframe、disabled 按钮),那些
由 walls/platforms/ 的 per-platform handler 处理;这里是"没有专属 handler 匹配时"的 generic fallback。{OLD _capture.py:
register_generic + fill_in} [CONFIDENCE: CONFIRMED — verbatim 逻辑迁移,sync→async 适配].

安全: 这是 caller 明确判定"这是注册墙"后才调的(walls.break_walls 的 gate),不会对任意页盲填 —— 否则会误填搜索框/
订阅框并误提交。fill_in 只填匹配到的字段,register_generic 返回是否点了提交。
"""
from __future__ import annotations


async def fill_in(scope, names: list[str], val: str) -> bool:
    """Fill the FIRST field matching any of `names` (by placeholder OR label, case-insensitive) with `val`.
    Returns True if a field was filled. Shared helper — also handed to the platform handlers so they reuse the
    same label/placeholder targeting. {OLD _capture.py:109-121 fill_in}."""
    for n in names:
        for gtr in (lambda n=n: scope.get_by_placeholder(n, exact=False),
                    lambda n=n: scope.get_by_label(n, exact=False)):
            try:
                loc = gtr()
                if await loc.count() > 0:
                    await loc.first.fill(val, timeout=2000)
                    return True
            except Exception:                             # noqa: BLE001 — a locator that errors → try the next strategy
                pass
    return False


async def register_generic(scope, reg: dict, dbg: dict) -> bool:
    """GENERIC fill+click for a self-service guest registration form — the fallback when no platform handler matches.
    Fills the standard First/Last/Company/Email/Name/Title fields, then clicks the first Register/Continue/Accept-type
    button (or link). Returns True if it clicked a submit control. {OLD _capture.py:123-147 register_generic}."""
    # fill every standard registrant field that the form exposes (missing fields are simply skipped)
    for names, val, tag in [(["First", "First Name", "Given"], reg["first"], "first"),
                            (["Last", "Last Name", "Surname", "Family"], reg["last"], "last"),
                            (["Company", "Organization", "Firm", "Institution"], reg["company"], "company"),
                            (["Email", "E-mail", "Work Email"], reg["email"], "email"),
                            (["Name", "Full Name"], reg["first"] + " " + reg["last"], "name"),
                            (["Title", "Job Title", "Role"], reg.get("title", "Analyst"), "title")]:
        if await fill_in(scope, names, val):
            dbg.setdefault("filled", []).append(tag)
    # click the submit control — try buttons first (the common case), then links (some platforms use an <a>)
    for lbl in ["Register", "Create", "Join", "Submit", "View", "Continue", "Enter", "Watch", "Accept", "Agree"]:
        try:
            btn = scope.get_by_role("button", name=lbl, exact=False)
            if await btn.count() > 0:
                await btn.first.click(timeout=2000)
                dbg["clicked"] = lbl
                return True
        except Exception:                                 # noqa: BLE001 — button not clickable → try the next label
            pass
    for lbl in ["Enter", "Continue", "Launch", "Join", "Click here"]:   # some platforms use a link, not a button
        try:
            ln = scope.get_by_role("link", name=lbl, exact=False)
            if await ln.count() > 0:
                await ln.first.click(timeout=2000)
                dbg["clicked"] = "link:" + lbl
                return True
        except Exception:                                 # noqa: BLE001
            pass
    return False
