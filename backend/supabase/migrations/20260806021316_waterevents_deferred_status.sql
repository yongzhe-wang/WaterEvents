-- events.status gains 'deferred' — work we CHOSE not to do yet, kept distinct from work that was attempted and failed.
--
-- 用一句话讲完: 当一个事件的所有 url 都属于当前关掉的抽取车道(MEDIA_KINDS 门控),它既不该记成 failed(那是把我们的
-- 配置决定赖给来源,而且会走重试通道空转),也不该记成 enriched(我们根本没读)。给它一个**不参与 claim 的状态**,
-- 以后打开那条车道时一条 UPDATE 就能全部放回队列。
--
-- WHY a new status rather than reusing `failed`: fail_event bumps fail_count and, on the third failure, sends the row to
-- `dead_letter` —
--   {EVENTS.PY "STATUS = CASE WHEN FAIL_COUNT + 1 >= 3 THEN 'DEAD_LETTER' ELSE 'FAILED' END"}
-- and dead_letter is NOT matched by the claim predicate —
--   {EVENTS.PY CLAIM_EVENTS "WHERE STATUS='DISCOVERED' OR (STATUS='RENDERING' AND LEASE_UNTIL < NOW())
--    OR (STATUS='FAILED' AND (NEXT_RETRY_AT IS NULL OR NEXT_RETRY_AT < NOW()))"}
-- so a deferred event would be re-claimed and re-deferred three times, then STRANDED permanently: re-enabling the pdf
-- lane later would never pick it up again. Observed within 45 seconds of turning the gate on:
--   {psql 2026-08-06 "FAILED | DEFERRED:KIND-DISABLED | 40"}
-- [CONFIDENCE: CONFIRMED 100% — the constraint, the claim predicate and the 40 mislabelled rows were all read off this
--  database; the stranding follows from dead_letter's absence from the claim predicate.]
--
-- WHY it must not be claimable: `discovered` is claimed UNCONDITIONALLY (the claim predicate checks next_retry_at only
-- for `failed`), so parking a deferred event back in `discovered` with a future retry time would loop immediately.
-- A status the predicate does not mention is the only shape that means "owed, but not now".
--
-- 上游触发: worker.py 的 kind-gate 分支。下游连接: 重新打开车道时的一条 UPDATE(见文件末尾的注释)。

alter table waterevents.events drop constraint if exists events_status_check;

alter table waterevents.events add constraint events_status_check
  check (status = any (array[
    'discovered'::text,   -- never enriched, claimable now
    'rendering'::text,    -- claimed by a worker, lease held
    'enriched'::text,     -- done, has documents
    'failed'::text,       -- ATTEMPTED and did not produce usable content; retried with backoff
    'dead_letter'::text,  -- failed 3x; human review
    'deferred'::text      -- NOT attempted, by our own configuration; not claimable, not a failure
  ]));

-- Repair the rows the first gated run mislabelled. They were never attempted, so fail_count must come back down too —
-- leaving it at 1 would give them a shortened retry budget the moment their lane is switched on.
update waterevents.events
   set status = 'deferred', fail_count = 0, next_retry_at = null
 where status in ('failed', 'dead_letter')
   and fail_reason = 'deferred:kind-disabled';

-- Re-enabling a lane is one statement, e.g. after MEDIA_KINDS gains pdf:
--   update waterevents.events set status='discovered', fail_reason=null where status='deferred';
-- Doing it wholesale is correct: the gate re-evaluates every url on the next pass, so an event whose kinds are still
-- disabled simply defers again, at the cost of one cheap re-classification and no fetch.
