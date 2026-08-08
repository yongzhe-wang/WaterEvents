r"""tests/edge/edge_run.py — 跑 Step 1(读文字出 mention 和 edge), 每条落一个可调试目录。

用一句话讲完: 读 edge_200 数据集的每个 ev_*.json → 拼 Step 1 prompt → QwenClient 批量并发打进 vLLM →
validate.py 做逐字证据校验 → 每个事件写一个 <id>/ 目录(trace.txt 人读 + out.json 机读 + prompt.txt 存证)
→ 最后打一张汇总表。**不写任何库**, 纯离线实验。

## 跟 media_run.py 一样, 没有 ground-truth oracle

除 auto_verifiable 层的 ticker 锚点外不含标注, 质量靠人读 trace 判断 ——
{USER 2026-07-23 "dont rely on ground truth read the output yourself ... structure the output so each page
as txt + full prompt + all the trace"}。所以每个事件都落完整 prompt 和模型原始输出, 而不只是解析后的结果。

## 长正文分块, 不截断

线上模型 Qwen2.5-14B-Instruct-AWQ 的 max_model_len = 32,768 token, 装不下长文档。
**但截断是在丢数据** —— 一份 60k 字的年报截到 28k, 后面那半的边就永远读不出来, 而且丢得静默。
所以改成分块跑, 每块单独抽, 结果合并去重。

分块参数直接复用项目现成的(handlers.py, 生产验证过), 不另造一套:
  _CHUNK_TARGET_CHARS = 18000   每块目标输入
  _CHUNK_OVERLAP      = 1500    向后重叠, 防止块边界把一句话/一个表切断

块数上限按真实长度分布定, 不是拍脑袋:
{PSQL 2026-08-08 event_documents n_chars 分位 "p50 2,727 · p75 6,814 · p90 17,811 ·
 p95 26,762 · p99 174,028 · max 3,771,299"}
按 18,000/块 → p95 只需 2 块, p99 需 10 块 → 上限取 12 块(约 216k 字符)覆盖到 p99 之外。
超过 12 块的不是"长文档"而是另外两类东西, 硬跑没有意义:
  ① 提取失败 —— md 里存的是 RTF 控制码而非正文
     {PSQL 实测 最长两条 "{\rtf1\adeflang1025\ansi\ansicpg1252..." 3,771,299 / 2,744,391 字符}
  ② SEC 10-K/10-Q 全文 —— 这类本就该走 FocusAlpha 的 item-splitter, 不进这条通用管道
     {tests/datasets/media_100/README.md "focusalpha-backend 已经把 SEC 做得好得多"}
     {PSQL 实测 "UNITED STATES SECURITIES AND EXCHANGE COMMISSION ... FORM 10-Q" 1,621,106 字符}
[CONFIDENCE: CONFIRMED 100% — 分位数与样本开头均为全表实测]

## 跑法

    cd /home/thebigsun/WaterEvents
    export $(sudo grep -E "^QWEN_" /etc/waterevents/fleet.env | xargs)
    PYTHONPATH=$PWD/backend EDGE_DATASET=tests/datasets/edge_200 EDGE_OUT=/tmp/edge_out \
      /home/thebigsun/venv/bin/python tests/edge/edge_run.py
"""
from __future__ import annotations

import asyncio
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from providers.qwen_llm import QwenClient                      # 复用项目的并发传输层, 不自己造轮子
from tests.edge.prompts import STEP1_PROMPT, STEP1_RETRY_PROMPT, STEP1_SCHEMA, _RETRY_MAX
from tests.edge.levels import PENDING_AT, WRITE_MAX, prompt_block
from tests.edge.validate import check_step1

_DATASET = os.environ.get("EDGE_DATASET",
                          os.path.join(os.path.dirname(__file__), "..", "datasets", "edge_200"))
_OUT = os.environ.get("EDGE_OUT", "/tmp/edge_out")
_LIMIT = int(os.environ.get("EDGE_LIMIT", "0"))                # >0 时只跑前 N 条, 用于快速试跑

# 分块参数 —— 与 handlers.py 对齐, 不另造一套
_CHUNK_CHARS = int(os.environ.get("EDGE_CHUNK_CHARS", "18000"))   # 每块目标输入
_CHUNK_OVERLAP = int(os.environ.get("EDGE_CHUNK_OVERLAP", "1500"))  # 向后重叠, 防切断
# 块数上限。见模块 docstring: p99=174k → 10 块, 取 12 覆盖到 p99 之外。
# 超过的标记 too_long 挂起, 不硬跑 —— 那些是提取失败(RTF 源码)或 SEC 全文, 都不该走这条管道。
_MAX_BLOCKS = int(os.environ.get("EDGE_MAX_BLOCKS", "12"))

