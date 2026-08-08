r"""tests/edge/sample.py — 从生产库分层抽 200 个事件,做成边抽取实验的数据集。

用一句话讲完: 连 WaterEvents 生产库, 按「这条事件能不能验证、难在哪」分四层抽 200 个已有正文的事件 →
每个事件落一个 ev_*.json(公司信息 + 标题 + 类型 + 日期 + event_documents.md 全文)→ 供 edge_run.py
把它们喂给 LLM 抽节点和边。分层的**唯一目的**是让评测能分成「机器能自动判对错的」和「只能人读的」两半。

## 为什么第一层要挑「正文里带交易所标记」的

`(NYSE: HCA)` 这种标记写在正文里时, 这个 mention 的正确答案是**确定的** —— ticker 能直接对到
water-graph 的 node_company_profile.primary_symbol → cik。于是这 100 条不需要任何人工标注就能自动算
准确率, 这是调 prompt 时唯一的客观标尺。
{PSQL 2026-08-08 press_release 正文 10,138 条中 5,684 条含交易所标记 = 56.1%, 平均每份 1.33 个}
[CONFIDENCE: CONFIRMED 100% — 正则 '\((NYSE|NASDAQ|TSX|LSE|SIX|Euronext)[^)]{0,12}:' 全表统计]

但它只覆盖**简单案例** —— 带 ticker 的都是上市公司, 库里大概率已有。真正难的
`Luminor Holding AS` / `DNB Baltic Invest AB` / `OTP Bank Plc` 恰恰没有 ticker, 所以第二层专门抽
不带标记的, 那部分只能人读。两层缺一不可。

## 为什么不按人口比例抽

同 media_100 的理由: 数据集的作用是**覆盖要测的情况**, 不是描述总体。press_release 被显著过采样
(总体有正文的事件里它占 9,554/22,547 ≈ 42%, 这里占 80%), 因为边几乎只出现在它里面 —— 实测 22 条
美股标题里只有 3-4 条能读出明确的边, 而那几条全是 press_release。
{tests/datasets/media_100/README.md "数据集的作用是覆盖代码路径,不是描述总体"}

## 跑法

    WATEREVENTS_DB_DSN=<supavisor pooler dsn> \
    EDGE_SAMPLE_OUT=tests/datasets/edge_200 \
      python3 tests/edge/sample.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

# asyncpg 而非 psycopg2 —— 生产机上只装了它, 且项目统一走 Supavisor transaction pooler(6543),
# 那要求 statement_cache_size=0。跟着项目约定走, 不额外引入依赖。
# {backend/agent/event_agent/storage/events.py:89 "asyncpg.create_pool(_DSN, ..., statement_cache_size=0"}
# {ir-media-8 实测 "✓ asyncpg / ✗ psycopg2 / ✗ psycopg / ✗ pg8000"}
# [CONFIDENCE: CONFIRMED 100% — 在目标机 venv 里逐个 import 试过]
import asyncpg

# 生产库只读取样, 不写任何东西。DSN 没有默认值 —— 缺了就该当场炸, 不该退回某个猜的地址。
# {backend/agent/event_agent/storage/queue.py 注释 "NO DEFAULT — the DSN must come from the environment
#  or the process must refuse to start"}
_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")
_OUT = os.environ.get("EDGE_SAMPLE_OUT", os.path.join(os.path.dirname(__file__), "..", "datasets", "edge_200"))

# 交易所标记的识别式。必须匹配到**右括号**, 因为冒号后面的 ticker 才是这一层存在的意义 ——
# 只匹配到冒号会把 "(NYSE: HCA)" 截成 "(NYSE)", 锚点就失去了 ground truth 的作用。
# 真实形态五花八门, 都要能整块捕获:
#   (NYSE: HCA) / (NYSE: MOG.A and MOG.B) / (TSX: CCO; NYSE: CCJ) / (NASDAQ: MRCY, www.mrcy.com)
# {PSQL 2026-08-08 抽样输出上述四种形态}
# {首版 bug 实测 "(Euronext Paris)" / "(Nasdaq)" / "(NYSE)" —— ticker 被 rstrip 掉了}
# [CONFIDENCE: CONFIRMED 100% — 首次抽样输出的 exchange_tags 全部缺 ticker, 已定位到 rstrip(":")]
# 交易所名后允许少量修饰(如 "Euronext Paris"), 然后必须有冒号 + 至少一个字符的 ticker。
_TAG_RE = re.compile(
    r"\((?:NYSE American|NYSE|NASDAQ|TSXV|TSX|LSE|SIX|Euronext|ASX)[^):]{0,15}:\s*[^)]{1,40}\)", re.I)

# 四个层。n 是目标条数, where 是额外的 SQL 条件。
# 合计 200 = 100 自动可验证 + 60 人读难案例 + 25 类型泛化 + 15 负样本。
_STRATA = [
    # ① 自动可验证: 正文带交易所标记 → ticker 就是免费 ground truth
    ("auto_verifiable", 100,
     "e.event_type = 'press_release' AND d.md ~* '\\((NYSE|NASDAQ|TSX|LSE|SIX|Euronext)[^)]{0,12}:'"),
    # ② 人读难案例: 不带标记的 press_release —— 私营机构/子公司/外国实体全在这层
    ("hard_no_tag", 60,
     "e.event_type = 'press_release' AND d.md !~* '\\((NYSE|NASDAQ|TSX|LSE|SIX|Euronext)[^)]{0,12}:'"),
    # ③ 类型泛化: 非 press_release 也可能藏公告, 验证抽取器不会只在一种文体上工作
    ("other_types", 25,
     "e.event_type IN ('earnings','filing','conference','dividend','shareholder_meeting')"),
    # ④ 负样本: 纯日程类标题(季报/年会/网播), 预期读不出边。
    #    没有负样本的评测集会养出一个「什么都敢答」的抽取器 —— 这是最容易犯的错。
    ("expect_no_edge", 15,
     "e.event_type IN ('webcast','presentation') AND length(e.title) < 60"),
]

# 抽样确定性: 按 md5(id::text) 排序而非随机种子, 同一个库跑两次得到同一批 200 条。
# {tests/datasets/media_100/README.md "抽样是确定性的(md5(id::text) 排序,非随机种子)"}
_SQL = """
select e.id::text            as event_id,
       e.title, e.event_type, e.event_date, e.source_url,
       c.ticker, c.ir_url,
       d.url                 as doc_url,
       d.n_chars,
       d.md
  from waterevents.events e
  join waterevents.event_documents d on d.event_id = e.id
  join waterevents.companies c       on c.id = e.company_id
 where d.n_chars between 800 and 60000        -- 太短没内容可读, 太长塞不进 context 且多半是列表页
   and {where}
 order by md5(e.id::text)
 limit $1
