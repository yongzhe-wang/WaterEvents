-- 20260726040000_waterevents_hashgate_metrics.sql
-- 用一句话讲完: 给 pages 加 content_hash(scan render 完算 sha256 → 和上次比,没变就 SKIP VLM = 双瓶颈里省 VLM 的关键杠杆)、
-- 给 work_queue 加 duration_s/last_render_pages/last_vlm_calls(每次 scan 的耗时+资源用量,喂 finish-time 估计)、建 scan_log
-- 追加表(每次 scan 一行:render_pages/vlm_calls/vlm_skipped/events/duration → 滑窗算 C_R(pages/hr)、C_V(calls/hr)、
-- hit_rate(hash 变更率) → packing solver 用这三个实测量动态解出 incremental 最优周期 T*)。
-- WHY: 单块 A5000 上 VLM 每 op 比 render 贵 ~7×,是全局 binding 瓶颈;不加 hash-gate 则 full 整周 sweep 需 ~446 VLM-hr >> 168hr
-- 不可行。hash-gate 让 render(便宜、算 hash)和 VLM(贵、只在内容变时 fire)解耦,系统才可行。
-- {USER 2026-07-26 "two bottleneck render and vlm, calculate usage for parallel; best rotation so full fits perfectly"}
-- [CONFIDENCE: CONFIRMED — 本 session 双资源模型 + packing 方程推导, 用户批准 START].

-- ── pages.content_hash —— hash-gate 的存储点。scan 时算 sha256(render text) 和这列比;save_pages 写入最新 hash ──
-- 没有 hash 的老行(content_hash IS NULL)→ gate 视为「首次见/变了」→ 照常 extract(fail-open, 绝不漏真事件)。
ALTER TABLE waterevents.pages ADD COLUMN IF NOT EXISTS content_hash text;

-- ── work_queue 三个 last-value 埋点 —— finish-time 估计的 per-unit EWMA 原料 (full 和 incremental 成本差 ~20×, 分开算) ──
ALTER TABLE waterevents.work_queue ADD COLUMN IF NOT EXISTS duration_s        double precision;  -- 上次 scan 墙钟秒数
ALTER TABLE waterevents.work_queue ADD COLUMN IF NOT EXISTS last_render_pages int;               -- 上次 render 了几页
ALTER TABLE waterevents.work_queue ADD COLUMN IF NOT EXISTS last_vlm_calls    int;               -- 上次真发了几次 VLM extract

-- ── scan_log —— 追加式吞吐日志。每完成一个 unit 追加一行;滑窗聚合 → C_R / C_V / hit_rate 三个 solver 输入 ──
-- WHY 追加表(不是 work_queue 的 last-value 列): 速率(pages/hr, calls/hr)必须按时间窗求和/求差, last-value 每行只留最新
-- 一次覆盖掉历史 → 无法算窗口速率。scan_log 保留每次 scan 的时间戳 + 资源量 → sum over window / window_hours = 稳态速率。
CREATE TABLE IF NOT EXISTS waterevents.scan_log (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),   -- scan 完成时刻 → 滑窗按此过滤
  unit_type    text NOT NULL,                         -- 'full' | 'incremental' (成本差 20×, 分开聚合)
  url          text,                                  -- 扫的 hub / seed url (审计用)
  render_pages int NOT NULL DEFAULT 0,                -- 这次 render 了几页 → Σ/window = C_R (pages/hr)
  vlm_calls    int NOT NULL DEFAULT 0,                -- 这次真发了几次 VLM extract → Σ/window = C_V (calls/hr)
  vlm_skipped  int NOT NULL DEFAULT 0,                -- hash 没变、被 gate 跳过的页数 → hit_rate = vlm_calls/(vlm_calls+vlm_skipped)
  events       int NOT NULL DEFAULT 0                 -- 这次抽出几个 event (监控用)
);

-- 滑窗聚合的热路径: WHERE ts > now()-interval ORDER BY ts → 索引 ts 让窗口过滤 index-only。
CREATE INDEX IF NOT EXISTS scan_log_ts_idx ON waterevents.scan_log (ts);
