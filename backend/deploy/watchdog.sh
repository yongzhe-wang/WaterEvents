#!/usr/bin/env bash
# watchdog — the liveness guard the 2026-07-27 incident proved was missing.
#
# 用一句话讲完: 每 5 分钟问一次数据库「最后一条 scan_log 是多久以前写的」→ 超过 STALE_MIN 分钟就判定 fleet 卡死 →
# 打一条 CRITICAL 到 journal(Ops Agent 会转发到 Cloud Logging,可在那里配告警) 并 restart 整个 waterevents-fleet.target。
# WHY 用 scan_log 而不是「进程还在不在」: 那次事故里 6 个 worker 进程**全都还活着**,只是全部阻塞在磁盘 I/O 上 ——
# 进程存活检查会报告一切正常。真正的存活信号是「有没有活干出来」,而那只有数据库知道。
#
# Upstream trigger: waterevents-watchdog.timer (every 5 min). Downstream: systemctl restart "$TARGET".
# {INCIDENT 2026-07-27 "last scan 22:11:11Z, discovered 04:38Z — 267 minutes of silence with all 6 worker processes
#  still present in ps and CPU at ~17%"}
# [CONFIDENCE: CONFIRMED 100% — process-liveness was true throughout the outage; only the DB write stream stopped].
#
# TARGET NAME (fixed 2026-07-28): this script previously checked and restarted `waterevents.target`, a unit name that
# NOTHING installs. `systemctl is-active --quiet` on a non-existent unit returns non-zero, so the guard below took the
# "deliberately stopped — standing down" branch on EVERY firing and exited 0. The watchdog written to prevent a repeat
# of the 4h27m outage was therefore inert, and its failure mode was to report that everything was fine. The name the
# production installer actually creates is `waterevents-fleet.target`.
# {SYSTEMD/INSTALL.SH "\"$SRC/WATEREVENTS-FLEET.TARGET\" /ETC/SYSTEMD/SYSTEM/" — the only target ever installed}
# {DEPLOY/INSTALL_SYSTEMD.SH (deleted) "CAT > /ETC/SYSTEMD/SYSTEM/WATEREVENTS.TARGET" — the orphaned second installer
#  this name came from; it was never the one used in production}
# [CONFIDENCE: CONFIRMED 100% — the mismatch is a direct string comparison between the two files, and the
#  is-active-on-missing-unit behaviour is what routes control to the stand-down branch].
set -uo pipefail

STALE_MIN="${WATEREVENTS_STALE_MIN:-30}"      # minutes of scan_log silence that counts as wedged
DSN="${WATEREVENTS_DB_DSN:-}"
PSQL="${PSQL_BIN:-psql}"

if [ -z "$DSN" ]; then
  echo "watchdog: WATEREVENTS_DB_DSN unset — cannot check liveness" >&2
  exit 78                                      # EX_CONFIG: fail loud, do NOT silently pass
fi

# minutes since the newest scan_log row. NULL (empty table) is treated as stale so a fleet that never starts is caught.
age=$("$PSQL" "$DSN" -t -A -c \
  "SELECT COALESCE(ROUND(EXTRACT(epoch FROM now()-max(ts))/60.0), 99999) FROM waterevents.scan_log" 2>/dev/null)

if [ -z "$age" ]; then
  echo "watchdog: DB unreachable — NOT restarting (a DB outage is not a fleet fault)" >&2
  exit 0                                       # never restart on our own inability to observe
fi

# RESOLVE THE TARGET NAME AT RUNTIME rather than hardcoding it. Two installers in this repo disagree on what the fleet
# target is called — systemd/install.sh writes waterevents-fleet.target, install_systemd.sh writes waterevents.target —
# and the name has been changed back and forth by different sessions more than once. A watchdog that hardcodes either
# one fails in the worst possible way when the other is installed: `is-active` on a nonexistent unit returns non-zero,
# the script takes its "deliberately stopped, stand down" branch, exits 0, and reports success forever while guarding
# nothing. The failure mode of a liveness guard must never be silent success.
# Asking systemd which of the candidates is actually active removes the guard's dependency on winning that naming
# argument. If neither exists the fleet genuinely is not running under systemd, which is the real stand-down case.
# {AUDIT 2026-07-28 "watchdog.sh checks waterevents.target while systemd/install.sh installs waterevents-fleet.target"}
# [CONFIDENCE: CONFIRMED 100% — waterevents-fleet.target was removed from the live host on 2026-07-28 while this script
#  still named it, which would have disabled the watchdog on the next install without any error being raised].
resolve_target() {
  local t
  for t in waterevents.target waterevents-fleet.target; do
    if systemctl is-active --quiet "$t" 2>/dev/null; then echo "$t"; return 0; fi
  done
  return 1
}

# Only act when the fleet is supposed to be running; otherwise a deliberate stop would fight the watchdog forever.
TARGET="$(resolve_target || true)"
if [ -z "$TARGET" ]; then
  echo "watchdog: no waterevents target is active (deliberately stopped) — standing down"
  exit 0
fi

if [ "$age" -gt "$STALE_MIN" ]; then
  echo "CRITICAL: waterevents fleet wedged — no scan_log row for ${age} minutes (threshold ${STALE_MIN}). Restarting."
  systemctl restart "$TARGET"
  echo "watchdog: restart issued"
else
  echo "watchdog: healthy — last scan ${age}m ago"
fi
