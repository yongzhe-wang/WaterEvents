#!/usr/bin/env bash
# Launch fetch_10 with the run env + CAPTURE exit code/signal (128+N: 137=SIGKILL/OOM, 139=SIGSEGV). Log -> tests/fetch_10.log.
cd /workspace/WaterEvents || exit 2
export QWEN_BASE_URLS=http://127.0.0.1:8000/v1 QWEN_SERVED_NAME=qwen-vl
export QWEN_API_KEY=sk-waterevents-0b1307fdf041607d7e55838c277320498bbee722867cad78
export QWEN_MAX_TOKENS=12000 EVENT_VISION_TEXT_CHARS=24000 EVENT_MAX_PAGES=40 EVENT_USE_IMAGE=1
export FETCH_COMPANY_CONCURRENCY="${FETCH_COMPANY_CONCURRENCY:-2}"   # companies at once
export EVENT_BATCH="${EVENT_BATCH:-8}"                               # pages rendered concurrently per company (was 16 → OOM)
rm -rf tests/10event && mkdir -p tests/10event
/root/venv/bin/python fetch_10.py
echo "=== fetch_10 EXIT=$? (137=SIGKILL/OOM 139=SIGSEGV) $(date -u +%H:%M:%S)UTC ===" >> tests/fetch_10.log
