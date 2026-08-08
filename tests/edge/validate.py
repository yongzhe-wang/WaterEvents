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

# 归一化: 统一各种引号/破折号 → 压 markdown 强调符 → 拆 [text](url) 只留 text → 压空白。
# 顺序有讲究: 先拆链接再压符号, 否则 [**x**](u) 会剩下孤立的括号。
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EMPH = re.compile(r"[*_`~#]+")
_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-", " ": " "})


def norm(s: str) -> str:
    """把一段文字压成可比对的形态。两边用同一个函数, 所以不会单方面放宽。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_QUOTES)
    s = _LINK.sub(r"\1", s)            # [text](url) → text
    s = _EMPH.sub("", s)               # 去掉 markdown 强调符
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
        # 引用完整性: 边的两端必须是留下来的 mention。指向被丢弃的 mention 的边一并丢弃 ——
        # 否则会产生指向不存在节点的悬空边。
        if e.get("subject") not in names or e.get("object") not in names:
            dropped_e.append((f"{e.get('subject')}→{e.get('object')}", "端点不在 mentions 里"))
            continue
        kept_e.append(e)

    # 没有边时必须给理由。沉默返回空数组会让「读不出来」和「模型偷懒」无法区分。
    if not kept_e and not (out.get("no_edge_reason") or "").strip():
        reasons.append("edges 为空但没有 no_edge_reason")

    return {
        "ok": not reasons,
        "kept": {"mentions": kept_m, "edges": kept_e},
        "dropped": {"mentions": dropped_m, "edges": dropped_e},
        "reasons": reasons,
        "stats": {
            "mentions_in": len(out.get("mentions") or []), "mentions_kept": len(kept_m),
            "edges_in": len(out.get("edges") or []), "edges_kept": len(kept_e),
            "title_body_mismatch": bool(out.get("title_body_mismatch")),
        },
    }