# 提取失败的特征: md 里是 RTF/富文本控制码而不是正文。分块只会把垃圾切成 N 份垃圾,
# 所以在分块之前先挡掉, 并且明确记 too_long/not_text 而不是静默跳过。
# {PSQL 2026-08-08 最长两条 md 以 "{\rtf1\adeflang1025\ansi..." 开头, 3,771,299 与 2,744,391 字符}
_NOT_TEXT = re.compile(r"^\s*\{\\rtf\d|^\s*%PDF-|^\s*PK\x03\x04")


def _split_blocks(text: str) -> list[str]:
    """把长正文切成带重叠的块。重叠是为了不让一句话/一张表正好落在块边界上。

    上游: _build_jobs。下游: 每块各调一次 Step 1, 结果按 evidence 去重后合并。
    n 用 ceil-div 算, 保证每块实际长度 <= _CHUNK_CHARS(而不是恰好等于, 那样最后一块会溢出)。
    """
    if len(text) <= _CHUNK_CHARS:
        return [text]
    n = -(-len(text) // _CHUNK_CHARS)                      # ceil-div
    step = -(-len(text) // n)                              # 每块步进
    out = []
    for i in range(n):
        start = max(0, i * step - (_CHUNK_OVERLAP if i else 0))
        out.append(text[start:(i + 1) * step])
    return out


def _build_jobs(rec: dict) -> tuple[list[dict], dict]:
    """把一条样本拼成 1..N 个 QwenClient job(长正文分块)。返回 (jobs, meta)。

    上游: edge_200 的 ev_*.json。下游: QwenClient.send_many + _merge。
    guided_json 用 STEP1_SCHEMA 强约束输出结构 —— vLLM 侧做语法约束比事后解析可靠得多。

    meta.skip 非空时 jobs 为空: 这条不进模型, 但**会被记录**而不是静默消失 ——
    not_text(提取失败, md 是 RTF 控制码)与 too_long(>12 块, 多半是 SEC 全文)
    都是需要人看的信号, 不是可以忽略的边角。
    """
    i = rec["input"]
    body = i.get("body") or ""

    # ① 非正文先挡: 分块只会把 RTF 垃圾切成 N 份垃圾, 还白烧 N 次 LLM
    if _NOT_TEXT.search(body[:200]):
        return [], {"n_blocks": 0, "skip": "not_text", "n_chars": len(body)}

    blocks = _split_blocks(body)

    # ② 块数超限 → 挂起。这类不是"长文档"而是 SEC 全文或提取异常, 硬跑没有意义
    if len(blocks) > _MAX_BLOCKS:
        return [], {"n_blocks": len(blocks), "skip": "too_long", "n_chars": len(body)}

    common = dict(
        ticker=(i.get("company") or {}).get("ticker") or "?",
        ir_url=(i.get("company") or {}).get("ir_url") or "?",
        title=i.get("title") or "",
        etype=i.get("type") or "",
        date=i.get("date") or "",
        precision=i.get("date_precision") or "unknown",
    )
    jobs = [{
        "system": "You extract entities and relationships from investor-relations text. Output JSON only.",
        "user": STEP1_PROMPT.format(body=b, level_block=prompt_block(), **common),
        "guided_json": STEP1_SCHEMA,
    } for b in blocks]
    return jobs, {"n_blocks": len(blocks), "skip": None, "n_chars": len(body)}


def _merge(outs: list[dict]) -> dict:
    """把同一事件多个块的抽取结果合并成一份。

    去重键的选择是有讲究的:
      mention 按 name 去重 —— 同一实体会在多个块里各出现一次(重叠区更是必然)
      edge 按 (subject, predicate, object) 去重 —— 同一条关系在重叠区会被抽两遍
    保留**先出现的那个**: 块是按正文顺序切的, 先出现的通常在更完整的上下文里。
    title_body_mismatch 取 or —— 任何一块认为不符就是不符。
    """
    mentions, edges, seen_m, seen_e = [], [], set(), set()
    mismatch, reasons = False, []
    for o in outs:
        if not isinstance(o, dict):
            continue
        for m in o.get("mentions") or []:
            k = (m.get("name") or "").strip().lower()
            if k and k not in seen_m:
                seen_m.add(k)
                mentions.append(m)
        for e in o.get("edges") or []:
            k = ((e.get("subject") or "").lower(), (e.get("predicate") or "").lower(),
                 (e.get("object") or "").lower())
            if k not in seen_e:
                seen_e.add(k)
                edges.append(e)
        mismatch = mismatch or bool(o.get("title_body_mismatch"))
        if o.get("no_edge_reason"):
            reasons.append(o["no_edge_reason"])
    return {"mentions": mentions, "edges": edges,
            "title_body_mismatch": mismatch,
            "no_edge_reason": " | ".join(reasons[:3]) or None}


async def _retry_failed(client, rec: dict, out: dict, chk: dict) -> tuple[dict, dict]:
    """对逐字校验失败的条目做一轮定向重试。返回 (合并后的 out, 新的 chk)。

    上游: main 拿到首轮 chk 后, 若 dropped 非空则调用。
    下游: 合并结果重新走 check_step1, 仍失败的才真正丢弃。

    只把**失败的条目**回给模型, 不重跑整篇 —— 通过的部分再问一次只会让模型改动已经对的答案。
    正文用第一块(重试针对的是抄写而不是重新阅读; 若原文在后面的块里, 模型会诚实地不返回该条,
    那正是我们要的行为)。
    """
    lines = []
    for name, why in chk["dropped"]["mentions"]:
        src = next((m for m in (out.get("mentions") or []) if m.get("name") == name), {})
        lines.append(f'- mention "{name}" — 你给的 evidence: "{(src.get("evidence") or "")[:200]}"')
    for pair, why in chk["dropped"]["edges"]:
        lines.append(f'- edge {pair} — 你给的 evidence 在正文里找不到')
    if not lines:
        return out, chk

    body = rec["input"]["body"]
    r = await client.send_one(
        system="You correct evidence quotes. Output JSON only.",
        user=STEP1_RETRY_PROMPT.format(body=body[:_CHUNK_CHARS], failed_block="\n".join(lines[:20])),
        guided_json=STEP1_SCHEMA)
    if not isinstance(r, dict) or r.get("__error__"):
        return out, chk

    # 合并: 重试结果里通过校验的条目补回原结果。_merge 的去重键会挡掉重复。
    merged = _merge([out, r])
    return merged, check_step1(merged, body)


def _write_trace(d: str, rec: dict, job: dict, raw: dict | None, chk: dict | None,
                 blkmeta: dict, err: str | None) -> None:
    """给一个事件写它的可调试目录: prompt.txt(完整输入) + raw.json(模型原始输出) + trace.txt(人读)。

    WHY 三个文件都要: 只存解析后的结果, 人就无法判断「模型读错了」还是「我们 prompt 问错了」。
    media_run.py 用的是同一套 —— 完整 prompt + RAW 输出 + 人读 trace。
    """
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "prompt.txt"), "w") as f:
        f.write(job["user"])
    if raw is not None:
        with open(os.path.join(d, "raw.json"), "w") as f:
            json.dump(raw, f, ensure_ascii=False, indent=1)

    i, m = rec["input"], rec["meta"]
    L = [f"# {rec['id']}   stratum={m['stratum']}",
         f"ticker      {(i.get('company') or {}).get('ticker')}",
         f"title       {i.get('title')}",
         f"type/date   {i.get('type')}  {i.get('date')} ({i.get('date_precision')})",
         f"n_chars     {m['n_chars']}   块数 {blkmeta.get('n_blocks')}"
         + (f"   ★ 前置挡下: {blkmeta['skip']}" if blkmeta.get("skip") else ""),
         f"锚点        {m.get('exchange_tags')}",
         f"doc_url     {m.get('doc_url')}", ""]
    if err:
        L += [f"## 失败", err]
    elif chk:
        s = chk["stats"]
        L += [f"## 统计",
              f"mentions  {s['mentions_in']} 抽出 → {s['mentions_kept']} 通过逐字校验",
              f"edges     {s['edges_in']} 抽出 → {s['edges_kept']} 通过逐字校验",
              f"title_body_mismatch  {s['title_body_mismatch']}",
              f"no_edge_reason       {(raw or {}).get('no_edge_reason')}", ""]
        if chk["dropped"]["mentions"] or chk["dropped"]["edges"]:
            L += ["## 被丢弃(证据不是原文逐字)"]
            L += [f"  mention  {n}  — {why}" for n, why in chk["dropped"]["mentions"]]
            L += [f"  edge     {n}  — {why}" for n, why in chk["dropped"]["edges"]]
            L += [""]
        L += ["## mentions"]
        for x in chk["kept"]["mentions"]:
            L += [f"  [{x.get('kind')}] {x.get('name')}",
                  f"      search_keys  {x.get('search_keys')}",
                  f"      role         {x.get('role_in_text')}",
                  f"      alias/tag    {x.get('aliases_in_text')} / {x.get('exchange_tag')}",
                  f"      evidence     {(x.get('evidence') or '')[:160]}"]
        L += ["", "## edges"]
        for e in chk["kept"]["edges"]:
            L += [f"  {e.get('subject')}  --{e.get('predicate')}-->  {e.get('object')}",
                  f"      at {e.get('valid_at')} ({e.get('valid_precision')})  attrs={e.get('attrs')}",
                  f"      evidence     {(e.get('evidence') or '')[:160]}"]
    with open(os.path.join(d, "trace.txt"), "w") as f:
        f.write("\n".join(L) + "\n")


