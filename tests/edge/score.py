r"""tests/edge/score.py — 读 edge_run 的产出,算出**不需要人工标注**的那部分指标。

用一句话讲完: 把 edge_run 落盘的 out.json 全部读进来,算五组数 —— 交易所标记捕获率、
确定度分布、predicate 复用度、分层产边率对比、丢弃原因分布 —— 每一组都能自动算,
因为它们的判据要么是正文里的客观锚点,要么是输出自身的结构性质。

## 为什么这五组数不需要 ground truth

按 {USER 2026-07-23 "dont rely on ground truth read the output yourself"} 不建标注集。
但下面五件事都能不靠标注判定:

1. **交易所标记捕获率** —— 正文里写着 `(NYSE: WLY)`,这是客观事实。模型的 mention 有没有
   把它记进 exchange_tag,是可以直接对照的。这是唯一一个有「正确答案」的指标。
2. **确定度分布** —— 不判对错,判分布形态。WaterEvents 这条线产不出 level 0(那是 SEC
   结构化字段专属),正常应集中在 1-3;大量落在 4-5 说明文本弱或模型在回避判断。
3. **predicate 复用度** —— 只出现一次的 predicate 占比高 = 模型在复述句子而不是给可聚合
   的关系类型。这是纯结构性质,不需要知道哪条边是对的。
4. **分层产边率对比** —— expect_no_edge 层的产边率**必须**显著低于 press_release 层。
   如果两者接近,说明抽取器在硬凑 —— 这个判据不需要知道任何一条边对不对,只看相对关系。
5. **丢弃原因分布** —— 哪一类闸门在拦东西、拦了多少。它告诉我们下一轮该改 prompt 的哪里。

## 什么仍然只能人读

关系本身对不对、实体消歧对不对、私营机构有没有被错误合并 —— 这些没有客观锚点,
只能读 trace.txt。本脚本**不假装**能评这些。

## 跑法

    EDGE_OUT=/tmp/edge_out EDGE_DATASET=tests/datasets/edge_200 python3 tests/edge/score.py
"""
from __future__ import annotations

import collections
import glob
import json
import os
import re
import sys

_OUT = os.environ.get("EDGE_OUT", "/tmp/edge_out")
_DATASET = os.environ.get("EDGE_DATASET",
                          os.path.join(os.path.dirname(__file__), "..", "datasets", "edge_200"))

# 从 "(NYSE: WLY)" 里取出 ticker 部分。允许多 ticker: "(TSX: CCO; NYSE: CCJ)" → CCO, CCJ
_TICKER = re.compile(r"[:\s]\s*([A-Z][A-Z0-9.\-]{0,9})\b")


def _tickers(tag: str) -> set[str]:
    """从交易所标记原文里抽出 ticker 集合。

    整块保留到现在才解析, 是因为形态太多: "(NYSE: MOG.A and MOG.B)" 两个、
    "(TSX: CCO; NYSE: CCJ)" 跨交易所两个、"(NASDAQ: MRCY, www.mrcy.com)" 带网址。
    统一抽成集合后做包含判断, 不要求顺序和数量完全一致。
    """
    # 去掉交易所名本身, 否则 NYSE/NASDAQ 会被当成 ticker
    body = re.sub(r"(?i)\b(NYSE American|NYSE|NASDAQ|TSXV|TSX|LSE|SIX|Euronext|ASX|Paris|London)\b", " ", tag)
    return {m.group(1) for m in _TICKER.finditer(" " + body)} - {"AND", "WWW"}


