#!/bin/bash
# tests/mbab.sh — clean A/B isolating the multi-browser effect: SAME total render concurrency (48) + same 4 companies +
# same max_pages, ONLY the browser-process count differs. b1 = 1 browser × 48 tabs (the starvation case that timed out
# events pages); b8 = 8 browsers × 6 tabs (the fix). Compare event yield + render-fail count + wall-clock. Each run is a
# FRESH python process building its OWN browser pool, so no cross-run cleanup needed (no pkill → won't touch other sessions).
cd /workspace/WaterEvents || exit 1

echo "======== B1: 1 browser × 48 tabs ========"
IR_WATERCRAWL_BROWSERS=1 IR_WATERCRAWL_MAX_PAGES=48 EVENT_BATCH=48 EVENT_MAX_PAGES=15 CONCURRENT=1 bash tests/run_crawl.sh > tests/mb_b1.log 2>&1
rm -rf tests/dc_b1; mv tests/deepcrawl_concurrent tests/dc_b1 2>/dev/null

echo "======== B8: 8 browsers × 6 tabs ========"
IR_WATERCRAWL_BROWSERS=8 IR_WATERCRAWL_MAX_PAGES=48 EVENT_BATCH=48 EVENT_MAX_PAGES=15 CONCURRENT=1 bash tests/run_crawl.sh > tests/mb_b8.log 2>&1
rm -rf tests/dc_b8; mv tests/deepcrawl_concurrent tests/dc_b8 2>/dev/null

echo DONE > tests/mbab_marker.txt
