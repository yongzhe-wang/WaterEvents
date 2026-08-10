-- events.queue_pos — 一个池子, 按 host 发牌。让"谁先被处理"变成排序的数学性质, 而不是靠人调优先级。
--
-- 用一句话讲完: 同优先级的 18 万行现在是个**无序堆**(每一行 next_retry_at 都是 NULL, 全部并列), 于是"哪一行
-- 先跑"是 planner 说了算。加一列 queue_pos, 它的整数部分是"第几轮"、小数部分是稳定的 host 抖动 —— 第 1 轮每个
-- host 各出一张牌, 第 2 轮再来一遍。队头永远是 3,107 个不同 host 各一行。
--
-- WHY this column has to exist. Two failures, both measured, both caused by the same absence of an order:
--
--   1. A re-queued row is never reached. Enrolling 216 events back to `discovered` at the default priority left them
--      untouched for half an hour while the fleet worked at full rate — they were tied with 168k others and the tie
--      is broken by nothing. {DB 2026-08-10 — "requeued_left" held at 216 across five samples, 10 minutes apart,
--      while media_docs_1h climbed 320 → 372.}
--   2. Escaping (1) with enrich_priority produces the opposite failure. Lifting 313 events to priority 8 put them at
--      the head — and 421 of their 539 documents were on ONE host, so 39 of the in-flight events were all
--      www.vodafone.com, each worker holding a slot while it waited its turn at that host's politeness cursor.
--      {DB 2026-08-10 — 39 in-flight events, all www.vodafone.com at enrich_priority=8; throughput 884 → 144 docs/h,
--       recovering to 444 within minutes of capping the batch to 2 per host.}
--
-- So an unordered queue starves a re-queued row, and the only tool for un-starving it manufactures head-of-line
-- blocking. Dealing the cards by host removes both at once, and removes them BY CONSTRUCTION rather than by a runtime
-- cap that someone has to remember to set. [CONFIDENCE: CONFIRMED 100% — both incidents were measured on this
-- database on 2026-08-10, hours apart.]
--
-- THE FORMULA (computed by the pacer, see solver/pacer.py::_respace_events):
--     seq_in_pop = row_number() OVER (PARTITION BY host, is_backfill ORDER BY created_at, id)
--     n_in_pop   = count(*)     OVER (PARTITION BY host, is_backfill)
--     n_in_host  = count(*)     OVER (PARTITION BY host)
--     slot       = (seq_in_pop - 0.5) * n_in_host / n_in_pop
--     jitter     = (abs(hashtext(host)) % 1024) / 1024.0
--     queue_pos  = slot + jitter
-- Three properties, all of them consequences of the arithmetic rather than tunables:
--   • Round k holds one row from every host with at least k rows. The biggest host holds 1,661 of 183,589 rows
--     (0.90%), so at the head of the queue its density is 1/3107.
--   • Within a host the two populations are interleaved at their own local ratio — that is what n_in_host/n_in_pop
--     does — so the backfill appears at its natural rate (one row in 12.3) instead of in a block.
--   • jitter keeps each round's internal order stable and host-scattered, so the queue does not walk the alphabet.
--
-- WHY host and not company_id. Politeness is keyed on the URL's host. The pool holds 3,107 hosts against 4,077
-- companies, i.e. some hosts serve several companies — partitioning by company would hand one host two slots in the
-- same round and reopen exactly the gap this closes.
--
-- KNOWN LIMIT: the host here is the EVENT's source_url host, and an event's documents may live elsewhere (a Q4 site's
-- pdfs on d18rn0p25nwr6d.cloudfront.net). Same-CDN clumping is not covered. The render service already protects its
-- own fetch lane from that {SERVICE.PY "POLITENESS FIRST, THEN THE SLOT ... A SLOT SPENT SLEEPING IS A SLOT NO OTHER
-- HOST CAN USE"}; what remains exposed is the upstream media worker slot.
--
-- 上游触发: pacer 每 tick 重算(只写真的变了的行)。下游连接: claim_events 的 ORDER BY。
alter table waterevents.events
  add column if not exists queue_pos double precision;

-- The claim reads (enrich_priority DESC, then position). enrich_priority stays FIRST so a deliberate human batch can
-- still jump the queue; queue_pos replaces the tie-break that was doing nothing. Partial index over the claimable
-- statuses only — the table is 280k rows and the claim never looks outside these four.
create index if not exists events_queue_order_idx on waterevents.events
  (enrich_priority desc, queue_pos)
  where status in ('discovered','rendering','failed','partial');

-- NULL sorts LAST in the claim's ORDER BY, deliberately: a row the pacer has not yet positioned sinks below every
-- positioned row instead of jumping ahead of them. New rows therefore wait at most one pacer tick for a position,
-- and a pacer outage degrades to "new work waits" rather than "new work floods the head".
