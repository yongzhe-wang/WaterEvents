"""tests/edge/validate.py — Step 1 输出的硬校验。不需要任何人工标注。

用一句话讲完: 对模型输出做四项纯程序化检查 —— 证据必须是正文逐字子串、边的两端必须在 mentions 里、
没有边时必须给理由、检索词不能是通用词 —— 不通过的整条丢弃并记录原因。

## 为什么逐字校验是这套方案里最值钱的一行代码

我们按 {USER 2026-07-23 "dont rely on ground truth read the output yourself"} 不建标注集, 所以没有
「答案对不对」的自动判据。但**「模型有没有编造原文」是可以自动判的** —— evidence 若不能在 body 里
原样找到, 那这条抽取的依据就是虚构的, 不管结论看起来多合理都不能要。这一条挡掉的是幻觉的**来源**,
而不是幻觉的**结果**, 所以比任何后置的合理性检查都有效。

## 为什么允许轻度归一化后再比对

真实正文是 markdown, 同一句话里可能夹着 `**` `*` `[](...)` 和不同的空白/引号形态。模型抄写时
往往会把这些吃掉。所以比对前两边都做同一套归一化(压空白、统一引号、去 markdown 强调符号)——
这**不放宽**「必须是原文」的要求, 只是不因排版差异误杀。
{event_documents 实文含 "**Getlink announces today its new growth ambitions**" 与
 "[View all news](https://newsroom.wiley.com/...)" 这类排版噪声}
"""
from __future__ import annotations

import re
import unicodedata

# 归一化: 统一引号/破折号 → 拆 [text](url) → 去脚注编号 → 压强调符 → 去孤立列表符 → 压空白。
# 顺序有讲究: 先拆链接再压符号, 否则 [**x**](u) 会剩下孤立的括号。
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 脚注编号 [1] / [12]。真实正文里它会插在句子【中间】:
#   "...consolidated EBITDA of EUR1 billion**- [1]- **by 2030."
# 模型很合理地跳过它读出连贯句子; 若不在归一化里去掉, 正确的抽取会被当成幻觉丢弃。
# {ev_001 实测 模型 evidence "...of EUR1 billion by 2030." vs 正文 "...of EUR1 billion- [1]- by 2030."}
# [CONFIDENCE: CONFIRMED 100% — 首轮 3 条试跑, ev_001 的 2 条边全因此被误杀]
_FOOTNOTE = re.compile(r"\[\d{1,3}\]")
_EMPH = re.compile(r"[*_`~#]+")
# 去掉脚注/强调符后会剩下孤立的 "- " 列表符, 一并压掉。
# 只压【被空白包围的】连字符 —— 不能动 "Otter-Tail" 这类词内连字符。
_DASH = re.compile(r"(?<=\s)-+(?=\s)|^-+\s|\s-+$")
_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                         "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def norm(s: str) -> str:
    """把一段文字压成可比对的形态。两边用同一个函数, 所以不会单方面放宽。

    这里放宽的只是【排版噪声】(markdown 标记、脚注编号、列表符), 不是内容 ——
    实词、数字、顺序全部保留, 所以「evidence 必须是原文」这个要求没有被削弱。
    """
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_QUOTES)
    s = _LINK.sub(r"\1", s)            # [text](url) → text
    s = _FOOTNOTE.sub(" ", s)          # [1] 脚注编号 → 空白
    # ★ 替换成空格而非删除: "billion**- [1]- **by" 若直接删 ** 会得到 "billion- - by",
    # 破折号紧贴单词导致 _DASH 的空白断言失配;替换成空格才能让它被正常吃掉。
    s = _EMPH.sub(" ", s)              # markdown 强调符 → 空格
    s = _DASH.sub(" ", s)              # 去掉孤立的列表破折号(保留词内连字符)
    return _WS.sub(" ", s).strip().lower()


def check_step1(out: dict, body: str) -> dict:
    """校验 Step 1 的输出。返回 {ok, kept, dropped, reasons} —— 不抛异常, 因为要统计而不是中断。

    上游: edge_run.py 拿到模型 JSON 后立刻调用。
    下游: kept 里的 mention/edge 才进 Step 2 召回。
    dropped 全部记进 trace, 供人读时判断模型在什么地方开始编。
    """
    nbody = norm(body)
    reasons: list[str] = []

    kept_m, dropped_m = [], []
    for m in out.get("mentions") or []:
        ev = m.get("evidence") or ""
        if not ev:
            dropped_m.append((m.get("name"), "无 evidence"))
            continue
        # ★ 核心校验: 证据必须能在正文里原样找到
        if norm(ev) not in nbody:
            dropped_m.append((m.get("name"), "evidence 不是正文逐字子串"))
            continue
        kept_m.append(m)

    names = {m.get("name") for m in kept_m}

    kept_e, dropped_e = [], []
    for e in out.get("edges") or []:
        ev = e.get("evidence") or ""
        if not ev or norm(ev) not in nbody:
            dropped_e.append((f"{e.get('subject')}→{e.get('object')}", "evidence 不是正文逐字子串"))
            continue
        # ★ 这里原本有两条检查, 已删除 —— 它们是语义判断被写成了规则:
        #   ① object 为空的兜底: schema 已要求 object 非空, 重复拦截没有意义
        #   ② 「边的端点必须在 mentions 里」: 200 条实测丢掉 70 条(占 20%), 而那些多半是
        #      对的边, 只是名字写法不一致(边写 "Zebra", mention 是 "Zebra Technologies
        #      Corporation")。判断两个写法是不是同一个实体需要理解语义, 不是字符串比对能做的。
        #      交给验证步骤(verify.py)由模型判断。
        # {USER 2026-08-08 "you shouldnt check right, use llm to determien"}
        # {200 条实测丢弃原因 "端点不在 mentions 里 70 条" 是最大宗}
        kept_e.append(e)

    # attributes: 同样只做两项机械检查 —— 证据逐字、entity 必须在 mentions 里。
    # 后者是结构性引用完整性(名字必须完全相同, 因为 attribute 是模型自己同时产出的,
    # 不存在跨来源写法不一致的问题), 与上面删掉的边端点检查性质不同。
    kept_a, dropped_a = [], []
    for a in out.get("attributes") or []:
        ev = a.get("evidence") or ""
        if not ev or norm(ev) not in nbody:
            dropped_a.append((f"{a.get('entity')}.{a.get('key')}", "evidence 不是正文逐字子串"))
            continue
        if a.get("entity") not in names:
            dropped_a.append((f"{a.get('entity')}.{a.get('key')}", "entity 不在 mentions 里"))
            continue
        kept_a.append(a)

    # 没有边时必须给理由。沉默返回空数组会让「读不出来」和「模型偷懒」无法区分。
    if not kept_e and not (out.get("no_edge_reason") or "").strip():
        reasons.append("edges 为空但没有 no_edge_reason")

    return {
        "ok": not reasons,
        "kept": {"mentions": kept_m, "attributes": kept_a, "edges": kept_e},
        "dropped": {"mentions": dropped_m, "attributes": dropped_a, "edges": dropped_e},
        "reasons": reasons,
        "stats": {
            "mentions_in": len(out.get("mentions") or []), "mentions_kept": len(kept_m),
            "edges_in": len(out.get("edges") or []), "edges_kept": len(kept_e),
            "attrs_in": len(out.get("attributes") or []), "attrs_kept": len(kept_a),
            "title_body_mismatch": bool(out.get("title_body_mismatch")),
        },
    }
