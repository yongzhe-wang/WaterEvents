"""tests/edge/iters.py — 节点与边的确定度:三档,数据库字段名 `iter`。

用一句话讲完: `iter` 回答一个问题 —— **这条东西是照抄的还是推出来的**。
iter0 = 结构化字段直接映射(SEC 那条线),iter1 = 文本白纸黑字写着,iter2 = 从上下文推出。
node 和 edge 共用同一把尺子,所以同一张图里能同时容纳「Form 4 报的持仓」和「新闻稿里读出的合作」,
而查询方能自己选:要严谨的查 iter<=1,要覆盖面的连 iter2 一起要。

## 为什么是三档不是六档

首版分了 0-5 六档(还有「需要指代消解」「无法确定」等)。200 条实测证明那些粒度是我造出来的:
{2026-08-08 200 条跑批 边 223 条 "level 0=7 · 1=159 · 2=23 · 3=1 · 4=33 · 5=0"}
level 3 只用了 1 次、level 5 一次都没用,而 level 0 被误用了 7 次(WaterEvents 读的是自然语言,
产不出结构化零推断)。模型真正能稳定区分的只有「明说」和「推断」两类。
[CONFIDENCE: CONFIRMED 100% — 分布来自 200 条实跑的边级统计]

## 为什么没有「不确定」这一档

说不准就**不产这条边**,而不是产一条标着「我不确定」的边混进图里。
一个标记为可疑的东西写进事实层,下游每一次查询都要重新决定要不要信它 —— 那个成本会一直付下去。
不产边的信号走 `no_edge_reason`,消歧分不清的信号走挂起队列,两者都不占 iter 的档位。

## 为什么 0 最高

跟 water-graph 现有的 edge_claim.tier 对齐 —— SEC 那条线现在就是 tier=0。
{USER 2026-08-08 "0 1 2 3 4 5, 0 is highest as definite like sec" → 后简化为三档}

## 一条边的 iter 取两部分的较大值(较差的那个)

边的 iter = max(关系本身的 iter, 两端实体的 iter)。
关系读得再准,主语指向了错误的实体,这条边照样是错的 —— 短板决定成色。
"""
from __future__ import annotations

ITERS: dict[int, dict[str, str]] = {
    0: {
        "name": "structural",
        "desc": "结构化字段直接映射,零推断",
        "example": "SEC Form 4 的 transactionCode=S → transacts;13F 的 CUSIP 行 → 持仓",
        "note": "WaterEvents 这条线**产不出 iter0** —— 它读的是自然语言。保留这一档是为了和"
                "water-graph 已有的 SEC 边同尺,让两者能在一张图里比较。",
    },
    1: {
        "name": "stated",
        "desc": "文本白纸黑字写着,照抄即可",
        "example": '"a member of the S&P SmallCap 600 Index" → is_member_of;'
                   '"has entered into an agreement to divest its entire shareholding in Luminor Holding AS"',
    },
    2: {
        "name": "inferred",
        "desc": "文本没直说,是从上下文推出来的",
        "example": '"said President and CEO Chuck MacFarlane" → employs。'
                   "原文从没出现「雇佣」,是从职务称谓推的 —— 推断通常是对的,但它是推断",
    },
}

# 落库去向。上游只负责诚实给 iter,不决定去留;分流只在这里发生。
WRITE_MAX = 2      # 三档都写进 edge_claim —— 因为「不确定」的已经在上游就不产边了

ITER_SCHEMA = {
    "type": "integer", "minimum": 0, "maximum": 2,
    "description": "确定度 0=结构化零推断 / 1=文本明说 / 2=从上下文推断",
}


def prompt_block() -> str:
    """渲染成 prompt 里的一段。抽取和消歧共用,避免两处描述漂移。

    上游: prompts.py。
    下游: 模型据此自评 —— 所以描述必须是**可观察特征**(文本有没有明说),
    而不是"你有多自信"这种主观量。
    """
    lines = ["iter(确定度)—— 每条 mention 和 edge 都要给:"]
    for k in sorted(ITERS):
        v = ITERS[k]
        lines.append(f"  {k} = {v['desc']}")
        lines.append(f"      例: {v['example']}")
    lines.append("")
    lines.append("判定只看一件事: **原文有没有直接说这件事**。")
    lines.append("  · 原文写了 → iter1。原文没写、是你从职务/上下文推出来的 → iter2")
    lines.append("  · iter0 不要用 —— 它是 SEC 结构化字段专属,读文字产不出")
    lines.append("  · 连推都推不确定 → **不要产这条边**,而不是标一个高 iter")
    lines.append("  · 一条边的 iter 取 max(关系, 主体, 客体) —— 短板决定成色")
    return "\n".join(lines)


def combine(*iters: int | None) -> int:
    """取较差(较大)的那一档。边的 iter = max(关系, 主体, 客体)。

    None 视为 2 —— 缺失的等级按最弱处理,不能当成"没问题"。
    """
    vals = [2 if x is None else int(x) for x in iters]
    return max(vals) if vals else 2