async def _process_one(client, sem, rec: dict, agg, preds, lv, per_stratum) -> None:
    """处理一条样本 → **立刻落盘**。这是流式的关键: 完成一条写一条, 不攒到最后。

    上游: main 为每条样本起一个 task。下游: 它自己写完 trace.txt / out.json 就结束。

    ★ 为什么必须流式: 首版是 send_many 一次性提交全部 300 个块、全部返回才开始写盘 ——
    200 条要跑二十分钟, 期间被打断就【一条产出都不剩】。2026-08-08 连续两次 200 条跑批
    都因此归零。改成按事件落盘后, 打断只损失正在跑的那一条。
    """
    idx_dir = os.path.join(os.path.abspath(_OUT), rec["id"])
    st = rec["meta"]["stratum"]

    # 断点续跑: 已有 out.json 的直接跳过。中断后重跑只处理没做完的。
    if os.path.exists(os.path.join(idx_dir, "out.json")):
        agg["resumed"] += 1
        return

    jobs, mt = _build_jobs(rec)

    if mt["skip"]:
        agg["skipped"] += 1
        agg[f"skip_{mt['skip']}"] += 1
        per_stratum[st]["skipped"] += 1
        _write_trace(idx_dir, rec, {"user": "(前置挡下, 未进模型)"}, None, None, mt,
                     f"前置挡下: {mt['skip']}  ({mt['n_chars']} 字符 / {mt['n_blocks']} 块)")
        return

    async with sem:                       # 事件级并发闸, 与 QwenClient 内部信号量叠加
        blocks_out = await client.send_many(jobs)

        ok_blocks = [o for o in blocks_out if isinstance(o, dict) and not o.get("__error__")]
        n_failed_blocks = len(blocks_out) - len(ok_blocks)
        if not ok_blocks:
            agg["failed"] += 1
            per_stratum[st]["failed"] += 1
            _write_trace(idx_dir, rec, jobs[0], None, None, mt,
                         f"全部 {len(blocks_out)} 块都失败")
            return

        out = _merge(ok_blocks)
        chk = check_step1(out, rec["input"]["body"])

        # 逐字校验失败 → 定向重试。区分「抄写手滑」与「凭空编造」:
        # 给了具体反馈还找不到原句的, 才判定为编造并真正丢弃。
        for _ in range(_RETRY_MAX):
            if not (chk["dropped"]["mentions"] or chk["dropped"]["edges"]):
                break
            before = len(chk["kept"]["mentions"]) + len(chk["kept"]["edges"])
            out, chk = await _retry_failed(client, rec, out, chk)
            agg["retry_recovered"] += (len(chk["kept"]["mentions"]) + len(chk["kept"]["edges"])) - before

    _write_trace(idx_dir, rec, {"user": jobs[0]["user"]}, out, chk, mt, None)
    with open(os.path.join(idx_dir, "out.json"), "w") as f:
        json.dump({"id": rec["id"], "stratum": st,
                   "n_blocks": mt["n_blocks"], "n_failed_blocks": n_failed_blocks,
                   "kept": chk["kept"], "dropped": chk["dropped"],
                   "stats": chk["stats"],
                   "no_edge_reason": out.get("no_edge_reason")}, f, ensure_ascii=False, indent=1)

    for e_ in chk["kept"]["edges"]:
        preds[(e_.get("predicate") or "?").strip().lower()] += 1
        # -1 = 模型没给这个字段(prompt 缺陷), 与「模型判定为 5」是完全不同的信号
        lv[int(e_["level"]) if e_.get("level") is not None else -1] += 1

    sd = chk["stats"]
    for k, c in (("events", 1), ("blocks", mt["n_blocks"]),
                 ("chunked", int(mt["n_blocks"] > 1)), ("failed_blocks", n_failed_blocks),
                 ("mentions_in", sd["mentions_in"]), ("mentions_kept", sd["mentions_kept"]),
                 ("edges_in", sd["edges_in"]), ("edges_kept", sd["edges_kept"]),
                 ("mismatch", int(sd["title_body_mismatch"])),
                 ("no_edge", int(sd["edges_kept"] == 0))):
        agg[k] += c
        per_stratum[st][k] += c

    done = agg["events"] + agg["skipped"] + agg["failed"]
    if done % 10 == 0:
        print(f"    …已完成 {done}", flush=True)


