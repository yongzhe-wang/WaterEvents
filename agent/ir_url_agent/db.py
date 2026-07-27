"""ir_url_agent.db — the Postgres layer: 幂等把发现的 event_hubs 写回 waterevents.companies。

用一句话讲完: connect_pool 建 Supavisor transaction-pooler 连接(search_path=waterevents), flush_event_hubs 把一家
公司发现的 hub 数组整列覆盖式幂等写回。**只有这两件事** —— 公司清单由调用方(tests/run_irurl_all.py)自己一次性
SELECT(它要按 event 数排序 + 断点续跑判据, 那是跑法不是 DB 层的事), 发现逻辑在 discover.py。
WHY 独立成层: 跟 event_agent/db.py 一样, DB 访问集中一处、复用同一套 pooler 三要素, 便于单测(只依赖 asyncpg)。
"""
from __future__ import annotations

import json
import os

import asyncpg                                              # 唯一重依赖 — 保持 db 层轻, 不拖抓取栈

# Supavisor transaction-mode pooler DSN(port 6543)从 env 读, 不硬编码 secret。canonical schema=waterevents
# (public 是 07-22 冻结的 legacy)。{SCAN whn86f4f7 "canonical = waterevents ... 写 public 会 false-green"}.
# [CONFIDENCE: CONFIRMED — 三方印证 canonical=waterevents(migrations + db.py:44 default + 前端 Accept-Profile)].
_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")
_SCHEMA = os.environ.get("WATEREVENTS_DB_SCHEMA", "waterevents")


async def connect_pool(min_size: int = 2, max_size: int = 8) -> asyncpg.Pool:
    """建 asyncpg pool。三要素硬约束(照抄 event_agent/db.py:47, 缺一必炸):statement_cache_size=0(Supavisor
    transaction-mode 强制, 否则 'prepared statement does not exist')、search_path pin 到 waterevents(否则裸表名
    落到空 public → false-green)、DSN 从 env(6543 pooler)。{SCAN whn86f4f7 "connect_pool 三要素是硬约束"}."""
    if not _DSN:                                            # fail-loud: 没 DSN 直接报, 别静默连本地
        raise RuntimeError("WATEREVENTS_DB_DSN not set — ir_url_agent needs the Supavisor pooler DSN")
    return await asyncpg.create_pool(_DSN, min_size=min_size, max_size=max_size,
                                     statement_cache_size=0,                       # Supavisor 强制
                                     server_settings={"search_path": _SCHEMA})     # pin canonical schema


async def flush_event_hubs(pool: asyncpg.Pool, company_id, hubs: list[dict]) -> None:
    """把发现的 event_hubs 幂等写回一家公司(整列覆盖 → 重跑同输入=同结果)。hubs 是 discover.py 产出的 object 数组
    (已 merge 了旧值)。同时打 ir_url_source='ir_url_agent'(与既有值域一致)+ ir_url_validation 存一句摘要。
    WHY 整列覆盖而非 append: worker 在内存里已 merge(旧 hubs ∪ 新发现, 按 url dedup), 传进来的就是最终态, 落库直接覆盖。"""
    n_seed = sum(1 for h in hubs if h.get("is_seed"))       # 摘要: 几个 seed / 共几个
    validation = f"ir_url_agent: {len(hubs)} hubs, {n_seed} seed"
    await pool.execute(
        "UPDATE companies SET event_hubs = $2::jsonb, ir_url_source = 'ir_url_agent', "
        "ir_url_validation = $3, updated_at = now() WHERE id = $1",
        company_id, json.dumps(hubs, ensure_ascii=False), validation)
