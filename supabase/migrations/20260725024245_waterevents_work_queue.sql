-- WaterEvents unified WORK QUEUE — one durable, pausable/resumable, dynamically-growable queue that holds BOTH heavy
-- weekly full-BFS-discovery units and light 30-min incremental hub-scan units, drained by one worker pool.
--
-- 用一句话讲完: 一张 DB 队列,每行 = 一个「要扫的页」;type 区分 full(一家 BFS 20+ 页 = 20+ VLM 调用,周更,低优先,
-- due_at 摊到整周)和 incremental(一个 hub = 1 VLM 调用,30min,高优先);worker 池按 (priority, due_at) SKIP-LOCKED 抢,
-- incremental 到期就插队、没 incremental 就啃 full 的海量 backlog → VLM 永不空转。停 worker=暂停(状态全在这表),重启=续跑,
-- 随时 INSERT=动态加公司/加 hub。{USER 2026-07-25 "one queue, full+incremental, pausable/resumable, add dynamically,
-- VLM never idle"} [CONFIDENCE: CONFIRMED — design agreed over this session].

CREATE TABLE IF NOT EXISTS waterevents.work_queue (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid,                                    -- FK-ish → companies.id (nullable: a hub may predate its company row)
  url          text NOT NULL,                           -- full: the company SEED url; incremental: the HUB url to shallow-scan
  type         text NOT NULL CHECK (type IN ('full','incremental')),
  status       text NOT NULL DEFAULT 'queued',          -- queued | running | done | failed
  due_at       timestamptz NOT NULL DEFAULT now(),      -- claimable only when due_at <= now(); full spread across the week
  priority     int  NOT NULL DEFAULT 100,               -- LOWER = higher; incremental (10) beats full (100) so it never waits
  vlm_weight   int  NOT NULL DEFAULT 1,                 -- ~VLM concurrency this unit costs: full≈5 (BFS batch), incremental=1
  lease_until  timestamptz,                             -- SKIP-LOCKED + lease: a crashed worker's row auto-reclaims after this
  lease_owner  text,
  attempt      int  NOT NULL DEFAULT 0,                 -- claim count (backoff / fail-loud after N)
  last_scanned_at  timestamptz,                         -- when this unit last completed a scan
  last_event_count int,                                 -- events found last scan (monitor + adaptive interval later)
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz DEFAULT now(),
  UNIQUE (type, url)                                     -- dedup: at most ONE row per (type,url) → dynamic-add is idempotent UPSERT
);

-- THE claim index: the worker's hot path is
--   WHERE status='queued' AND due_at<=now() [AND type=$] ORDER BY priority, due_at FOR UPDATE SKIP LOCKED LIMIT 1
-- so index (status, priority, due_at) makes both the filter and the ordering index-only. {claim ladder}.
CREATE INDEX IF NOT EXISTS work_queue_claim_idx
  ON waterevents.work_queue (status, priority, due_at);

-- Reclaim scan: find rows whose lease lapsed (crashed worker) — a reconcile cron flips them back to 'queued'.
CREATE INDEX IF NOT EXISTS work_queue_lease_idx
  ON waterevents.work_queue (status, lease_until) WHERE status = 'running';
