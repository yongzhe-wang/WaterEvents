#!/bin/bash
# tests/sweep.sh — render-concurrency sweep to find the sweet spot. render=6 starved the GPU (~20% util) but rendered
# reliably; render=48 saturated the GPU yet TIMED OUT the events pages (one Chromium can't do 48 heavy renders at once)
# → event yield collapsed (NVDA 94→8). Sweep 6/16/24 over the SAME 4 companies (concurrent) and read event-yield +
# render-fail-rate + wall-clock per point; the best is the one that maximizes EVENTS, not GPU util. {USER "render faster
# / gpu can handle more" — but the real limiter is render reliability, measured here}.
cd /workspace/WaterEvents || exit 1
for R in 6 16 24; do
  rm -rf tests/deepcrawl_concurrent
  IR_WATERCRAWL_MAX_PAGES=$R EVENT_BATCH=$R bash tests/run_crawl.sh > tests/sweep_r$R.log 2>&1
  rm -rf tests/dc_r$R; mv tests/deepcrawl_concurrent tests/dc_r$R 2>/dev/null
  echo "[sweep] render=$R done"
done
echo DONE > tests/sweep_marker.txt