async def main() -> int:
    """读数据集 → 每条独立处理并即时落盘 → 汇总。

    上游: sample.py 产出的 edge_200。下游: 人读 trace.txt; score.py 算指标。
    不写任何数据库。
    """
    files = sorted(glob.glob(os.path.join(os.path.abspath(_DATASET), "ev_*.json")))
    if _LIMIT:
        files = files[:_LIMIT]
    if not files:
        print(f"数据集为空: {_DATASET}", file=sys.stderr)
        return 2
    recs = [json.load(open(f)) for f in files]
    print(f"跑 {len(recs)} 条  →  {_OUT}", flush=True)

    agg = collections.Counter()
    preds: collections.Counter = collections.Counter()
    lv: collections.Counter = collections.Counter()
    per_stratum: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    client = QwenClient()
    sem = asyncio.Semaphore(int(os.environ.get("EDGE_EVENT_CONCURRENCY", "3")))
    await asyncio.gather(*[_process_one(client, sem, r, agg, preds, lv, per_stratum) for r in recs])

    print("\n════════ 汇总 ════════")
    e = max(agg["events"], 1)
    print(f"  成功 {agg['events']}  失败 {agg['failed']}  前置挡下 {agg['skipped']}"
          f" (not_text {agg['skip_not_text']} / too_long {agg['skip_too_long']})"
          f"  断点跳过 {agg['resumed']}")
    print(f"  总块数 {agg['blocks']}  其中分块处理的事件 {agg['chunked']} 条  块级失败 {agg['failed_blocks']}")
    print(f"  mention  抽出 {agg['mentions_in']} → 逐字校验通过 {agg['mentions_kept']} "
          f"({100 * agg['mentions_kept'] // max(agg['mentions_in'], 1)}%)")
    print(f"  edge     抽出 {agg['edges_in']} → 逐字校验通过 {agg['edges_kept']} "
          f"({100 * agg['edges_kept'] // max(agg['edges_in'], 1)}%)")
    print(f"  重试救回 {agg['retry_recovered']} 条 —— 这些是抄写手滑而非编造, 直接丢会误杀")
    print(f"  每条平均产边 {agg['edges_kept'] / e:.2f}   标题正文不符 {agg['mismatch']}")

    if lv:
        tot = sum(lv.values())
        print(f"\n  ── 确定度分布(边) ──")
        for k in sorted(lv):
            tag = "  ★ 模型未给该字段(prompt 问题, 不是模型不确定)" if k < 0 else ""
            print(f"  level {k if k >= 0 else '缺失':>5}  {lv[k]:5d}  {'█' * (30 * lv[k] // max(tot, 1))}{tag}")
        writable = sum(c for k, c in lv.items() if 0 <= k <= WRITE_MAX)
        print(f"  可写入主图(level<={WRITE_MAX}) {writable}  ·  挂起(level>={PENDING_AT}) {tot - writable}")

    if preds:
        once = sum(1 for _, c in preds.items() if c == 1)
        print(f"\n  ── predicate 复用度 ──")
        print(f"  不同 predicate {len(preds)} 个 / 边总数 {sum(preds.values())}"
              f"   只出现一次的占 {100 * once // max(len(preds), 1)}%")
        print("  最常见: " + " · ".join(f"{k}×{c}" for k, c in preds.most_common(8)))

    print("\n  ── 按层 ──")
    print(f"  {'stratum':18s} {'条数':>5s} {'产边':>6s} {'无边':>6s} {'逐字通过率':>10s}")
    for st, c in sorted(per_stratum.items()):
        rate = 100 * c["edges_kept"] // max(c["edges_in"], 1)
        print(f"  {st:18s} {c['events']:5d} {c['edges_kept']:6d} {c['no_edge']:6d} {rate:9d}%")
    print(f"\n  人读入口: {os.path.abspath(_OUT)}/<id>/trace.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
