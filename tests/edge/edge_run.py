r"""tests/edge/edge_run.py — 跑 Step 1(读文字出 mention 和 edge), 每条落一个可调试目录。

用一句话讲完: 读 edge_200 数据集的每个 ev_*.json → 拼 Step 1 prompt → QwenClient 批量并发打进 vLLM →
validate.py 做逐字证据校验 → 每个事件写一个 <id>/ 目录(trace.txt 人读 + out.json 机读 + prompt.txt 存证)
→ 最后打一张汇总表。**不写任何库**, 纯离线实验。

## 跟 media_run.py 一样, 没有 ground-truth oracle

除 auto_verifiable 层的 ticker 锚点外不含标注, 质量靠人读 trace 判断 ——
{USER 2026-07-23 "dont rely on ground truth read the output yourself ... structure the output so each page
as txt + full prompt + all the trace"}。所以每个事件都落完整 prompt 和模型原始输出, 而不只是解析后的结果。

## 正文截断是硬约束不是调优旋钮

线上模型 Qwen2.5-14B-Instruct-AWQ 的 max_model_len = 32,768 token, 而数据集正文最长 47,737 字符。
超了 vLLM 直接 400。所以必须截, 截多少由「模型窗口 − 输出预算 − prompt 模板」倒推, 不是拍脑袋。
截断的事实会写进 trace 和汇总 —— 被截过的样本, 它漏掉的边不算模型的错。
{curl /v1/models 2026-08-08 "root":"Qwen/Qwen2.5-14B-Instruct-AWQ","max_model_len":32768}
{client.py 注释 "input 20769 + max 12000 > 32768 → 400" —— 超窗是硬失败, 不是降级}
[CONFIDENCE: CONFIRMED 100% — 端点 /v1/models 实测返回]

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
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from providers.qwen_llm import QwenClient                      # 复用项目的并发传输层, 不自己造轮子
from tests.edge.prompts import STEP1_PROMPT, STEP1_SCHEMA
from tests.edge.validate import check_step1

_DATASET = os.environ.get("EDGE_DATASET",
                          os.path.join(os.path.dirname(__file__), "..", "datasets", "edge_200"))
_OUT = os.environ.get("EDGE_OUT", "/tmp/edge_out")
_LIMIT = int(os.environ.get("EDGE_LIMIT", "0"))                # >0 时只跑前 N 条, 用于快速试跑

# 正文字符上限。倒推自模型窗口:
#   32,768 token 窗口 − 4,000 输出预算 − 约 800 prompt 模板 ≈ 28,000 token
#   英文约 4 字符/token, 但正文含 CJK(样本里日/韩/中文占比不低)按 2 字符/token 保守估
#   → 28,000 × 2 ≈ 56,000, 再留一半余量 → 28,000 字符
# 数据集里 n_chars 中位 5,012, 只有极少数会被截。
_BODY_MAX = int(os.environ.get("EDGE_BODY_MAX", "28000"))


def _build_job(rec: dict) -> tuple[dict, bool]:
    """把一条样本拼成一个 QwenClient job。返回 (job, 是否截断过正文)。

    上游: edge_200 的 ev_*.json。下游: QwenClient.send_many。
    guided_json 用 STEP1_SCHEMA 强约束输出结构 —— vLLM 侧做语法约束比事后解析可靠得多。
    """
    i = rec["input"]
    body = i.get("body") or ""
    truncated = len(body) > _BODY_MAX
    if truncated:
        body = body[:_BODY_MAX]
    user = STEP1_PROMPT.format(
        ticker=(i.get("company") or {}).get("ticker") or "?",
        ir_url=(i.get("company") or {}).get("ir_url") or "?",
        title=i.get("title") or "",
        etype=i.get("type") or "",
        date=i.get("date") or "",
        precision=i.get("date_precision") or "unknown",
        body=body,
    )
    return ({
        "system": "You extract entities and relationships from investor-relations text. Output JSON only.",
        "user": user,
        "guided_json": STEP1_SCHEMA,
    }, truncated)


def _write_trace(d: str, rec: dict, job: dict, raw: dict | None, chk: dict | None,
                 truncated: bool, err: str | None) -> None:
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
         f"n_chars     {m['n_chars']}" + ("   ★ 正文被截断" if truncated else ""),
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


async def main() -> int:
    """读数据集 → 批量跑 Step 1 → 落 trace → 汇总。

    上游: sample.py 产出的 edge_200。下游: 人读 trace.txt; recall.py 消费 out.json 的 mentions。
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

    jobs, trunc = [], []
    for r in recs:
        j, t = _build_job(r)
        jobs.append(j)
        trunc.append(t)

    # send_many 一次性提交全部, 由 QwenClient 的全局信号量控并发(vLLM continuous-batching 在 GPU 侧批处理)。
    # 返回顺序与 jobs 一致 —— asyncio.gather 保序, 所以可以按下标对回样本。
    client = QwenClient()
    outs = await client.send_many(jobs)

    agg = collections.Counter()
    per_stratum: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for rec, job, out, tr in zip(recs, jobs, outs, trunc):
        d = os.path.join(os.path.abspath(_OUT), rec["id"])
        st = rec["meta"]["stratum"]
        # send_many 对失败的 job 返回带 __error__ 的 dict, 不抛异常 —— 单条失败不该中断整批
        err = (out or {}).get("__error__") if isinstance(out, dict) else "no output"
        if err:
            agg["failed"] += 1
            per_stratum[st]["failed"] += 1
            _write_trace(d, rec, job, out, None, tr, str(err))
            continue

        chk = check_step1(out, rec["input"]["body"])
        _write_trace(d, rec, job, out, chk, tr, None)
        with open(os.path.join(d, "out.json"), "w") as f:
            json.dump({"id": rec["id"], "stratum": st, "truncated": tr,
                       "kept": chk["kept"], "dropped": chk["dropped"],
                       "stats": chk["stats"],
                       "no_edge_reason": out.get("no_edge_reason")}, f, ensure_ascii=False, indent=1)

        s = chk["stats"]
        for k, c in (("events", 1), ("truncated", int(tr)),
                     ("mentions_in", s["mentions_in"]), ("mentions_kept", s["mentions_kept"]),
                     ("edges_in", s["edges_in"]), ("edges_kept", s["edges_kept"]),
                     ("mismatch", int(s["title_body_mismatch"])),
                     ("no_edge", int(s["edges_kept"] == 0))):
            agg[k] += c
            per_stratum[st][k] += c

    print("\n════════ 汇总 ════════")
    e = max(agg["events"], 1)
    print(f"  成功 {agg['events']}  失败 {agg['failed']}  正文被截 {agg['truncated']}")
    print(f"  mention  抽出 {agg['mentions_in']} → 逐字校验通过 {agg['mentions_kept']} "
          f"({100 * agg['mentions_kept'] // max(agg['mentions_in'], 1)}%)")
    print(f"  edge     抽出 {agg['edges_in']} → 逐字校验通过 {agg['edges_kept']} "
          f"({100 * agg['edges_kept'] // max(agg['edges_in'], 1)}%)")
    print(f"  每条平均产边 {agg['edges_kept'] / e:.2f}   标题正文不符 {agg['mismatch']}")
    print("\n  ── 按层 ──")
    print(f"  {'stratum':18s} {'条数':>5s} {'产边':>6s} {'无边':>6s} {'逐字通过率':>10s}")
    for st, c in per_stratum.items():
        n = max(c["events"], 1)
        rate = 100 * c["edges_kept"] // max(c["edges_in"], 1)
        print(f"  {st:18s} {c['events']:5d} {c['edges_kept']:6d} {c['no_edge']:6d} {rate:9d}%")
    print(f"\n  人读入口: {os.path.abspath(_OUT)}/<id>/trace.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
