"""watercrawl.walls.registrant — the GUEST identity used to pass a public webcast's self-service registration form.

用一句话讲完: 很多 IR webcast(Q4/veracast/kvgo/open-exchange)在放 player 前有一道"填个名字邮箱就进"的 guest 注册墙 —
这不是真凭证墙,是防爬的软 gate,填一个通用 guest 身份就过。这里放那个默认身份(env 可覆盖),login/platform handler
共用它填表。WHY 独立成文件: 身份是配置不是逻辑 —— 换 demo email 不用碰填表代码,且集中一处避免每个 handler 各写一份。
{OLD _capture.py 的 `reg` dict 由 caller 传 reg_json;这里把它变成 watercrawl 自带的默认 + env 覆盖} [CONFIDENCE: CONFIRMED].

这是 GUEST-path 专用:只用于本就对公众开放、仅要求"填表登记"的 webcast(guest/without-account 路径)。不用于、也过不了
需要真实 per-attendee 凭证的 credential 墙(那种由 platform handler 检测并 flag,不伪造)。
"""
from __future__ import annotations

import os


def guest() -> dict:
    """The default guest registrant {first,last,email,company,title}. Env-overridable so ops can set a real-looking
    demo identity without a code change. Used by login.register_generic + every platform guest-path handler."""
    return {
        "first": os.environ.get("WATERCRAWL_REG_FIRST", "Alex"),
        "last": os.environ.get("WATERCRAWL_REG_LAST", "Analyst"),
        "email": os.environ.get("WATERCRAWL_REG_EMAIL", "ir.research.guest@gmail.com"),
        "company": os.environ.get("WATERCRAWL_REG_COMPANY", "Independent Research"),
        "title": os.environ.get("WATERCRAWL_REG_TITLE", "Analyst"),
    }
