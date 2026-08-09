r"""tests/edge/verify.py — 让模型复核已抽出的边。直接跑在 edge_run 的产出上,不重跑 Step 1。

用一句话讲完: 读 /tmp/edge_out 里已有的边 → 连同原文一起交回模型 → 逐条判 keep / fix / drop →
落一份 verify.json 并打统计。**这一步不写任何规则**, 判断全部由模型做。

## 为什么是模型验证而不是程序校验

首版我写了一条规则「evidence 里出现了非本边端点的实体 → 张冠李戴嫌疑」, 200 条上报出 42 条,
实际读下来一半是误报 —— "Zebra" 被当成了 "Zebra Technologies Corporation" 之外的另一个实体,
而它只是简称。下意识反应是再加一条「用 aliases 归一」去补, 但补丁还会遇到缩写、旧称、译名、
带不带法人后缀…… 永远补不完。
{USER 2026-08-08 "you shouldnt check right, use llm to determien"}

分界线:
  · 纯机械的事实检查 → 代码(evidence 是不是原文逐字子串 —— 字符串包含, 不需要理解任何东西)
  · 需要理解语义的判断 → 模型(简称指不指同一家、施动者是母公司还是子公司)

## 要抓的是一个系统性偏差, 不是零散错误

200 条实测里的真错几乎都是同一种: **模型把「文章的主角公司」当成了所有关系的默认主体**。
  Bitdeer Technologies Group --employs--> Paul Hanson
    「Paul Hanson, Chairman of Bitdeer Industrial」—— 他是子公司的董事长
  Bitdeer Technologies Group --communicates_with--> Taylor Adams
    「Taylor Adams, President and CEO of the Economic Development Authority of Western Nevada」
    —— 他根本不是 Bitdeer 的人
这也解释了为什么 employs 高居第一(41/223): 文里出现的人被一律挂到主角公司名下。

## 跑法

    export $(sudo grep -E "^QWEN_" /etc/waterevents/fleet.env | xargs)
    PYTHONPATH=$PWD/backend EDGE_OUT=/tmp/edge_out EDGE_DATASET=/tmp/edge_200 \
      python3 tests/edge/verify.py
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

from providers.qwen_llm import QwenClient
from tests.edge.iters import prompt_block
from tests.edge.prompts import VERIFY_PROMPT, VERIFY_SCHEMA

_OUT = os.environ.get("EDGE_OUT", "/tmp/edge_out")
_DATASET = os.environ.get("EDGE_DATASET", "/tmp/edge_200")
# 与 Step 1 同一个窗口预算 —— 验证要看到原文才能判主体归属
_BODY_MAX = int(os.environ.get("EDGE_CHUNK_CHARS", "18000"))


def _edges_block(edges: list[dict]) -> str:
    """把边渲染成给模型复核的清单。带序号, 模型按序号回判决。

    证据要原样给全 —— 复核的核心就是「证据支不支持这条关系」, 截断证据等于让它盲判。
    """
    out = []
    for i, e in enumerate(edges):
        out.append(f"[{i}] {e.get('subject')}  --{e.get('predicate')}-->  {e.get('object')}\n"
                   f"    证据: {e.get('evidence')}")
    return "\n".join(out)


async def _verify_one(client, sem, rid: str, agg: collections.Counter) -> None:
    """复核一条样本的全部边 → 落 verify.json。

    上游: edge_run 产出的 out.json。下游: 人读 verify.json, 或据 verdict 决定写不写库。
    一次把这条样本的所有边一起给模型 —— 让它能看到边与边之间的关系
    (同一篇里 employs 挂错主体往往是成批的, 一起看才看得出规律)。
    """
    d = json.load(open(os.path.join(_OUT, rid, "out.json")))
    edges = d["kept"]["edges"]
    if not edges:
        return
    vf = os.path.join(_OUT, rid, "verify.json")
    if os.path.exists(vf):                       # 断点续跑
        agg["resumed"] += 1
        return

    rec = json.load(open(os.path.join(_DATASET, f"{rid}.json")))
    async with sem:
        r = await client.send_one(
            system="You verify extracted relationships against the source text. Output JSON only.",
            user=VERIFY_PROMPT.format(body=rec["input"]["body"][:_BODY_MAX],
                                      edges_block=_edges_block(edges),
                                      level_block=prompt_block()),
            guided_json=VERIFY_SCHEMA)
    if not isinstance(r, dict) or r.get("__error__"):
        agg["failed"] += 1
        return

    verdicts = {v["i"]: v for v in (r.get("verdicts") or []) if isinstance(v, dict) and "i" in v}
    merged = []
    for i, e in enumerate(edges):
        v = verdicts.get(i)
        # 模型没给判决的边计入 no_verdict —— 不默认放行, 那等于悄悄当成 keep
        if not v:
            agg["no_verdict"] += 1
            merged.append({**e, "_verdict": "no_verdict"})
            continue
        agg[v["verdict"]] += 1
        e2 = {**e, "_verdict": v["verdict"], "_reason": v.get("reason")}
        if v["verdict"] == "fix":
            e2["_orig_subject"], e2["_orig_object"] = e.get("subject"), e.get("object")
            e2["subject"] = v.get("subject") or e.get("subject")
            e2["object"] = v.get("object") or e.get("object")
        if v.get("iter") is not None:
            e2["iter"] = v["iter"]
        merged.append(e2)

    with open(vf, "w") as f:
        json.dump({"id": rid, "stratum": d["stratum"], "edges": merged}, f, ensure_ascii=False, indent=1)
    agg["events"] += 1


async def main() -> int:
    """对 edge_out 里所有有边的样本跑复核, 打统计。只读 out.json, 只写 verify.json。"""
    rids = [os.path.basename(os.path.dirname(f))
            for f in sorted(glob.glob(os.path.join(os.path.abspath(_OUT), "*", "out.json")))]
    if not rids:
        print(f"没有结果: {_OUT}", file=sys.stderr)
        return 2
    print(f"复核 {len(rids)} 条样本的边 …", flush=True)

    agg = collections.Counter()
    client = QwenClient()
    sem = asyncio.Semaphore(int(os.environ.get("EDGE_EVENT_CONCURRENCY", "6")))
    await asyncio.gather(*[_verify_one(client, sem, r, agg) for r in rids])

    tot = agg["keep"] + agg["fix"] + agg["drop"] + agg["no_verdict"]
    print(f"\n════════ 复核结果({tot} 条边) ════════")
    for k, label in (("keep", "成立"), ("fix", "关系对但主体/客体写错"),
                     ("drop", "证据不支持"), ("no_verdict", "模型没给判决")):
        n = agg[k]
        print(f"  {label:22s} {n:5d}  ({100 * n // max(tot, 1):3d}%)  {'█' * (30 * n // max(tot, 1))}")
    print(f"\n  可直接用的(keep)          {agg['keep']}")
    print(f"  修正后可用(keep+fix)      {agg['keep'] + agg['fix']}")
    print(f"  样本 {agg['events']} 条  失败 {agg['failed']}  断点跳过 {agg['resumed']}")
    print(f"\n  逐条理由见 {os.path.abspath(_OUT)}/<id>/verify.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