"""


def _date_precision(s: str | None) -> str:
    """把 events.event_date 的文本粒度判出来, 供 edge_claim.valid_precision 用。

    WHY 不强行补成某一天: events.event_date 是 text, 生产库里 YYYY / YYYY-MM / YYYY-Q1 / YYYY-MM-DD
    混存(建表注释明说 "kept as text: the model records whatever granularity the page shows")。
    water-graph 的 edge_claim 正好有 valid_at + valid_precision 两列能原样接住 ——
    把 "2023-Q1" 补成 "2023-01-01" 是在伪造精度。
    {waterevents.events 建表注释 "event_date text -- kept as text: ... (YYYY / YYYY-MM / YYYY-Q1 / YYYY-MM-DD)"}
    [CONFIDENCE: CONFIRMED 100% — media_100 样本里实际出现 "2023-Q1" 与 "2026-07-30" 两种粒度]
    """
    if not s:
        return "unknown"
    s = s.strip()
    if re.fullmatch(r"\d{4}", s):                    return "year"
    if re.fullmatch(r"\d{4}-Q[1-4]", s, re.I):       return "quarter"
    if re.fullmatch(r"\d{4}-\d{2}", s):              return "month"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s):      return "day"
    return "unknown"


def _tags(md: str) -> list[str]:
    """抽出正文里的交易所标记原文, 作为该样本的自动评测锚点。

    整块保留不做解析 —— 解析成 ticker 是评测脚本的事, 抽样阶段不加工。
    保留原文是为了让 "(TSX: CCO; NYSE: CCJ)" 这种双重上市的信息不被提前压扁成单个 ticker。
    """
    return sorted({m.group(0) for m in _TAG_RE.finditer(md or "")})[:10]


async def main() -> int:
    """连库 → 四层各抽 n 条 → 落盘 ev_*.json + _manifest.json。

    上游: 无(手动跑)。下游: tests/edge/edge_run.py 读这个目录。
    只读生产库, 不写任何东西。
    """
    if not _DSN:
        # 铁律: 缺凭证当场炸, 不猜一个默认地址。
        print("WATEREVENTS_DB_DSN 未设置 —— 指向 Supabase Supavisor pooler(6543)", file=sys.stderr)
        return 2

    out = os.path.abspath(_OUT)
    os.makedirs(out, exist_ok=True)

    # statement_cache_size=0 是 transaction-mode pooler 的硬性要求 —— 同 storage/events.py
    conn = await asyncpg.connect(_DSN, statement_cache_size=0)
    picked: list[dict] = []
    seen: set[str] = set()                      # 跨层去重: 一个 event 只能进一层

    try:
        for stratum, n, where in _STRATA:
            rows = await conn.fetch(_SQL.format(where=where), n * 3)   # 多取 3 倍, 去重后再截断
            got = 0
            for r in rows:
                if got >= n:
                    break
                if r["event_id"] in seen:
                    continue
                seen.add(r["event_id"])
                picked.append({**dict(r), "_stratum": stratum})
                got += 1
            print(f"  {stratum:16s} 目标 {n:3d}  实得 {got:3d}", flush=True)
    finally:
        await conn.close()

    strata_count: dict[str, int] = {}
    for i, r in enumerate(picked, 1):
        st = r["_stratum"]
        strata_count[st] = strata_count.get(st, 0) + 1
        rid = f"ev_{i:03d}_{st}"
        rec = {
            "id": rid,
            # ── Step 1 的 LLM 输入(整包喂进去) ──
            "input": {
                "company": {"ticker": r["ticker"], "ir_url": r["ir_url"]},
                "title": r["title"],
                "type": r["event_type"],
                "date": r["event_date"],
                "date_precision": _date_precision(r["event_date"]),
                "body": r["md"],                       # event_documents.md 全文, 不截断 —— 截断策略交给 runner
            },
            # ── 仅供分层与评测, runner 抽取时忽略 ──
            "meta": {
                "stratum": st,
                "db_event_id": r["event_id"],
                "n_chars": r["n_chars"],
                "doc_url": r["doc_url"],
                "source_url": r["source_url"],
                # 自动评测锚点: 正文里出现的交易所标记原文。auto_verifiable 层必非空。
                "exchange_tags": _tags(r["md"]),
            },
        }
        with open(os.path.join(out, f"{rid}.json"), "w") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)

    manifest = {
        "name": "edge_200",
        "n": len(picked),
        "purpose": "LLM 抽节点与边的实验集 —— 分层目的是把评测切成「机器能自动判对错」和「只能人读」两半",
        "strata": strata_count,
        "auto_verifiable_note": "该层正文含 (NYSE:/NASDAQ:…) 标记, ticker 即 ground truth, 无需人工标注",
        "no_ground_truth": "其余层不含标注 —— 按 {USER 2026-07-23 'dont rely on ground truth read the output yourself'} 人读判质量",
        "deterministic": "order by md5(event_id::text), 同库重跑得到同一批",
    }
    with open(os.path.join(out, "_manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    print(f"\n落盘 {len(picked)} 条 → {out}")
    print(f"  分层: {strata_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
