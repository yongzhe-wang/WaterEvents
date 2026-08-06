-- events.meta_fixed — what stage-2's metadata task actually CHANGED, and what it changed FROM.
--
-- 用一句话讲完: 第一个 VLM 任务(确认/补全 title·date·type)一直在跑,但它的结果从来没被写回 events —— 写库语句只
-- 更新 status / enriched_at / claim_token / media_urls。这一列既让修正真正落库,也让"修了多少条"可数、可审计。
--
-- WHY this was invisible: the writer's UPDATE names four columns and none of them is title/date/event_type
--   {DB_MEDIA.PY "UPDATE EVENTS SET STATUS='ENRICHED', ENRICHED_AT=NOW(), CLAIM_TOKEN=NULL, MEDIA_URLS = (…)"}
-- while the extractor faithfully filled them on an in-memory object that is then discarded
--   {HANDLERS.PY "CHART.CONFIRM_METADATA(CONTRIB.GET(\"TITLE\", \"\"), CONTRIB.GET(\"DATE\", \"\"), …)"}
-- so every ROUTE call has been returning corrections into the void. The cost is visible in production: an event whose
-- title is literally a Cloudflare block page still carries it after enrichment, and 34% of a 47-page labelled sample
-- had an empty or degenerate title (8 empty, 7 literally "html", 1 "Access denied | … used Cloudflare to restrict
-- access") — exactly the population this task exists to repair.
-- [CONFIDENCE: CONFIRMED 100% — the UPDATE statement and the discarded call site were both read from the files named;
--  the 34% is counted over tests/datasets/html_extract_50.]
--
-- WHY store the OLD value rather than a boolean: "we changed 1,200 titles" is not checkable. Keeping what was replaced
-- makes every correction auditable after the fact and makes a bad correction recoverable, at the cost of one short
-- string per fixed field. A boolean would answer the dashboard's question and no other.
--
-- Shape: {"title": "<previous value>", "date": "<previous value>", "type": "<previous value>"} — only the keys that
-- actually changed are present, so `meta_fixed ? 'title'` counts title repairs directly.
--
-- 上游触发: db_media.mark_enriched_media。下游连接: /api/today-media 的队列条。

alter table waterevents.events add column if not exists meta_fixed jsonb;

-- Partial index: the dashboard only ever asks about rows where something WAS fixed, and those are the minority.
create index if not exists events_meta_fixed_idx on waterevents.events using gin (meta_fixed)
  where meta_fixed is not null;

comment on column waterevents.events.meta_fixed is
  'Stage-2 metadata repairs: {"title|date|type": "<value before the repair>"}. Key present = that field was changed.';
