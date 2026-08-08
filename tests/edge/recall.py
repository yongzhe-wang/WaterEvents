"""tests/edge/recall.py — Step 2: 从 water-graph 召回候选实体, 分块交给 LLM。

用一句话讲完: 拿 Step 1 给的 search_keys 去 water-graph 的 node_entity 做 ILIKE 召回 → 先用多个 key 的
**AND 交集**天然收窄 → 空了退化到召回最少的那个 key → 还是太多就判 AMBIGUOUS 挂起 → 剩下的按 K 条一块
切开。**这一层不做任何判断**: 不排序、不打分、不设相似度阈值 —— 那些全是要调的东西, 交给 Step 3 的 LLM。

## 为什么要 blocking 而不是「宽召回全给 LLM」

因为会塞爆 context。实测 node_entity 上的 ILIKE 召回量:
{PSQL 2026-08-08  Luminor 0 · Nordea 1 · Rubrik 1 · DNB 3 · Wiley 11 · Advanced 19 · Blackstone 72
                  · Bank 246 · Holdings 1,304 · Capital 1,474}
[CONFIDENCE: CONFIRMED 100% — 对 node_entity 逐词 count(*) ILIKE 实测]

分布是极端长尾: 九成 mention 落在 0–20 条(零块或一块搞定), 少数通用词炸到上千条。
所以真正要解决的不是「怎么分块」, 而是「先别让它炸」—— AND 交集干的就是这件事。

## AND 交集为什么能替代相似度排序

"Blackstone Capital Partners" 给出 keys ["Blackstone", "Capital"]:
  单独 "Blackstone" → 72 条, 单独 "Capital" → 1,474 条, 交集 → 远小于两者。
多个词同时命中的必然更相关 —— 这正是相似度排序想做的事, 但**不需要选相似度函数、不需要定阈值**。

## 两个参数, 都是算出来的不是调出来的

  K     = 50   每块候选数。候选每条约 40 token(名字+kind+cik+边摘要), 单次调用给候选的预算约 2,000 token
  B_MAX = 4    每个 mention 最多几块。即最多看 200 个候选, 超过就判 AMBIGUOUS —— 这是成本上限, 不是质量旋钮
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# asyncpg 而非 psycopg2 —— 生产机上只装了它(ir-media-8 实测),且项目统一走这个驱动。
# {backend/agent/event_agent/storage/events.py:89 "asyncpg.create_pool(..., statement_cache_size=0"}
import asyncpg

# water-graph 的图谱库(不是 WaterEvents 事件库)—— 候选实体在这。
_GRAPH_DSN = os.environ.get("GRAPH_DSN", "")

K = int(os.environ.get("EDGE_BLOCK_K", "50"))            # 每块候选数
B_MAX = int(os.environ.get("EDGE_BLOCK_MAX", "4"))       # 每个 mention 最多几块

# 通用词黑名单。这些词几乎每家机构都有, 单独拿来检索必然召回上千条。
# prompt 里已经要求模型别给, 这里是第二道保险 —— 模型偶尔还是会给。
# {PSQL 2026-08-08 "Capital 1,474 · Holdings 1,304 · Bank 246"}
_TOO_GENERIC = {
    "capital", "holdings", "holding", "group", "bank", "partners", "partner", "fund", "funds",
    "inc", "ltd", "llc", "plc", "corp", "corporation", "company", "co", "trust", "management",
    "investments", "investment", "advisors", "advisers", "international", "global", "the",
}


@dataclass
class RecallResult:
    """一个 mention 的召回结果。

    blocks 为空且 ambiguous=False → 库里确实没有, Step 3 都不用调, 直接 NEW。
    ambiguous=True → 候选超过 K*B_MAX, 挂起人工, 不烧 LLM。
    """
    mention: str
    keys_used: list[str]
    n_candidates: int
    ambiguous: bool = False
    blocks: list[list[dict]] = field(default_factory=list)
    note: str = ""


def _clean_keys(keys: list[str]) -> list[str]:
    """滤掉太短和太通用的检索词。

    WHY 要滤: 模型偶尔会给 "Capital" 这种词, 实测单独召回 1,474 条 —— 直接把这个 mention 推进
    AMBIGUOUS, 白白挂起一个本来能判的实体。滤掉之后还剩别的 key 就仍然能查。
    长度 <2 的词(如 "AB" "AS")同理: 那是法人后缀不是识别词。
    """
    out = []
    for k in keys or []:
        k = (k or "").strip()
        if len(k) < 3:                          # "AB"/"AS"/"NV" 这类后缀, 不是识别词
            continue
        if k.lower() in _TOO_GENERIC:
            continue
        out.append(k)
    return out[:3]


async def _query(conn, keys: list[str], how: str) -> list[dict]:
    """按 keys 查 node_entity。how='and' 取交集, how='or' 取单键。

    带上 kind 和 cik 是给 Step 3 判断用的 —— 光看名字分不出两个同名的
    "Blackstone Family Tactical Opportunities"(cik 1960393 kind=unknown vs cik 1853361 kind=institution)。
    {PSQL 2026-08-08 node_entity 实测存在上述两条同名不同 cik 的记录}
    [CONFIDENCE: CONFIRMED 100% — name ilike 'Blackstone%' 输出]
    """
    if not keys:
        return []
    joiner = " and " if how == "and" else " or "
    # asyncpg 用 $1/$2 位置参数, 不是 %s
    where = joiner.join([f"n.name ilike ${i + 1}" for i in range(len(keys))])
    # limit 给到 K*B_MAX+1 —— 多取一条就能判断「是否超限」, 不必 count(*) 再查一遍
    rows = await conn.fetch(
        f"""select n.cik as entity_id, n.name, n.kind
              from node_entity n
             where {where}
             limit {K * B_MAX + 1}""",
        *[f"%{k}%" for k in keys],
    )
    return [dict(r) for r in rows]


async def recall_one(conn, mention: dict) -> RecallResult:
    """对一个 mention 做召回 → 分块。

    上游: Step 1 的 mentions[]。下游: Step 3 逐块判断。
    流程严格按 AND → 退化 → 超限判定 → 分块, 中间没有任何「哪个更像」的判断。
    """
    name = mention.get("name", "")
    keys = _clean_keys(mention.get("search_keys", []))
    if not keys:
        # 所有 key 都被滤掉了(全是通用词或太短)—— 退回用完整名字查一次, 总比不查好
        keys = [name] if len(name) >= 3 else []
    if not keys:
        return RecallResult(name, [], 0, note="no usable search key")

    # ① AND 交集: 多个词同时命中 → 天然收窄, 不需要相似度排序
    rows = await (_query(conn, keys, "and") if len(keys) > 1 else _query(conn, keys, "or"))

    # ② 交集为空 → 退化到「召回条数最少」的那个 key(最有区分度的那个)
    if not rows and len(keys) > 1:
        best, best_rows = None, None
        for k in keys:
            r = await _query(conn, [k], "or")
            if best_rows is None or len(r) < len(best_rows):
                best, best_rows = k, r
        rows, keys = (best_rows or []), [best or keys[0]]

    n = len(rows)

    # ③ 超过 K*B_MAX → 说明检索词太宽泛, 挂起人工, 不调 LLM(成本上限)
    if n > K * B_MAX:
        return RecallResult(name, keys, n, ambiguous=True,
                            note=f"候选 >{K * B_MAX}, 检索词过宽")

    # ④ 按 K 分块。n=0 时 blocks 为空 → 调用方直接判 NEW, 零次 LLM 调用
    blocks = [rows[i:i + K] for i in range(0, n, K)]
    return RecallResult(name, keys, n, blocks=blocks)


async def recall_event(mentions: list[dict]) -> list[RecallResult]:
    """一个事件的全部 mention 一次性召回(共用一个连接)。

    上游: edge_run.py 拿到 Step 1 输出后调用。
    下游: 每个 RecallResult 的 blocks 逐块喂 Step 3。
    """
    if not _GRAPH_DSN:
        raise RuntimeError("GRAPH_DSN 未设置 —— 指向 water-graph 图谱库")
    conn = await asyncpg.connect(_GRAPH_DSN, statement_cache_size=0)
    try:
        # 会话级放宽: 默认 2 分钟对 78,467 行的 ILIKE 够用, 但通用词会扫全表
        await conn.execute("set statement_timeout = '2min'")
        return [await recall_one(conn, m) for m in mentions]
    finally:
        await conn.close()
