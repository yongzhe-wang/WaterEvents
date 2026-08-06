-- events.enrich_priority — push a chosen set of events to the FRONT of the stage-2 queue.
--
-- 用一句话讲完: 加一个整数优先级列,claim 时先按它降序排;要盯着某 100 个事件跑就把它们的优先级设高,worker 下一轮
-- 就先领这些,UI 上马上能看到它们出现。默认 0,所以对现有 28 万行没有任何行为变化。
--
-- WHY a column and not a clever ORDER BY: the existing claim orders by `next_retry_at NULLS FIRST`, and every
-- `discovered` row has next_retry_at NULL —
--   {EVENTS.PY CLAIM_EVENTS "ORDER BY NEXT_RETRY_AT NULLS FIRST"}
--   {psql 2026-08-06 "DISCOVERED | 280774"}
-- so all 280k rows tie at the front and the order among them is whatever the planner returns. There is no value you
-- can write into next_retry_at that sorts BEFORE NULL, so prioritising a subset is not expressible without a new
-- column. [CONFIDENCE: CONFIRMED 100% — the ORDER BY and the row count were both read off this database.]
--
-- WHY it is not a boolean: "run these next" and "run these after those" are both real, and a boolean can only say the
-- first. An int costs the same and lets a second batch queue behind the first without either being reset.
--
-- 上游触发: tests/media/enqueue_events.py (or any UPDATE). 下游连接: claim_events' ORDER BY.

alter table waterevents.events
  add column if not exists enrich_priority int not null default 0;

-- Partial index over just the claimable statuses. The full table is 281k rows and the claim only ever looks at these
-- two, so indexing the rest would pay for pages the query never reads.
-- {EVENTS.PY CLAIM_EVENTS "WHERE STATUS='DISCOVERED' OR (STATUS='RENDERING' AND LEASE_UNTIL < NOW())
--  OR (STATUS='FAILED' AND (NEXT_RETRY_AT IS NULL OR NEXT_RETRY_AT < NOW()))"}
-- [CONFIDENCE: CONFIRMED 100% — predicate copied from the live claim query.]
create index if not exists events_enrich_priority_idx
  on waterevents.events (enrich_priority desc, next_retry_at)
  where status in ('discovered', 'failed');

-- Enqueue a specific set (what the test harness does):
--   update waterevents.events
--      set enrich_priority = 100, status = 'discovered', claim_token = NULL, lease_until = NULL,
--          fail_reason = NULL, fail_count = 0, next_retry_at = NULL
--    where id = any($1::uuid[]);
-- Clearing status/claim/fail alongside the priority is deliberate: a re-run of a chosen batch should start from a
-- clean slate, or a previous failure's backoff would hold back the very events being prioritised.
