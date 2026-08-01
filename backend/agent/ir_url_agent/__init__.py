"""ir_url_agent — 给每家公司发现 N 个 IR 入口 URL(events / presentations / calendar / homepage),写进
`waterevents.companies.event_hubs`,让 crawl 能从多个 frontier 同时起爬(不再只吃一个 homepage seed)。

用一句话讲完: 对一家公司先用 watercrawl.render_full 渲它的 IR 首页(浏览器执行 JS + 四级 fallback → 连 mega-menu
里的链接都拿得到)→ 从这一页的链接里筛出同站 IR-ish 候选 → 候选太少(整条渲染链都挂)才退回 Brave SERP 搜索 →
候选一次性喂 QwenClient guided_json 分类(kind + belongs_to_company + is_seed)→ 幂等写回 event_hubs。
WHY 存在: 6-9-event 的根因是 IR homepage 的 events 子页链接藏在 JS mega-menu 里、BFS 抽不到 → 从没爬到完整
events 列表。与其手动 patch 每个 seed,不如给每家预先发现多个 IR 入口 URL 直接 seed 进 frontier — 一劳永逸。
{USER 2026-07-25 "multiple url for one company so it start from multiple frontier ... use search tool + llm filter
for the iragent, create a new folder called ir_url_agent"} [CONFIDENCE: CONFIRMED — 直接指令].

PACKAGE LAYOUT (刻意只有两个模块 — 本 agent 只发现 URL 并幂等入库, 不做深 BFS, 也不需要队列/lease):
  - discover.py  — 单公司 pipeline: render_full 收 nav 链接 →(料太少才)SERP 兜底 → Qwen 分类 → hub objects
  - db.py        — asyncpg 层: connect_pool(waterevents) + flush_event_hubs(幂等写)
曾经还有个常驻 worker.py + db.fetch_companies, 已删 —— 用户明确要"一个脚本跑完"而不是队列式 worker, 跑法收敛到
`tests/run_irurl_all.py`(读全量公司 → 并发 discover → flush, 断点续跑)。{USER 2026-07-26 "JUST RUN ALL USE ONE SCRIPT"}.

复用(不新建): `from agent.event_agent.urls import _canon` —— URL 规范化/dedup key 必须跨 agent 一致。
DB schema: 一律 canonical `waterevents`(public 是 07-22 冻结的 legacy)。{SCAN whn86f4f7 "canonical = waterevents"}.

跑法 (在 pod/GCP 上, 不在 Mac — MEMORY run-work-on-vm-not-mac):
  cd /workspace/WaterEvents && WATERCRAWL_NO_SHOT=1 \
    WATEREVENTS_DB_DSN=... QWEN_API_KEY=... QWEN_BASE_URLS=http://127.0.0.1:8000/v1 \
    PYTHONPATH=/workspace/WaterEvents/backend nohup /root/venv/bin/python tests/run_irurl_all.py > /workspace/irurl_all.log 2>&1 &
"""
