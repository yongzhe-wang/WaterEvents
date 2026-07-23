"""event_agent.db — the DISCOVERY worker's Postgres layer: claim a company, batch-flush its events idempotently,
renew the lease, mark it done, and reconcile crashed workers.

用一句话讲完: worker 从 companies 队列 SKIP-LOCKED 抢一家公司(拿 lease)→ crawl_company 内存跑完 → 把 events 用
`INSERT ... SELECT unnest(...) ON CONFLICT DO UPDATE(合并 media)` 批量幂等落 events 表 → 翻 companies.status →
崩溃的话 lease 过期被 reconcile 回收重认领。**这层只管"活怎么安全地领、落、收",不含任何 event 抽取逻辑**(那在
extract.py)—— 换 DB / 换队列不动 crawl。

WHY asyncpg + Supavisor transaction pooler: 2000+ 公司 × N worker 会打爆 Postgres 直连;transaction-mode pooler 把
连接 multiplex 收敛。transaction mode 不支持 server-side prepared statements → 必须 statement_cache_size=0。
{RESEARCH wv2d0n0v3 "Supavisor transaction mode (6543) ... asyncpg 设 statement_cache_size=0"}
[CONFIDENCE: CONFIRMED 100% — prepared-statement caching is incompatible with a transaction-mode pooler that rotates
backends per transaction].
"""
from __future__ import annotations

import json
import os

import asyncpg

from .urls import _canon                                    # the exact canonical-url key the BFS dedups on (stdlib-only, no heavy import chain)

# The Supavisor transaction-mode pooler DSN (port 6543), from env so no secret is hard-coded. The worker NEVER opens a
# session-mode direct connection at 2000-company scale. {RESEARCH "全部走 Supavisor transaction-mode pooler ... 防连接耗尽"}.
_DSN = os.environ.get("WATEREVENTS_DB_DSN", "")

# lease knobs — the soft lease is renewed by a heartbeat during the (possibly ~20 min) crawl; the hard deadline is an
# absolute cap so a wedged worker can't hold a company forever even if its heartbeat keeps firing.
# {DESIGN wlkrnxklp "lease_hard_deadline ... 防单公司永久占 worker"}.
LEASE_MIN = int(os.environ.get("WATEREVENTS_LEASE_MIN", "30"))          # soft lease minutes (heartbeat renews)
LEASE_HARD_H = int(os.environ.get("WATEREVENTS_LEASE_HARD_H", "2"))     # absolute hold cap, hours
FLUSH_BATCH = int(os.environ.get("WATEREVENTS_FLUSH_BATCH", "25"))      # events per batch INSERT {DESIGN "events 攒 25 行"}


async def connect_pool(min_size: int = 1, max_size: int = 4) -> asyncpg.Pool:
    """Open the asyncpg pool against the Supavisor transaction pooler. statement_cache_size=0 is MANDATORY (transaction
    mode rotates the backend per tx, so a cached prepared statement points at the wrong session → 'prepared statement
    does not exist')."""
    if not _DSN:
        raise RuntimeError("WATEREVENTS_DB_DSN not set — point it at the Supabase Supavisor pooler (port 6543).")
    return await asyncpg.create_pool(_DSN, min_size=min_size, max_size=max_size, statement_cache_size=0)


async def claim_company(pool: asyncpg.Pool, worker_id: str, run_id: str) -> asyncpg.Record | None:
    """Atomically claim ONE claimable company (queued, OR discovering-but-lease-expired) and mark it `discovering` with
    a fresh lease. SKIP LOCKED means N concurrent workers never fight over the same row — each gets a distinct company
    or None. Returns the claimed row (id, ir_url, attempt), or None when the queue is drained.
    {RESEARCH wv2d0n0v3 "batch-claim ... UPDATE ... SKIP LOCKED ... RETURNING"} — here LIMIT 1 (a worker owns one company)."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            UPDATE companies SET
                status = 'discovering',
                lease_owner = $1,
                lease_until = now() + ($2 || ' minutes')::interval,
                lease_hard_deadline = now() + ($3 || ' hours')::interval,
                attempt = attempt + 1,
                run_id = $4,
                updated_at = now()
            WHERE id = (
                SELECT id FROM companies
                WHERE status = 'queued'
                   OR (status = 'discovering' AND lease_until < now())   -- reclaim a crashed worker's company
                ORDER BY updated_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, ir_url, attempt;
            """,
            worker_id, str(LEASE_MIN), str(LEASE_HARD_H), run_id,
        )


async def renew_lease(pool: asyncpg.Pool, company_id, worker_id: str) -> bool:
    """Heartbeat: push the soft lease forward while the crawl runs — but ONLY if we still own it (lease_owner match) AND
    we're inside the hard deadline. Returns False if we've lost the lease (someone reclaimed us / hit the hard cap) so
    the worker can abort instead of writing into a company another worker now owns."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE companies SET lease_until = now() + ($3 || ' minutes')::interval, updated_at = now()
            WHERE id = $1 AND lease_owner = $2 AND now() < lease_hard_deadline
            RETURNING id;
            """,
            company_id, worker_id, str(LEASE_MIN),
        )
        return row is not None