def main() -> int:
    """读 out.json + 原始样本 → 打五组指标。只读不写。

    上游: edge_run.py 的输出目录。下游: 人看这张表决定下一轮改什么。
    """
    outs = {}
    for f in sorted(glob.glob(os.path.join(os.path.abspath(_OUT), "*", "out.json"))):
        d = json.load(open(f))
        outs[d["id"]] = d
    if not outs:
        print(f"没有结果: {_OUT}", file=sys.stderr)
        return 2
    recs = {}
    for f in sorted(glob.glob(os.path.join(os.path.abspath(_DATASET), "ev_*.json"))):
        r = json.load(open(f))
        recs[r["id"]] = r
    print(f"读到 {len(outs)} 条结果\n")

    # ── ① 交易所标记捕获率 —— 唯一有客观正确答案的指标 ──
    n_have, n_caught, n_exact = 0, 0, 0
    for rid, o in outs.items():
        tags = (recs.get(rid, {}).get("meta") or {}).get("exchange_tags") or []
        if not tags:
            continue
        n_have += 1
        want = set().union(*(_tickers(t) for t in tags)) if tags else set()
        got = set()
        for m in o["kept"]["mentions"]:
            if m.get("exchange_tag"):
                got |= _tickers(m["exchange_tag"])
        if got:
            n_caught += 1
        if want and want & got:
            n_exact += 1
    print("════════ ① 交易所标记捕获(唯一有客观答案的指标) ════════")
    print(f"  正文含标记的事件      {n_have}")
    print(f"  模型记下了标记        {n_caught} ({100 * n_caught // max(n_have, 1)}%)")
    print(f"  且 ticker 对得上      {n_exact} ({100 * n_exact // max(n_have, 1)}%)")
    print("  注: 正文 ticker 与 companies.ticker 可能不同(WLY vs WLYB 是 ADR),")
    print("      所以这里比的是「模型抄的标记」与「正文里的标记」, 不是与 companies.ticker")

    # ── ② 确定度分布 ──
    lv = collections.Counter()
    for o in outs.values():
        for e in o["kept"]["edges"]:
            lv[e["level"] if e.get("level") is not None else -1] += 1
    tot = sum(lv.values())
    print(f"\n════════ ② 确定度分布(边 {tot} 条) ════════")
    for k in sorted(lv):
        tag = "  ★ 模型未给字段 = prompt 缺陷, 不是模型不确定" if k < 0 else ""
        print(f"  level {k if k >= 0 else '缺失':>5}  {lv[k]:5d}  {'█' * (30 * lv[k] // max(tot, 1))}{tag}")

    # ── ③ predicate 复用度 ──
    pr = collections.Counter()
    for o in outs.values():
        for e in o["kept"]["edges"]:
            pr[(e.get("predicate") or "?").strip().lower()] += 1
    once = sum(1 for _, c in pr.items() if c == 1)
    print(f"\n════════ ③ predicate 复用度 ════════")
    print(f"  {len(pr)} 种 / {sum(pr.values())} 条边   只出现一次的占 {100 * once // max(len(pr), 1)}%")
    print("  高占比 = 模型在复述句子而不是给可聚合的关系类型")
    print("  最常见: " + " · ".join(f"{k}×{c}" for k, c in pr.most_common(10)))

    # ── ④ 分层产边率 —— 判「有没有硬凑」的关键对比 ──
    print(f"\n════════ ④ 分层产边率(expect_no_edge 必须显著低) ════════")
    print(f"  {'stratum':18s} {'条数':>5s} {'产边总数':>9s} {'每条均边':>9s} {'零边比例':>9s}")
    by = collections.defaultdict(lambda: [0, 0, 0])
    for o in outs.values():
        b = by[o["stratum"]]
        b[0] += 1
        b[1] += len(o["kept"]["edges"])
        b[2] += int(len(o["kept"]["edges"]) == 0)
    for st, (n, e, z) in sorted(by.items()):
        print(f"  {st:18s} {n:5d} {e:9d} {e / max(n, 1):9.2f} {100 * z // max(n, 1):8d}%")

    # ── ⑤ 丢弃原因 —— 告诉我们下一轮改 prompt 的哪里 ──
    why = collections.Counter()
    for o in outs.values():
        for _, w in o["dropped"]["mentions"]:
            why[f"mention: {w}"] += 1
        for _, w in o["dropped"]["edges"]:
            why[f"edge:    {w}"] += 1
    print(f"\n════════ ⑤ 丢弃原因(闸门拦了什么) ════════")
    for k, v in why.most_common():
        print(f"  {v:5d}  {k}")

    print(f"\n  仍需人读: 关系对不对、实体消歧对不对 —— 无客观锚点, 见 {_OUT}/<id>/trace.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
