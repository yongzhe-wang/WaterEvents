-- events.status='partial' + events.pending_kinds — 把"走到哪一步"和"还欠什么活"拆成两件事。
--
-- 用一句话讲完: status 一直是个扁平枚举,所以"这个 event 已经富集好了,只是当初有几条车道关着、文档没抓"这件事
-- 没地方表达 —— 唯一的办法是把它整件退回 discovered 重跑一遍。加一个状态 partial(已富集、但还欠活)和一列
-- pending_kinds(欠的是哪几条车道),worker 认领到 partial 就只跑那几种 kind 的 handler,跳过 html 渲染和 VLM。
--
-- WHY this is worth a new state rather than just re-queueing. Every one of the events this exists for ALREADY HAS its
-- html extracted; re-running them whole would re-render pages whose content is already in the database and pay a VLM
-- route call for a routing decision that was already made:
--   {DB 2026-08-10 — 14,893 events carry a `skipped:kind-disabled` document url; 14,893 of 14,893 also carry at least
--    one html url, 18,654 html urls in total, against 26,939 documents actually owed (1.81 per event)}
-- So a whole-event re-run buys 26,939 documents at the price of 18,654 browser renders + 14,893 VLM calls, all of them
-- redundant. [CONFIDENCE: CONFIRMED 100% — counted by joining events to the url ledger on the live database.]
--
-- WHY one state + one array, and NOT a pair of states (docs_pending / docs_running). A lapsed lease has to be returned
-- to the right place, and with a single in-flight status the reaper cannot tell a full pass from a document-only one —
-- which is what pushed me toward a second in-flight status. Making `pending_kinds` the single source of truth removes
-- the need: the two reclaim sites branch on the ARRAY instead of on a status, so `rendering` keeps its one meaning and
-- its one lease. {REAPER.SH:90 "WHERE STATUS='RENDERING' AND LEASE_UNTIL < NOW()"} and the self-reclaim arm of
-- claim_events {EVENTS.PY:212 "OR (STATUS='RENDERING' AND LEASE_UNTIL < NOW())"} each grow one CASE.
--
-- WHY pending_kinds is not premature generality. The second consumer is already sitting in the same table: audio was
-- switched off and left its own backlog behind, in exactly this shape.
--   {DB 2026-08-10 event_media_urls — "AUDIO | SKIPPED | 370" and "VIDEO | PENDING | 1278"}
-- Re-opening that lane needs the identical mechanism, and a `docs_`-prefixed state could not carry it.
-- [CONFIDENCE: CONFIRMED 100% — both counts read from the ledger.]
--
-- WHAT pending_kinds IS NOT: it is not a ledger. It records that an ATTEMPT is owed, not that a document is missing —
-- a document-only pass clears it whether or not the fetch succeeded, because leaving it set on failure would loop the
-- event forever. The truth about what was actually obtained stays in event_media_urls, which now upserts and so can
-- correct itself. Anyone reading pending_kinds as "documents we still lack" will be wrong.
--
-- 上游触发: 一条 UPDATE 登记欠账(见文件末尾的注释), 或将来重开某条车道时同样的一条。
-- 下游连接: claim_events 的认领谓词 + RETURNING, worker 的文档专用分叉, mark_enriched_media 的终态, reaper 的退租。

-- The CHECK has to be replaced rather than extended — Postgres has no "add value" for a check constraint the way it
-- has for an enum type. Dropping and re-adding is atomic inside the migration's implicit transaction.
alter table waterevents.events drop constraint if exists events_status_check;
alter table waterevents.events add constraint events_status_check
  check (status in ('discovered','rendering','enriched','failed','dead_letter','deferred','partial'));

-- NOT NULL DEFAULT '{}' so every existing row reads as "nothing owed" without a backfill pass, and so the reclaim
-- CASE never has to think about NULL. An empty array and NULL would mean the same thing here, and one of them is
-- enough. Postgres stores the default without rewriting the table.
alter table waterevents.events
  add column if not exists pending_kinds text[] not null default '{}';

-- The claim's partial index must learn the new status or `partial` rows are invisible to the planner's index path and
-- the claim degrades to a seq scan over 280k rows. Recreated rather than altered — a partial index's predicate is not
-- mutable. {EVENTS_ENRICH_CLAIMABLE_IDX "USING BTREE (STATUS, NEXT_RETRY_AT) WHERE (STATUS = ANY (ARRAY[
--  'DISCOVERED','RENDERING','FAILED']))"}
drop index if exists waterevents.events_enrich_claimable_idx;
create index events_enrich_claimable_idx on waterevents.events (status, next_retry_at)
  where status in ('discovered','rendering','failed','partial');

-- Nothing is enrolled by this migration. Turning 14,893 rows into `partial` is a separate, deliberate act, and it must
-- happen AFTER the queue ordering work — enrolled first, they enter the queue as an unordered heap sorted by nothing,
-- which is the same shape that serialised the fleet behind one host on 2026-08-10:
--   {DB 2026-08-10 — 39 of the in-flight events were all www.vodafone.com at enrich_priority=8; throughput fell from
--    884 docs/h to 144 docs/h until the batch was capped to 2 per host}
-- The enrolling statement, for when that ordering exists:
--   update waterevents.events e
--   set status='partial', pending_kinds=array['pdf','xlsx','docx','pptx'], claim_token=null, lease_until=null
--   where e.status='enriched' and exists (
--     select 1 from waterevents.event_media_urls u
--     where u.event_id=e.id and u.reason='skipped:kind-disabled'
--       and u.kind in ('pdf','xlsx','docx','pptx'));
