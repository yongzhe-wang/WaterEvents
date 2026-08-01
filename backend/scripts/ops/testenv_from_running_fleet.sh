#!/usr/bin/env bash
# testenv_from_running_fleet — derive a test shell's env from the environment of the fleet process that is ACTUALLY
# RUNNING on this host, by reading /proc/<pid>/environ, and print it as `export` lines for `eval`/`source`.
#
# 用一句话讲完: 找到活着的 worker/pacer/vLLM 进程 → 读它的 /proc/<pid>/environ → 把 crawl 需要的那几个变量原样打印成
# export 行 → 测试脚本 source 它，于是测试进程和生产进程用的是同一份 env。WHY: 因为反过来做过三次，每次都错。
#
# The failure this exists to stop, three times in one session:
#   1. Diagnosed CECO as "content fetched then discarded" — the pod shell had no WATERCRAWL_HTTP_FIRST=0, production had
#      it in BOTH env files already. The bug was in the harness.
#   2. Concluded tiers 3/4 were dormant "by design" — the pod shell had no WEBSHARE_PROXY, production had one.
#   3. Concluded extract silently drops every event on Shimano and Boeing — the pod shell had no QWEN_API_KEY, so every
#      VLM call returned 401 and the extractor correctly saw zero events. Nothing was being dropped.
# Each time the conclusion was about production and the evidence was about a shell that did not match it. Copying an env
# file by hand does not fix this, because the file drifts from what the process was actually launched with. The running
# process's own environ cannot drift from itself.
# {MEASURED 2026-08-01 "[qwen] send failed after 3 retries: AuthenticationError: Error code: 401 - {'error':
#  'Unauthorized'}" then "RAW from VLM: 0 events" — a clean extractor reported as a broken one}
# [CONFIDENCE: CONFIRMED 100% — all three misdiagnoses are in this session's transcript with their corrections.]
#
# usage:  eval "$(bash backend/scripts/ops/testenv_from_running_fleet.sh)"   # then run your harness
#         bash backend/scripts/ops/testenv_from_running_fleet.sh --check     # names only, no values
set -u
WANT="WATEREVENTS_DB_DSN WATEREVENTS_DB_SCHEMA WATEREVENTS_RUN_ID QWEN_BASE_URLS QWEN_SERVED_NAME QWEN_API_KEY
      WEBSHARE_PROXY WATERCRAWL_NO_SHOT EVENT_USE_IMAGE EVENT_MAX_PAGES EVENT_BATCH EVENTINC_PROFILE
      IR_WATERCRAWL_MAX_PAGES IR_WATERCRAWL_BROWSERS PYTHONPATH"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

# Prefer a queue_worker: it is the process whose env the crawl path actually runs under. Fall back to the pacer, then to
# vLLM (which carries QWEN_API_KEY as --api-key even when no worker is up). Bracket the pattern so grep skips itself.
pid=""
for pat in '[e]vent_agent.scheduler.worker' '[e]vent_agent.scheduler.solver.pacer'; do
  pid=$(pgrep -f "$pat" 2>/dev/null | head -1) && [ -n "$pid" ] && { src="$pat"; break; }
done
# vLLM carries the api-key on its COMMAND LINE (`--api-key sk-...`), never in the fleet's environ, so it is a SEPARATE
# source rather than a fallback for the whole set. This is what the RunPod pod needs: it runs a vLLM and no fleet, so
# the key is present on the box and discoverable, and failing to discover it is what produced four 401s and the false
# conclusion that the extractor silently drops every event on Shimano and Boeing.
# {MEASURED 2026-08-01 pod: `ps aux` shows `--api-key sk-wa...`, while the ir-media fleet's own environ has NO
#  QWEN_API_KEY at all and still produces events — the two vLLMs differ in whether they enforce a key}
# [CONFIDENCE: CONFIRMED 100% — read off both process tables in the same session.]
vk=$(ps -eo args= 2>/dev/null | sed -n 's/.*--api-key[= ]\([^ ]*\).*/\1/p' | head -1)
emit() {                                                  # one place that decides --check (names) vs eval (values)
  if [ "$CHECK" = 1 ]; then echo "  $1 (${#2} chars, $3)"; else printf "export %s='%s'\n" "$1" "$(printf '%s' "$2" | sed "s/'/'\\\\''/g")"; fi
}
if [ -n "$vk" ]; then
  emit QWEN_API_KEY "$vk" "from the local vLLM command line"
  # A vLLM on this box serves on loopback; when there is no fleet environ to say otherwise, that IS the endpoint.
  [ -z "$pid" ] && { emit QWEN_BASE_URLS "http://127.0.0.1:8000/v1" "local vLLM"; emit QWEN_SERVED_NAME "qwen-vl" "local vLLM"; }
fi
if [ -z "$pid" ]; then
  echo "# NO fleet process on $(hostname) — DB/crawl vars unavailable here; derive those on the host running the fleet." >&2
  [ -n "$vk" ] || { echo "# and no vLLM either — nothing to derive. Refusing to guess." >&2; exit 3; }
  exit 0
fi
echo "# derived from pid $pid ($src) on $(hostname)" >&2

# /proc/<pid>/environ is NUL-separated. Emit one `export NAME='VALUE'` per wanted key, single-quoting the value and
# escaping any embedded single quote, so a DSN with punctuation survives the round-trip into the caller's shell.
found=""
while IFS= read -r -d '' kv; do
  name="${kv%%=*}"; val="${kv#*=}"
  case " $WANT " in *" $name "*) ;; *) continue ;; esac
  found="$found $name"
  if [ "$CHECK" = 1 ]; then
    echo "  $name (${#val} chars)"
  else
    printf "export %s='%s'\n" "$name" "$(printf '%s' "$val" | sed "s/'/'\\\\''/g")"
  fi
done < "/proc/$pid/environ"

for w in $WANT; do
  case " $found " in *" $w "*) ;; *) echo "# MISSING from the running process: $w" >&2 ;; esac
done
