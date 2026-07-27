-- 20260726050000_waterevents_scheduler_state.sql
-- 用一句话讲完: 一张单行表(id=1)存 packing solver 每次解出的调度状态 —— T*(incremental 最优周期秒)、binding 资源、
-- 实测 C_R/C_V/hit_rate、full 周期 ETA、profile 名 —— 让 complete_work 读 t_star_s 决定 incremental re-arm 间隔(不再写死
-- 30min)、让 Today 页读这一行直接显示调度仪表盘。solver(pacer.py)每小时 UPDATE 它,是「动态调整算法」的落点。
-- WHY 单行表(不是 env):多进程 worker + pacer 要共享同一个 T*;env 改了要重启进程,DB 一行改了所有进程下次读就生效,
-- 且 Today 页能直接查。{USER 2026-07-26 "adjust dynamically based on algorithm; adjustable when we move to GCP"}
-- [CONFIDENCE: CONFIRMED — solver 输出要跨进程共享 + 前端可读].

CREATE TABLE IF NOT EXISTS waterevents.scheduler_state (
  id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),   -- 单行:永远只有 id=1
  profile     text,                                        -- 'runpod' | 'gcp32' | 'gcp64' —— 换机器改这个 → 换 capacity 天花板
  t_star_s    double precision,                            -- incremental 最优周期(秒);complete_work re-arm 读它
  binding     text,                                        -- 'render' | 'vlm' —— 当前哪个资源卡着 T*(仪表盘显示)
  c_r         double precision,                            -- 实测 render 吞吐 pages/hr(滑窗)
  c_v         double precision,                            -- 实测 VLM 吞吐 page-extractions/hr(滑窗)
  hit_rate    double precision,                            -- hash 变更率 = vlm_calls/(vlm_calls+vlm_skipped)
  eta_full_h  double precision,                            -- 当前 full backlog 全跑完预计还需几小时(Little's Law)
  inc_hubs    int,                                         -- 参与摊派的 incremental hub 数(solve 时的 N_hub)
  note        text,                                        -- solver 的可读结论(如 "full weekly infeasible, inc-only")
  updated_at  timestamptz DEFAULT now()
);

-- 播种单行(幂等):首次 pacer 跑之前 Today 页也能读到一行占位。
INSERT INTO waterevents.scheduler_state (id, profile, note)
VALUES (1, 'runpod', 'not solved yet')
ON CONFLICT (id) DO NOTHING;
