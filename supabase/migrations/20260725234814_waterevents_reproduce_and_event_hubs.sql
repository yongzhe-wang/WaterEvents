-- 20260725234814_waterevents_reproduce_and_event_hubs.sql
-- 用一句话讲完: 把生产库里手建(out-of-band)的对象补进版本控制, 让 fresh `supabase db push` 能重建 live 现状
-- (消除 E2/E3/E4/E5 的 false-green), 并给 canonical waterevents.companies 加 event_hubs 多-URL seed 列作 ir_url_agent 落点。
-- WHY: legacy-db scan 发现 waterevents.pages / event_companies view / events.source_url / companies.ticker 全靠手工 SQL
-- 建在生产库、任何 tracked migration 里零 CREATE → 干净库一 `db push` 就 `relation/column does not exist`;
-- event_hubs 那 2403 行还压在 07-22 冻结的 public.companies(死表), 往 waterevents 写 event_hubs 会写到没这列的表。
-- {SCAN whn86f4f7 "waterevents.pages 表无 migration, 手建"; "event_companies 零 create view"; "events.source_url out-of-band ALTER";
--  "event_hubs 落在 public.companies 一个列上, 往 waterevents 写会写到没这列的表 = false-green"}
-- [CONFIDENCE: CONFIRMED 100% — live `\d` 亲验: pages/event_companies/source_url/ticker 全在生产库但不在任何 migration 文件]

-- ── E2: waterevents.pages —— discovery 的 source-page 落点 (frontend "Source page" 读它), 之前手建无 migration ──
-- 列/约束精确匹配 live `\d waterevents.pages`。CREATE ... IF NOT EXISTS → 对现库幂等(表已在, 不重建), 对 fresh 库补齐。
CREATE TABLE IF NOT EXISTS waterevents.pages (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),   -- live: id uuid pk gen_random_uuid()
  company_id uuid,                                         -- FK-ish → waterevents.companies.id (live 无显式 FK)
  run_id     text,                                         -- 哪次 run 抓的
  url        text NOT NULL,                                -- 页 URL
  content    text,                                         -- 渲染后正文
  n_chars    integer,                                      -- 正文字数
  created_at timestamptz DEFAULT now(),
  UNIQUE (company_id, url)                                 -- save_pages 的 ON CONFLICT(company_id,url) 幂等依赖此 unique
);

-- ── E4: events.source_url —— 每条 event 的来源页 URL (frontend "Source page"), 之前 out-of-band ALTER 加的 ──
-- 已在 live(scan E4), 此处补进版本控制; ADD COLUMN IF NOT EXISTS → 幂等。
ALTER TABLE waterevents.events ADD COLUMN IF NOT EXISTS source_url text;

-- ── E5: companies.ticker —— 之前 out-of-band ALTER, 由 trg_set_ticker 触发器在 INSERT 时填 ──
-- 已在 live(scan E5), 补进版本控制; frontend /api/today + /api/companies select ticker 依赖它。
ALTER TABLE waterevents.companies ADD COLUMN IF NOT EXISTS ticker text;

-- ── ir_url_agent 的 multi-URL seed 落点 —— canonical waterevents.companies 加 event_hubs + ir_url 元数据列 ──
-- WHY: event_hubs 原只在 07-22 冻结的 public.companies(死表, 2403 行非空); 迁到 canonical 让 ir_url_agent 有地方写、
-- 让 crawl 能从一家的多个 IR 入口 URL 同时起爬 (multi-frontier)。element 语义见 ir_url_agent/db.py 注释。
-- {SCAN whn86f4f7 "orphan 列 event_hubs ... waterevents.companies 无对应列 ... 数据孤岛风险" — 决策点 #3 用户拍板: 回填保留}
ALTER TABLE waterevents.companies ADD COLUMN IF NOT EXISTS event_hubs jsonb NOT NULL DEFAULT '[]'::jsonb;   -- 多 URL 数组
ALTER TABLE waterevents.companies ADD COLUMN IF NOT EXISTS ir_url_source text;        -- 'ir_url_agent' / 'fmp' / 'manual-...' 出处
ALTER TABLE waterevents.companies ADD COLUMN IF NOT EXISTS ir_url_confidence text;    -- high|medium|low (legacy 兼容, 新 agent 不写)
ALTER TABLE waterevents.companies ADD COLUMN IF NOT EXISTS ir_url_validation text;    -- 存活/校验状态串

-- ── E3: event_companies view —— frontend 公司列表 (/api/companies) 唯一数据源, 之前手建无 migration ──
-- 精确复制 live `pg_get_viewdef('waterevents.event_companies')`: 用 JOIN (非 LEFT JOIN) → 只列有 ≥1 event 的公司。
CREATE OR REPLACE VIEW waterevents.event_companies AS
  SELECT c.id, c.ticker, c.ir_url, count(e.id)::integer AS event_count
  FROM waterevents.companies c
    JOIN waterevents.events e ON e.company_id = c.id
  GROUP BY c.id, c.ticker, c.ir_url;
