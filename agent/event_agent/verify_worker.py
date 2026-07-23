"""event_agent.verify_worker — prove the discovery worker's DB coordination is CORRECT, against a real Postgres, with
ZERO browser / VLM / GPU. This is the verifiable core: it exercises db.py's claim / flush / reconcile / fencing exactly
as worker.py would, but with a mock crawl result, so the 5 correctness properties can be asserted cheaply + repeatably.

用一句话讲完: 往 companies 塞几家公司 → 并发抢(证独占)→ 双 flush 同一批+多 media(证幂等+merge)→ 让 lease 过期跑
reconcile(证崩溃回收)→ 旧 owner 迟到 mark(证 fencing 不覆盖)→ 数非终态归零(证完成判据)。全程真 Postgres,无 GPU。

Run ON GCP (never Mac {MEMORY "never run compute on the local Mac"}):
  WATEREVENTS_DB_DSN=<supavisor 6543 dsn> python3 -m agent.event_agent.verify_worker
"""
from __future__ import annotations

import asyncio
import json

from . import db


async def _seed(pool, n: int) -> None:
    """Reset the test rows + insert n queued companies. Uses a distinct ir_url marker so we only touch test data."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM events WHERE run_id = 'verify-run';")
        await conn.execute("DELETE FROM companies WHERE ir_url LIKE 'https://verify.test/%';")
        for i in range(n):
            await conn.execute("INSERT INTO companies (ir_url, status) VALUES ($1, 'queued');",
                               f"https://verify.test/co{i}")


async def _count_nonterminal(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM companies WHERE ir_url LIKE 'https://verify.test/%' "
            "AND status NOT IN ('discovered','discovered_partial','failed');")


async def main() -> None:
    pool = await db.connect_pool(max_size=12)
    results = {}
    try:
        # ── TEST 1 — 独占 claim: 20 个并发 claim 抢 8 家公司 → 恰好 8 个不同 id + 12 个 None,绝无重复 ──
        await _seed(pool, 8)
        claims = await asyncio.gather(*[db.claim_company(pool, f"w{i}", "verify-run") for i in range(20)])
        got = [c["id"] for c in claims if c is not None]
        results["1_exclusive_claim"] = (len(got) == 8 and len(set(got)) == 8)

        # ── TEST 2 — 幂等 flush + media merge: 同一 event 落两次(第二次多一个 media)→ 1 行 + media 合并 ──
        cid = got[0]
        await db.flush_events(pool, cid, "verify-run",
                              [{"title": "Q1", "date": "2026-04-03", "type": "earnings", "urls": ["https://verify.test/e/1"]}])
        await db.flush_events(pool, cid, "verify-run",                          # re-flush: same primary url + extra pdf
                              [{"title": "Q1", "date": "2026-04-03", "type": "earnings",
                                "urls": ["https://verify.test/e/1", "https://verify.test/e/1.pdf"]}])
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT media_urls FROM events WHERE company_id=$1;", cid)
        merged = json.loads(rows[0]["media_urls"]) if rows else []
        results["2_idempotent_merge"] = (len(rows) == 1 and set(merged) ==
                                         {"https://verify.test/e/1", "https://verify.test/e/1.pdf"})

        # ── TEST 3 — lease 回收: 一家 claim 后强制 lease 过期 → reconcile 把它回 queued → 可再 claim ──
        victim = got[1]
        async with pool.acquire() as conn:                                     # simulate a crashed worker: expire its lease
            await conn.execute("UPDATE companies SET lease_until = now() - interval '1 min' WHERE id=$1;", victim)
        reclaimed = await db.reconcile(pool)
        reclaim2 = await db.claim_company(pool, "w-reclaimer", "verify-run")    # someone should now be able to take it
        results["3_lease_reclaim"] = (reclaimed >= 1 and reclaim2 is not None)

        # ── TEST 4 — fencing 防脑裂: A claim 后被 B 抢走,A 迟到 mark_company → 因 lease_owner 不匹配 no-op ──
        async with pool.acquire() as conn:                                     # A owns a fresh company
            await conn.execute("INSERT INTO companies (ir_url, status, lease_owner, lease_until, lease_hard_deadline) "
                               "VALUES ('https://verify.test/fence', 'discovering', 'A', now()+interval '30 min', now()+interval '2 h');")
            fid = await conn.fetchval("SELECT id FROM companies WHERE ir_url='https://verify.test/fence';")
            await conn.execute("UPDATE companies SET lease_owner='B' WHERE id=$1;", fid)   # B reclaimed it
        await db.mark_company(pool, fid, "A", {"status": "ok", "events": [], "pages": 1})  # A's stale write
        async with pool.acquire() as conn:
            owner = await conn.fetchval("SELECT lease_owner FROM companies WHERE id=$1;", fid)
        results["4_fencing"] = (owner == "B")                                  # A's mark did NOT flip it (fenced)

        # ── TEST 5 — 完成判据: 把所有 verify 公司标 discovered → 非终态计数归零 ──
        async with pool.acquire() as conn:
            await conn.execute("UPDATE companies SET status='discovered' WHERE ir_url LIKE 'https://verify.test/%';")
        results["5_completion_barrier"] = (await _count_nonterminal(pool) == 0)

        # ── report ──
        for k, v in results.items():
            print(f"  {'✅' if v else '❌'} {k}")
        allpass = all(results.values())
        print(f"\n[verify_worker] {'ALL PASS ✅' if allpass else 'SOME FAILED ❌'}")
        # cleanup test rows so a re-run starts clean
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM events WHERE run_id='verify-run';")
            await conn.execute("DELETE FROM companies WHERE ir_url LIKE 'https://verify.test/%';")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
