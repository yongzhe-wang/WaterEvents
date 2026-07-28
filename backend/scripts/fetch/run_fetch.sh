#!/usr/bin/env bash
# Launch fetch_10 with the run env + CAPTURE exit code/signal (128+N: 137=SIGKILL/OOM, 139=SIGSEGV). Log -> tests/fetch_10.log.
# cwd stays at the REPO ROOT because every output path below is root-relative (tests/ did NOT move in the restructure).
# Only the python IMPORT root moved, so that is what PYTHONPATH points at, and fetch_10.py is addressed by its real path.
# This also repairs a PRE-EXISTING break: `python fetch_10.py` stopped resolving when the 2026-07-26 package split moved
# the script into scripts/fetch/, so this launcher had already been dead — the restructure only made it visible.
# {RESTRUCTURE 2026-07-28} [CONFIDENCE: CONFIRMED 100% — file is at backend/scripts/fetch/fetch_10.py].
# set -u so an unset var is an error rather than an empty string; `${VAR:?}` below aborts on a missing secret either
# way, but -u also catches any future bare `$VAR` typo instead of silently substituting "".
set -u
WE_ROOT="${WE_ROOT:-/workspace/WaterEvents}"
cd "$WE_ROOT" || exit 2
export PYTHONPATH="$WE_ROOT/backend"
export QWEN_BASE_URLS=http://127.0.0.1:8000/v1 QWEN_SERVED_NAME=qwen-vl
# NO KEY LITERAL — `${VAR:?msg}` fails loudly when unset. The bare-assignment form that was here committed the live
# vLLM api-key into git; export it from the environment (or source /etc/waterevents.env) before running this launcher.
# {GIT GREP 2026-07-28 "RUN_FETCH.SH:12 EXPORT QWEN_API_KEY=SK-WATEREVENTS-0B1307FDF041607D7E55838C277320498BBEE722867CAD78"}
# [CONFIDENCE: CONFIRMED 100% — read off the tracked file at HEAD 9d3402f; identical literal in supervise_fetch.sh:17].
export QWEN_API_KEY="${QWEN_API_KEY:?set QWEN_API_KEY before running run_fetch.sh (e.g. set -a; . /etc/waterevents.env; set +a)}"
export QWEN_MAX_TOKENS=12000 EVENT_VISION_TEXT_CHARS=24000 EVENT_MAX_PAGES=40 EVENT_USE_IMAGE=1
export FETCH_COMPANY_CONCURRENCY="${FETCH_COMPANY_CONCURRENCY:-2}"   # companies at once
export EVENT_BATCH="${EVENT_BATCH:-8}"                               # pages rendered concurrently per company (was 16 → OOM)
rm -rf tests/10event && mkdir -p tests/10event
/root/venv/bin/python backend/scripts/fetch/fetch_10.py
echo "=== fetch_10 EXIT=$? (137=SIGKILL/OOM 139=SIGSEGV) $(date -u +%H:%M:%S)UTC ===" >> tests/fetch_10.log
