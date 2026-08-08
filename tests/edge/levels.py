"""tests/edge/levels.py — 节点与边的确定度分级(0–5)。node 和 edge 共用同一把尺子。

用一句话讲完: 把「这条东西有多确定」从一个二元的 UNSURE 标志改成 **0–5 的等级**, 0 是
SEC 结构化字段那种零推断的确定, 5 是无法确定 —— 于是同一张图里可以同时容纳「Form 4 报的
持仓」和「新闻稿里读出来的合作关系」, 而查询方能自己选要多确定的。

## 为什么必须分级而不是二元

water-graph 现有的边全部来自 SEC 结构化字段: code=S 直接映射成 transacts, 零推断, 不会错。
WaterEvents 的边是从自然语言读出来的, 对错是概率性的。两者混进同一张 edge_claim 而不加区分,
会把整张图的可信度拉到低的那一档 —— 但**把它们分开存也不对**, 那样就查不到跨来源的关系链。
分级让它们能共存: 要严谨的查 level<=1, 要覆盖面的查 level<=4。

## 为什么是 0 最高

跟 water-graph 现有的 edge_claim.tier 对齐 —— SEC 那条线现在就是 tier=0。
{USER 2026-08-08 "0 1 2 3 4 5, 0 is highest as definite like sec"}

## 一条数据的 level 取两部分的**较大值**(即较差的那个)

一条边的确定度 = max(关系本身的确定度, 两端实体的确定度)。
理由: 关系读得再准, 如果主语指向了错误的实体, 这条边照样是错的 —— 短板决定成色。
"""
from __future__ import annotations

# ── 等级定义。三处引用同一套: Step 1 抽取自评、Step 3 消歧自评、落库时决定去向。 ──
LEVELS: dict[int, dict[str, str]] = {
    0: {
        "name": "structural",
        "desc": "结构化字段直接映射, 零推断",
        "example": "SEC Form 4 的 transactionCode=S → transacts;13F 的 CUSIP 行 → 持仓",
        "note": "WaterEvents 这条线**产不出 level 0** —— 它读的是自然语言。保留这一级是为了和"
                "water-graph 已有的 SEC 边同尺, 让两者能在一张图里比较。",
    },
    1: {
        "name": "explicit_anchored",
        "desc": "文本逐字写明, 且实体带唯一标识",
        "example": '"Wiley (NYSE: WLY) today announced the launch of Advanced Brain" '
                   "—— 关系明示, 主语有交易所标记可直接锚定",
    },
    2: {
        "name": "explicit_by_name",
        "desc": "文本逐字写明关系, 但实体只能靠名字匹配(无 ticker/CIK)",
        "example": '"DNB Baltic Invest AB has entered into an agreement to divest its entire '
                   'shareholding in Luminor Holding AS" —— 关系毫无歧义, 但两个实体都不是 SEC 申报人',
    },
    3: {
        "name": "needs_coreference",
        "desc": "关系明确, 但主语/宾语是简称或代词, 要跨句判断指向哪个实体",
        "example": '"Luminor was established in 2017 through the merger of..." —— "Luminor" 在同一段里'
                   "既可能指 Luminor Holding AS 也可能指 Luminor Bank AS",
    },
    4: {
        "name": "inferred",
        "desc": "关系不是直接陈述, 需要从上下文推出",
        "example": '"said President and CEO Chuck MacFarlane" → 推出 employs 关系。'
                   "文本从没说「雇佣」, 是从职务称谓推的",
    },
    5: {
        "name": "unresolved",
        "desc": "实体或关系任一无法确定 —— 不进主图, 挂起人工",
        "example": "候选里有两条同名不同 cik 的实体, 上下文不足以区分;或候选超过上限被判 AMBIGUOUS",
    },
}

# 落库去向。这是唯一按 level 分流的地方 —— 上游只负责给出诚实的 level, 不决定去留。
WRITE_MAX = 4          # <= 这个 level 才写进 edge_claim
PENDING_AT = 5         # == 这个 level 进 entity_resolution_pending, 等人看

# Step 1 / Step 3 的 schema 复用这个片段, 保证两处的字段名和取值范围一致。
LEVEL_SCHEMA = {
    "type": "integer", "minimum": 0, "maximum": 5,
    "description": "确定度 0(结构化零推断)~5(无法确定)",
}


def prompt_block() -> str:
    """把等级定义渲染成 prompt 里的一段。抽取和消歧两个 prompt 共用, 避免两处描述漂移。

    上游: prompts.py 的 STEP1_PROMPT / STEP3_PROMPT。
    下游: 模型据此自评 —— 所以描述必须是模型能判断的**可观察特征**(文本有没有明说、
    实体有没有标识), 而不是"你有多自信"这种主观量。
    """
    lines = ["确定度等级(每条 mention 和 edge 都要给):"]
    for k in sorted(LEVELS):
        v = LEVELS[k]
        lines.append(f"  {k} = {v['desc']}")
        lines.append(f"      例: {v['example']}")
    lines.append("")
    lines.append("判定要点:")
    lines.append("  · 看的是**可观察特征**(文本有没有明说、实体有没有唯一标识), 不是你的主观信心")
    lines.append("  · 一条边的等级取 max(关系本身的等级, 两端实体的等级) —— 短板决定成色")
    lines.append("  · 宁可给高一级(更不确定)也不要给低了。给低了会让错误混进严谨查询的结果里")
    return "\n".join(lines)


def combine(*levels: int | None) -> int:
    """取较差(较大)的那一级。用于边的等级 = max(关系等级, 主体等级, 客体等级)。

    None 视为 5 —— 缺失的等级不能当成"没问题", 那正是最该挂起的情况。
    """
    vals = [5 if x is None else int(x) for x in levels]
    return max(vals) if vals else 5
