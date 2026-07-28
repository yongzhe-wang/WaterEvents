#!/usr/bin/env bash
# supervise_fetch — AUTO-RESTART + AUTO-CLEAN wrapper around fetch_10. WHY: fetch_10 can die on a cgroup OOM (SIGKILL,
# EXIT=137) mid-run; this loop cleans orphan Chromium and RELAUNCHES fetch_10, which RESUMES (skips companies that already
# have a non-empty events.txt), so each attempt moves forward instead of re-crawling from scratch. Stops on "10event DONE"
# or after MAX_TRIES. {USER 2026-07-23 "we need a way to auto restart or clean"}.
set -u
# cwd = REPO ROOT (log paths are root-relative, tests/ did not move); PYTHONPATH = backend/, the new import root.
# Same pre-existing `python fetch_10.py` break repaired here as in run_fetch.sh. {RESTRUCTURE 2026-07-28}.
WE_ROOT="${WE_ROOT:-/workspace/WaterEvents}"
cd "$WE_ROOT" || exit 2
export PYTHONPATH="$WE_ROOT/backend"
LOG=tests/fetch_10.log
MAX_TRIES="${SUPERVISE_MAX_TRIES:-8}"

# run env (same as the old run_fetch.sh)
export QWEN_BASE_URLS=http://127.0.0.1:8000/v1 QWEN_SERVED_NAME=qwen-vl
# NO KEY LITERAL — second copy of the same committed secret as run_fetch.sh:12, removed for the same reason. Fails
# loudly here rather than after MAX_TRIES restart attempts that would each authenticate with a stale/absent key.
# {GIT GREP 2026-07-28 "SUPERVISE_FETCH.SH:17 EXPORT QWEN_API_KEY=SK-WATEREVENTS-0B1307FDF041607D7E55838C277320498BBEE722867CAD78"}
# [CONFIDENCE: CONFIRMED 100% — read off the tracked file at HEAD 9d3402f].
export QWEN_API_KEY="${QWEN_API_KEY:?set QWEN_API_KEY before running supervise_fetch.sh (e.g. set -a; . /etc/waterevents.env; set +a)}"
export QWEN_MAX_TOKENS=12000 EVENT_VISION_TEXT_CHARS=24000 EVENT_MAX_PAGES=40 EVENT_USE_IMAGE=1
# PARALLELISM. Multiple SEPARATE Chromium PROCESSES (IR_WATERCRAWL_BROWSERS) spread the render tabs across cores so no
# single browser's main-thread/IPC starves — the config default 1 browser × 24 tabs was the starvation case (NVDA yield
# 94→8). 6 browsers × ~4 tabs each keeps every browser near the reliable ~6-tab level while total render concurrency = 24.
# Safe now that the OOM bombs are defused (RENDER_ABORT_PX aborts giant pages pre-shot, SHOT_MAX_PX clips, binary-route
# guard skips PDF downloads). {USER 2026-07-23 "use more browser parallel"}.
export IR_WATERCRAWL_BROWSERS="${IR_WATERCRAWL_BROWSERS:-4}"         # separate Chromium processes
export IR_WATERCRAWL_MAX_PAGES="${IR_WATERCRAWL_MAX_PAGES:-24}"      # total concurrent renders (≈4 tabs/browser)
export FETCH_COMPANY_CONCURRENCY="${FETCH_COMPANY_CONCURRENCY:-4}"   # companies at once (feeds the browser pool)
export EVENT_BATCH="${EVENT_BATCH:-12}"                              # pages rendered concurrently per company per round

clean_chromium() {   # bracket-trick regex so THIS script's own arg text can't self-match the kill
  ps -eo pid=,args= | grep -E '[c]hrome|[h]eadless_shell|[c]hromium' | awk '{print $1}' | xargs -r kill -9 2>/dev/null
}

# fresh start unless RESUME=1 (RESUME keeps already-completed per-company events.txt across a manual relaunch)
if [ "${RESUME:-0}" != "1" ]; then rm -rf tests/10event; fi
mkdir -p tests/10event
: > "$LOG"

for try in $(seq 1 "$MAX_TRIES"); do
  echo "=== [supervise] attempt $try/$MAX_TRIES $(date -u +%H:%M:%S)UTC ===" >> "$LOG"
  clean_chromium; sleep 2
  /root/venv/bin/python backend/scripts/fetch/fetch_10.py >> "$LOG" 2>&1
  code=$?
  echo "=== [supervise] fetch_10 EXIT=$code (137=OOM/SIGKILL 139=SIGSEGV) attempt $try ===" >> "$LOG"
  if grep -q "10event DONE" "$LOG"; then
    echo "=== [supervise] ✅ DONE after $try attempt(s) — stopping ===" >> "$LOG"; break
  fi
  done_n=$(ls -d tests/10event/*/events.txt 2>/dev/null | wc -l)
  echo "=== [supervise] not DONE (exit $code); $done_n/10 companies finished; cleaning + RESUMING → attempt $((try+1)) ===" >> "$LOG"
done
clean_chromium
echo "=== [supervise] supervisor exit $(date -u +%H:%M:%S)UTC ===" >> "$LOG"