def _dedup_key(urls: list[str]) -> str:
    """An event's STABLE cross-recrawl identity = the canonical of its PRIMARY url (urls[0] = the detail page). NOT the
    whole url set, which grows as media is discovered — a key that changed with media would defeat ON CONFLICT and
    duplicate the event on re-crawl. {MIGRATION events.dedup_key comment}."""
    return _canon(urls[0]) if urls else ""


async def flush_events(pool: asyncpg.Pool, company_id, run_id: str, events: list[dict]) -> int:
    """Idempotently persist a company's events in batches. Uses `INSERT ... SELECT unnest(...)` (one typed array per
    column — the 5x-faster wide-batch shape) with `ON CONFLICT (company_id, dedup_key) DO UPDATE` that MERGES media_urls
    (union, dedup) rather than DO NOTHING — so an event re-discovered from a second route (or a re-crawl) gains its extra
    media instead of being dropped. {RESEARCH "INSERT...SELECT unnest() ... 5.02x"; ADVERSARIAL "DO UPDATE SET MEDIA_URLS
    = ... || EXCLUDED.MEDIA_URLS 不丢 route 补的 media"}. Returns rows attempted."""
    written = 0
    for i in range(0, len(events), FLUSH_BATCH):
        batch = events[i:i + FLUSH_BATCH]
        keys, titles, dates, types, medias = [], [], [], [], []
        seen_in_batch = set()                                # ON CONFLICT can't catch dups WITHIN one INSERT → dedup here
        for e in batch:
            k = _dedup_key(e.get("urls") or [])
            if not k or k in seen_in_batch:                  # an event with no url shouldn't exist (extract drops it), skip defensively
                continue
            seen_in_batch.add(k)
            keys.append(k)
            titles.append(e.get("title") or "")
            dates.append(e.get("date") or "")
            types.append(e.get("type") or "")
            medias.append(json.dumps(e.get("urls") or []))   # jsonb array as text, cast below
        if not keys:
            continue
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (company_id, run_id, dedup_key, title, event_date, event_type, media_urls)
                SELECT $1, $2, k, t, d, ty, m::jsonb
                FROM unnest($3::text[], $4::text[], $5::text[], $6::text[], $7::text[]) AS x(k, t, d, ty, m)
                ON CONFLICT (company_id, dedup_key) DO UPDATE SET
                    media_urls = (
                        SELECT coalesce(jsonb_agg(DISTINCT u), '[]'::jsonb)      -- union existing + new, dedup
                        FROM jsonb_array_elements(events.media_urls || excluded.media_urls) AS u
                    );
                """,
                company_id, run_id, keys, titles, dates, types, medias,
            )
        written += len(keys)
    return written


async def mark_company(pool: asyncpg.Pool, company_id, worker_id: str, result: dict) -> None:
    """Flip the company to its terminal discovery status once the crawl finished + events are flushed. `discovered` only
    when the crawl was clean; `discovered_partial` when fail-loud counters are nonzero (pages dropped) so downstream
    NEVER mistakes a degraded run for the whole truth. Fencing on lease_owner: if we lost the lease mid-crawl this UPDATE
    matches nothing (another worker owns it now) → we don't clobber their result. {CRAWL.PY status ok/incomplete}."""
    status = "discovered" if result.get("status") == "ok" else "discovered_partial"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE companies SET
                status = $3, lease_owner = NULL, lease_until = NULL,
                event_count = $4, pages = $5, failed_render = $6, failed_extract = $7,
                trace_dir = $8, updated_at = now()
            WHERE id = $1 AND lease_owner = $2;
            """,
            company_id, worker_id, status,
            len(result.get("events") or []), result.get("pages") or 0,
            result.get("failed_render") or 0, result.get("failed_extract") or 0,
            result.get("trace_dir") or "",
        )


async def fail_company(pool: asyncpg.Pool, company_id, worker_id: str, reason: str) -> None:
    """Mark a company `failed` when the crawl itself raised (not a partial — a hard error). Fenced on lease_owner."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE companies SET status='failed', lease_owner=NULL, lease_until=NULL, trace_dir=$3, updated_at=now() "
            "WHERE id=$1 AND lease_owner=$2;",
            company_id, worker_id, f"ERROR: {reason}"[:500],
        )


async def reconcile(pool: asyncpg.Pool) -> int:
    """Sweeper (run by a cron): reclaim companies stuck in `discovering` whose lease expired (crashed worker). Flips them
    back to `queued` so the next claim picks them up + re-crawls (idempotent via ON CONFLICT). Returns rows reclaimed.
    {ADVERSARIAL "兜底扫描 ... 所有非终态且无活跃 lease 的行"} — for companies the only non-terminal state is `discovering`."""
    async with pool.acquire() as conn:
        # execute() returns the command tag "UPDATE N"; parse N so the caller gets the true reclaimed count (fetchrow
        # would only surface the first row while the UPDATE still hits all matches — a misleading 1/0).
        tag = await conn.execute(
            "UPDATE companies SET status='queued', lease_owner=NULL, lease_until=NULL, updated_at=now() "
            "WHERE status='discovering' AND lease_until < now();"
        )
        return int(tag.split()[-1]) if tag.startswith("UPDATE") else 0
