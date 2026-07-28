"""run_irurl_all — 一个脚本把全库公司的 IR 入口 URL 发现完, 结果写进 waterevents.companies.event_hubs。

用一句话讲完: 连 waterevents → 一次性取出全部公司(events 最少的排前面, 最需要救的先做)→ semaphore 控并发, 每家跑
discover_company(watercrawl 渲 IR 首页收 nav 链接 → Qwen 14B guided_json 分类)→ 立刻幂等写回 event_hubs → 打进度。
WHY 一个脚本而不是常驻 worker: 这是一次性全量回填, 不需要队列/lease/重入那套; 断了直接重跑, 已完成的靠
ir_url_source='ir_url_agent' 跳过(RESUME 默认开)。{USER 2026-07-26 "JUST RUN ALL USE ONE SCRIPT"}.

Run ON THE POD (vLLM 14B up):
  cd /workspace/WaterEvents && WATERCRAWL_NO_SHOT=1 WATERCRAWL_HTTP_FIRST=0 \
    WATEREVENTS_DB_DSN=... QWEN_API_KEY=... QWEN_BASE_URLS=http://127.0.0.1:8000/v1 \
    PYTHONPATH=/workspace/WaterEvents/backend nohup /root/venv/bin/python tests/run_irurl_all.py > /workspace/irurl_all.log 2>&1 &
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from providers.qwen_llm import QwenClient
from agent.ir_url_agent import db
from agent.ir_url_agent.discover import discover_company

CONC = int(os.environ.get("IRURL_CONC", "8"))          # 同时处理几家(每家 = 1 次浏览器渲染 + 1 次 Qwen 调用)
LIMIT = int(os.environ.get("IRURL_LIMIT", "0"))        # 只跑前 N 家(0 = 全部), 调试用
RESUME = os.environ.get("IRURL_RESUME", "1") not in ("0", "false", "no")   # 跳过已做过的(断点续跑)


async def _fetch_all(pool) -> list[dict]:
    """取全部待做公司 → [{id,ticker,ir_url}]。ORDER BY event 数升序 = 最缺 events 的先做(中途断了也先赚到最高价值那批)。
    RESUME 判据 = event_hubs[0] 是不是 OBJECT。WHY 用数据形状而不是 ir_url_source: 那个 tag 有 2559 行是**老的外部
    ir_url_agent** 留下的(从 legacy public.companies 一起 backfill 过来), 拿它当"做过了"会把 2554 家从没处理过的公司
    全跳过。本脚本写的 hub 一定是 object({url,kind,source,is_seed,alive,checked_at}), legacy 是裸 URL 字符串, 形状能
    100% 区分。{PSQL 2026-07-26 "TAGGED IR_URL_AGENT 2559 / EVENT_HUBS[0] IS OBJECT 5 / IS STRING 2390"}
    [CONFIDENCE: CONFIRMED — 5 家实测写入后 object 计数正好从 0 变 5, 而 tag 计数纹丝不动仍是 2559]."""
    # 除了"没做过"(hubs 不是 object), 还要重做"做了但只拿到 primary 一条"的 —— 那批是 SERP 兜底挂掉的受害者
    # (DDG 每次 22s 超时 → 兜底返回空 → 只剩我们始终插入的 primary)。换 Brave 后它们值得再试一次。
    # {PSQL 2026-07-26 "SERP-FALLBACK 32 家 AVG_HUBS=1.00 REAL_EVENT_PAGES=0" vs "NAV-HARVEST 174 家 AVG_HUBS=5.24"}
    where_resume = ("AND (jsonb_typeof(c.event_hubs->0) IS DISTINCT FROM 'object' "
                    "     OR jsonb_array_length(c.event_hubs) <= 1)") if RESUME else ""
    rows = await pool.fetch(
        f"""
        SELECT c.id, c.ticker, c.ir_url
        FROM companies c
        LEFT JOIN events e ON e.company_id = c.id
        WHERE c.ir_url IS NOT NULL AND c.ir_url <> '' {where_resume}
        GROUP BY c.id, c.ticker, c.ir_url
        ORDER BY count(e.id) ASC, c.id
        """)
    out = [{"id": r["id"], "ticker": r["ticker"], "ir_url": r["ir_url"]} for r in rows]
    return out[:LIMIT] if LIMIT else out


async def _one(co: dict, client: QwenClient, pool, sem: asyncio.Semaphore, state: dict, total: int) -> None:
    """一家: discover → flush → 计数打印。任何异常都吞掉(一家失败不能停全量), 失败的下次重跑会再被领到。"""
    async with sem:
        try:
            hubs = await discover_company(co, client)
        except Exception as e:                                 # noqa: BLE001 — 单家失败不沉全局
            hubs = []
            print(f"[irurl] ERR {co.get('ticker')}: {type(e).__name__}: {str(e)[:100]}", flush=True)
        if hubs:                                               # 有发现才写(空发现不覆盖, 留给下次重跑)
            try:
                await db.flush_event_hubs(pool, co["id"], hubs)
            except Exception as e:                             # noqa: BLE001 — 写失败也不能停
                print(f"[irurl] DB-ERR {co.get('ticker')}: {str(e)[:100]}", flush=True)
        state["done"] += 1
        state["hubs"] += len(hubs)
        n_seed = sum(1 for h in hubs if h.get("is_seed"))
        el = time.time() - state["t0"]
        rate = state["done"] / max(el, 1)
        eta = (total - state["done"]) / max(rate, 1e-6) / 60
        print(f"[irurl] {state['done']}/{total} {(co.get('ticker') or '')[:12]:12} "
              f"hubs={len(hubs):2} seed={n_seed:2} | {rate*60:.0f}/min ETA {eta:.0f}min", flush=True)


async def main() -> None:
    pool = await db.connect_pool(min_size=2, max_size=8)
    client = QwenClient()                                      # 一个共享 client → vLLM continuous batching 池化所有分类调用
    try:
        companies = await _fetch_all(pool)
        total = len(companies)
        print(f"[irurl] START {total} companies | CONC={CONC} RESUME={RESUME}", flush=True)
        sem = asyncio.Semaphore(CONC)
        state = {"done": 0, "hubs": 0, "t0": time.time()}
        await asyncio.gather(*(_one(c, client, pool, sem, state, total) for c in companies))
        el = time.time() - state["t0"]
        print(f"[irurl] DONE {state['done']}/{total} companies, {state['hubs']} hubs, {el/60:.1f}min", flush=True)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
