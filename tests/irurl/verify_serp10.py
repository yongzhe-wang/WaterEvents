"""verify_serp10 — 验证 SERP 兜底修复(DDG→Brave)后, 之前"只拿到 primary"的公司能不能真找到 events 页。

用一句话讲完: 读一份公司清单(全是首页渲染失败、必然走 SERP 兜底的), 用修好的 discover_company 重跑, 打印每家发现的
hub 明细 —— 直接回答"走 SERP 时质量会不会掉"。不落库(纯验证)。
{PSQL 2026-07-26 "SERP-FALLBACK 32 家 AVG_HUBS=1.00 REAL_EVENT_PAGES=0" — 修复前 SERP 路径产出为零}
Run ON THE POD: VERIFY_FILE=/tmp/verify10.json PYTHONPATH=/workspace/WaterEvents /root/venv/bin/python tests/verify_serp10.py
"""
from __future__ import annotations

import asyncio
import json
import os

from providers.qwen_llm import QwenClient
from agent.ir_url_agent.discover import discover_company

FILE = os.environ.get("VERIFY_FILE", "/tmp/verify10.json")
REAL = {"events", "calendar", "presentations"}          # 真正有价值的 kind(不是 homepage/news)


async def main() -> None:
    cos = json.load(open(FILE, encoding="utf-8"))
    client = QwenClient()
    tot_hubs = tot_real = with_real = 0
    for c in cos:
        try:
            hubs = await discover_company(c, client)
        except Exception as e:                          # noqa: BLE001 — 一家失败继续下一家
            print("{:10} EXC {}".format(c.get("ticker"), str(e)[:70]), flush=True)
            continue
        real = [h for h in hubs if h.get("kind") in REAL]
        tot_hubs += len(hubs); tot_real += len(real); with_real += 1 if real else 0
        print("\n=== {} (before: {} hub) -> now {} hubs, {} real event-pages ===".format(
            c.get("ticker"), c.get("prev_hubs"), len(hubs), len(real)), flush=True)
        for h in hubs[:10]:
            print("   [{:13}] seed={:5} {}".format(h.get("kind"), str(h.get("is_seed")), (h.get("url") or "")[:82]), flush=True)
    n = len(cos)
    print("\n===== SUMMARY over {} SERP-path companies =====".format(n), flush=True)
    print("avg hubs/company      : {:.2f}   (was 1.00 with the broken DDG fallback)".format(tot_hubs / max(n, 1)), flush=True)
    print("real event-pages found: {}  (was 0)".format(tot_real), flush=True)
    print("companies w/ >=1 real : {}/{}  (was 0/{})".format(with_real, n, n), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
