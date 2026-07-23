#!/bin/bash
# tests/mbab2.sh — SELF-GATED lower-concurrency sweep. The pod is shared with a concurrent session (fetch_10/media_run/
# pickpool); render failures are nav TIMEOUTS, which inflate under CPU contention, so a contended run gives dirty data
# AND disrupts the other session. So: WAIT until the other session's runs finish, THEN run clean. Two configs, both
# 3 tabs/browser (vs B8's 6), to test whether cutting per-browser tabs kills the ~40% render-fail:
#   C16 = 16 browsers × 3 tab = 48 total (same throughput, fewer tabs/browser)
#   D24 =  8 browsers × 3 tab = 24 total (lower total load)
cd /workspace/WaterEvents || exit 1

# gate: block until no other-session crawl is running (check every 15s)
while ps aux | grep -E "[f]etch_10|[m]edia_run|[p]ickpool" | grep -q .; do sleep 15; done
sleep 5   # let their browsers die + GPU settle

echo "======== C16: 16 browsers × 3 tab (total 48) ========"
IR_WATERCRAWL_BROWSERS=16 IR_WATERCRAWL_MAX_PAGES=48 EVENT_BATCH=48 EVENT_MAX_PAGES=15 CONCURRENT=1 bash tests/run_crawl.sh > tests/mb_c16.log 2>&1
rm -rf tests/dc_c16; mv tests/deepcrawl_concurrent tests/dc_c16 2>/dev/null

# re-gate before D24 in case the other session restarted mid-C16
while ps aux | grep -E "[f]etch_10|[m]edia_run|[p]ickpool" | grep -q .; do sleep 15; done
sleep 5

echo "======== D24: 8 browsers × 3 tab (total 24) ========"
IR_WATERCRAWL_BROWSERS=8 IR_WATERCRAWL_MAX_PAGES=24 EVENT_BATCH=24 EVENT_MAX_PAGES=15 CONCURRENT=1 bash tests/run_crawl.sh > tests/mb_d24.log 2>&1
rm -rf tests/dc_d24; mv tests/deepcrawl_concurrent tests/dc_d24 2>/dev/null

echo DONE > tests/mbab2_marker.txt
