"""verify_storage — prove the EVENT-level storage layer is correct against a real Postgres, with ZERO browser/VLM/GPU.

用一句话讲完: 塞一家 test 公司 → 同一个 event 落两次(第二次多一个 media)证明幂等 + media 合并 → 存一页 content
证明 save_pages 写的 content_hash 正好等于 sha256(content),也就是 incremental hash-gate 下一轮比对时依赖的那个不变量。
全程真 Postgres,无 GPU。

历史: 本文件的前身是 verify_worker.py,它的 5 个用例里有 4 个测的是 company-level lease 机制(claim_company /
reconcile / mark_company / 完成判据)。统一调度器落地后那套机制已无任何生产调用方,已于 2026-07-27 连同 companies
.event_count 一起删除,所以那 4 个用例也随之移除;这里保留唯一仍覆盖存活代码的 flush 幂等用例,并补上 hash-gate 不变量。
{GREP 2026-07-27 "ZERO PRODUCTION CALLERS OF CLAIM_COMPANY / MARK_COMPANY / RECONCILE"}
[CONFIDENCE: CONFIRMED 100% — 删除前全仓 grep 过调用方].

Run ON GCP (never the local Mac {MEMORY "never run compute on the local Mac"}):
  WATEREVENTS_DB_DSN=<supavisor 6543 dsn> python3 -m tests.event.verify.verify_storage
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from agent.event_agent.storage import events as db

_MARKER = "https://verify.test/"                     # every row this test creates carries it → cleanup can't touch real data


async def _seed_company(pool) -> str:
    """Reset this test's rows and insert ONE company, returning its id. Scoped by the _MARKER ir_url prefix so a re-run
    (or a crash mid-run) never deletes production companies."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM events WHERE run_id = 'verify-run';")
        await conn.execute("DELETE FROM pages  WHERE url LIKE $1;", _MARKER + "%")
        await conn.execute("DELETE FROM companies WHERE ir_url LIKE $1;", _MARKER + "%")
        return await conn.fetchval("INSERT INTO companies (ir_url) VALUES ($1) RETURNING id;", _MARKER + "co0")


async def main() -> None:
    pool = await db.connect_pool(max_size=4)
    results = {}
    try:
        cid = await _seed_company(pool)

        # ── TEST 1 — 幂等 flush + media merge: 同一 event 落两次(第二次多一个 pdf)→ 仍是 1 行,且 media 取并集 ──
        # 这正是 incremental 每轮重扫同一个 hub 却不会产生重复 event 的依据。{SCAN.PY "A RE-SCAN MERGES (ON CONFLICT
        # DEDUP_KEY), SO INCREMENTAL RE-SCANNING A HUB EVERY CYCLE ONLY ADDS NEWLY-ANNOUNCED EVENTS"}
        ev = {"title": "Q1", "date": "2026-04-03", "type": "earnings", "urls": [_MARKER + "e/1"]}
        await db.flush_events(pool, cid, "verify-run", [ev])
        await db.flush_events(pool, cid, "verify-run", [{**ev, "urls": [_MARKER + "e/1", _MARKER + "e/1.pdf"]}])
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT media_urls FROM events WHERE company_id=$1;", cid)
        merged = json.loads(rows[0]["media_urls"]) if rows and isinstance(rows[0]["media_urls"], str) \
            else (rows[0]["media_urls"] if rows else [])
        results["1_idempotent_merge"] = (len(rows) == 1 and set(merged) == {_MARKER + "e/1", _MARKER + "e/1.pdf"})

        # ── TEST 2 — hash-gate 不变量: save_pages 存的 content_hash 必须 == sha256(content) ──
        # scan._make_gate 用 sha256(fresh page_text) 去比对这一列;两边只要有一丝不一致,gate 就永远判"变了"→ 每轮都
        # 烧 VLM。2026-07-27 的事故正是这一列为 NULL(migration 加列后没回填)导致 12,382 页恒判变更、hash-gate 全程失效。
        # {SCAN.PY:49 "RETURN PREV != H  # DIFFER (OR FIRST-SEEN: PREV NONE) → CHANGED → EXTRACT"}
        # {MEASURED 2026-07-27 "12578/14468 = 86.9% OF PAGES HAD content_hash NULL; INCREMENTAL BURNED VLM ON 84% OF SCANS"}
        # [CONFIDENCE: CONFIRMED 100% — 回填后全表 14468/14468 复算一致, 0 mismatch].
        body = "Investor Day 2026 — agenda and webcast replay."
        await db.save_pages(pool, cid, "verify-run", [{"url": _MARKER + "p/1", "content": body}])
        async with pool.acquire() as conn:
            stored = await conn.fetchval("SELECT content_hash FROM pages WHERE company_id=$1 AND url=$2;",
                                         cid, _MARKER + "p/1")
        results["2_hash_gate_invariant"] = (stored == hashlib.sha256(body.encode("utf-8")).hexdigest())

        # ── TEST 3 — save_pages 幂等: 同一 url 再存(内容变了)→ 仍是 1 行, hash 跟着新内容走 ──
        # 若这里退化成插入第二行, (company_id,url) 的 gate 查询就会读到不确定的那一行 → 漏 skip 或错 skip。
        body2 = body + " Updated: replay link added."
        await db.save_pages(pool, cid, "verify-run", [{"url": _MARKER + "p/1", "content": body2}])
        async with pool.acquire() as conn:
            n_pages = await conn.fetchval("SELECT count(*) FROM pages WHERE company_id=$1 AND url=$2;",
                                          cid, _MARKER + "p/1")
            stored2 = await conn.fetchval("SELECT content_hash FROM pages WHERE company_id=$1 AND url=$2;",
                                          cid, _MARKER + "p/1")
        results["3_pages_upsert"] = (n_pages == 1 and stored2 == hashlib.sha256(body2.encode("utf-8")).hexdigest())

        for k, v in results.items():
            print(f"  {'✅' if v else '❌'} {k}")
        print(f"\n[verify_storage] {'ALL PASS ✅' if all(results.values()) else 'SOME FAILED ❌'}")

        async with pool.acquire() as conn:                   # cleanup so a re-run starts clean (marker-scoped)
            await conn.execute("DELETE FROM events WHERE run_id='verify-run';")
            await conn.execute("DELETE FROM pages  WHERE url LIKE $1;", _MARKER + "%")
            await conn.execute("DELETE FROM companies WHERE ir_url LIKE $1;", _MARKER + "%")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
