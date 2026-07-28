-- 20260727095145_waterevents_drop_legacy_company_cols.sql
-- 用一句话讲完: 统一调度器落地后, companies 表上那套「公司级 lease + 每公司结果计数」的列就再没有代码读或写了 ——
-- 认领状态搬到了 work_queue(status/lease_owner/lease_until/attempt), 每次扫描的结果计数搬到了 work_queue
-- .last_event_count/last_render_pages/last_vlm_calls, 事件真实数量由 event_companies view 现算 —— 这条 migration 把
-- 那 9 个僵尸列删掉, 让「表里有的字段」重新等于「系统真正在用的字段」。
--
-- WHY 必须删而不是留着: 这些列不是空的, 是**存着过时的错值**, 会主动误导任何直接查 companies 的人。最典型的是
-- event_count —— 全表加起来 162, 而 events 表实际有 124,811 行; 按 status 分组去看 avg(event_count) 会得到一片 0.00,
-- 读起来就像"全库没有事件"。本次排查第一轮正是被它骗过一次, 差点得出完全错误的结论。留着一个恒假的字段, 成本不是
-- 磁盘, 是每个后来者都要重新踩一次坑。
-- {MEASURED 2026-07-27 "SUM(COMPANIES.EVENT_COUNT) = 162 VS COUNT(*) FROM EVENTS = 124811"}
-- {MEASURED 2026-07-27 "SELECT STATUS, AVG(EVENT_COUNT) ... → DISCOVERED 0.00 / DISCOVERED_PARTIAL 0.00"}
-- [CONFIDENCE: CONFIRMED 100% — 写这些列的 claim_company/renew_lease/mark_company/fail_company/reconcile 已于
--  commit 4da7eb5 删除, 删除前全仓 grep 确认零生产调用方].
--
-- 删除前已把这 9 列连同 id 全量导出到本地 TSV(2,785 行)作为回退依据; 列可回加, 数据可回灌。
--
-- 保留哪些、为什么(这三列看起来像同类, 但都不是僵尸):
--   • status / run_id     —— scan.py:35 仍在写: `INSERT INTO companies (ir_url, status, run_id) VALUES ($1,
--                            'discovering', $2)`。虽然没有任何地方再读它们, 但删列会直接打断这条 INSERT, 且
--                            tests/event/e2e/{enqueue,cleanup}_killerdeal.py 用 run_id 圈定测试集。要清得先改代码,
--                            属于独立一步, 不塞进这条 migration。
--   • ir_url_confidence   —— 确实没有读者也没有写者, 但它存着 2,763 行**正确的** IR-URL 置信度数据(来自早期
--                            ir_url_agent 发现流程), 性质是"暂时没用上的真数据", 不是"恒假的坏计数器"。删它是丢信息,
--                            不是去噪 —— 需要单独确认。
--
-- 连带效果: companies_claimable_idx 建在 (status, lease_until) 上, lease_until 一删该索引自动消失。这正是我们要的 ——
-- 它服务的是已被删除的 claim_company 查询。companies_status_check 只依赖 status, 不受影响。
-- {PG_INDEXES 2026-07-27 "COMPANIES_CLAIMABLE_IDX ... USING BTREE (STATUS, LEASE_UNTIL) WHERE (STATUS = ANY (...))"}

BEGIN;

-- 公司级 lease 机制 —— 认领/续租/防脑裂全部搬到 work_queue, 这四列在 companies 上已无任何读写方。
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS lease_owner;
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS lease_until;          -- 连带 drop companies_claimable_idx
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS lease_hard_deadline;
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS attempt;              -- 重试计数现在是 work_queue.attempt

-- 每公司结果计数 —— 由 mark_company 写入, 该函数已删; 真实数量改由 event_companies view 现算 count(e.id)。
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS event_count;          -- 恒假计数器: 162 vs 真实 124,811
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS pages;
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS failed_render;
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS failed_extract;
ALTER TABLE waterevents.companies DROP COLUMN IF EXISTS trace_dir;            -- trace 路径现在只出现在 worker 日志里

COMMIT;
