-- event_documents.updated_at — WHEN this document was last (re)extracted, as distinct from when it first appeared.
--
-- 用一句话讲完: 这张表只有 created_at, 而写入是 ON CONFLICT (event_id, url) DO UPDATE —— 重跑一个事件会改写
-- md/blocks/via, 却不动 created_at。于是"最近 10 分钟产出了多少文档"这个问题, 对重跑的部分永远答 0。
--
-- WHY it matters, concretely: this is not a cosmetic gap, it produced a false alarm and nearly a false diagnosis.
-- While the media fleet was draining a re-queued batch, html documents read as ZERO for four consecutive 10-minute
-- windows, which looked exactly like a broken html path:
--   {2026-08-10 04:20/04:30/04:40/04:50 buckets on created_at — html 0 | 0 | 0 | 0, against 27-28 in the two buckets
--    before them}
-- The html path was fine. Those events' html rows already existed, so the upsert refreshed their content and left
-- created_at at its original value; only the NON-html rows were genuinely new (they had failed before the fetch fixes
-- landed, so no row existed to update) and therefore only they showed up:
--   {SAME WINDOW, three events read individually — 8659f91a… docs "html:trafilatura, pdf:coords, pdf:coords,
--    xlsx:xlrd-fallback, xlsx:xlrd-fallback"; 9fb148ac… docs "html:trafilatura, pdf:coords, xlsx:xlrd-fallback"}
-- [CONFIDENCE: CONFIRMED 100% — the zero buckets and the html documents those very events carry were read from the
--  same database minutes apart.]
--
-- Same class as the ledger's ON CONFLICT DO NOTHING fixed earlier the same day: a record that cannot reflect re-work
-- reads as "nothing happened". Backfilled to created_at so existing rows keep a sane ordering rather than a NULL.
--
-- 上游触发: db_media.upsert (媒体 worker 每次写文档)。下游连接: /api/today-media 的产出速率, 以及任何按时间窗
-- 统计抽取吞吐的查询 —— 它们现在必须用 updated_at 才能把重跑算进去。
alter table waterevents.event_documents
  add column if not exists updated_at timestamptz not null default now();

update waterevents.event_documents set updated_at = created_at where updated_at > created_at;

-- The throughput queries scan a recent window and group by kind; this is the index that shape wants.
create index if not exists event_documents_updated_at_idx
  on waterevents.event_documents (updated_at desc, kind);
