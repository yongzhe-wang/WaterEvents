-- event_media_urls gains `reason` — the FULL outcome string, next to the constrained `status` enum.
--
-- 用一句话讲完: 台账的 status 只允许 4 个值,于是 `skipped:kind-disabled` 在写库那一刻被截成 `skipped`,理由丢了。
-- 加一列存全文:status 继续当枚举用(可索引、可聚合),reason 回答「为什么」。
--
-- WHY this is a bug and not a nitpick: the writer's own docstring promises the detail —
--   {DB_MEDIA.PY "`url_status` = Chart's ledger {url: status} so the ledger records what ACTUALLY happened per
--    resource ('done' / 'failed:…' / 'skipped:…') instead of stamping every row 'done'"}
-- but the column forbids it —
--   {MIGRATION 20260723145355 "STATUS TEXT NOT NULL DEFAULT 'PENDING' CHECK (STATUS IN ('PENDING','DONE','FAILED','SKIPPED'))"}
-- so the code has to truncate to satisfy the constraint
--   {DB_MEDIA.PY "ST = NEXT((S FOR S IN (\"DONE\", \"FAILED\", \"SKIPPED\", \"PENDING\") IF RAW.STARTSWITH(S)), \"DONE\")"}
-- and the ledger cannot tell "we chose not to run this lane" from "this is a mailto link". Both read `skipped`.
-- [CONFIDENCE: CONFIRMED 100% — the promise, the constraint and the truncation were each read from the files named.]
--
-- WHY a second column rather than widening the CHECK: an unconstrained status would let any string in and the
-- dashboard's GROUP BY status would fragment into dozens of buckets. Keeping the enum tight and putting the detail
-- beside it preserves both aggregation and explanation.
--
-- 上游触发: db_media.mark_enriched_media。下游连接: /api/artifact?kind=urls 的台账表,以及 Today · Media 的 URL ledger 卡。

alter table waterevents.event_media_urls add column if not exists reason text;

comment on column waterevents.event_media_urls.reason is
  'Full outcome string from the Chart ledger, e.g. skipped:kind-disabled, failed:misrouted-binary:file-magic. '
  'status is the coarse enum for aggregation; this is the why.';
