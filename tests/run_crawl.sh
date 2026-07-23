#!/bin/bash
# tests/run_crawl.sh — clean launcher for deepcrawl.py on the pod. All env vars EXPORTED here (not plumbed through a
# nested `env $E` string, which silently dropped vars because a non-exported shell var isn't seen by a child bash -c —
# that produced the 401 + mode=sequential + max_pages=? failures). Override any knob at call time:
#   CONCURRENT=1 IR_WATERCRAWL_MAX_PAGES=48 EVENT_BATCH=48 EVENT_MAX_PAGES=15 bash tests/run_crawl.sh
cd /workspace/WaterEvents || exit 1

export QWEN_BASE_URLS=http://127.0.0.1:8000/v1
export QWEN_API_KEY=sk-waterevents-0b1307fdf041607d7e55838c277320498bbee722867cad78
export QWEN_MAX_TOKENS=${QWEN_MAX_TOKENS:-12000}
export QWEN_RETRIES=${QWEN_RETRIES:-3}
export EVENT_VISION_TEXT_CHARS=${EVENT_VISION_TEXT_CHARS:-24000}
export EVENT_MAX_PAGES=${EVENT_MAX_PAGES:-15}          # BFS depth per company
export EVENT_BATCH=${EVENT_BATCH:-48}                  # pages rendered+sent per BFS round (was 16)
export IR_WATERCRAWL_MAX_PAGES=${IR_WATERCRAWL_MAX_PAGES:-48}   # TOTAL render concurrency across the browser pool
export IR_WATERCRAWL_BROWSERS=${IR_WATERCRAWL_BROWSERS:-1}      # SEPARATE Chromium processes (spread tabs → no single-browser starve)
export CONCURRENT=${CONCURRENT:-1}                     # companies-concurrent by default

echo "[run_crawl] CONCURRENT=$CONCURRENT RENDER=$IR_WATERCRAWL_MAX_PAGES BROWSERS=$IR_WATERCRAWL_BROWSERS BATCH=$EVENT_BATCH MAX_PAGES=$EVENT_MAX_PAGES"
exec /root/venv/bin/python tests/deepcrawl.py
