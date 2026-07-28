#!/usr/bin/env bash
# watchdog — the liveness guard the 2026-07-27 incident proved was missing.
#
# 用一句话讲完: 每 5 分钟问一次数据库「最后一条 scan_log 是多久以前写的」→ 超过 STALE_MIN 分钟就判定 fleet 卡死 →
# 打一条 CRITICAL 到 journal(Ops Agent 会转发到 Cloud Logging,可在那里配告警) 并 restart 整个 waterevents.target。
# WHY 用 scan_log 而不是「进程还在不在」: 那次事故里 6 个 worker 进程**全都还活着**,只是全部阻塞在磁盘 I/O 上 ——
# 进程存活检查会报告一切正常。真正的存活信号是「有没有活干出来」,而那只有数据库知道。
#
# Upstream trigger: waterevents-watchdog.timer (every 5 min). Downstream: systemctl restart waterevents.target.
# {INCIDENT 2026-07-27 "last scan 22:11:11Z, discovered 04:38Z — 267 minutes of silence with all 6 worker processes
#  still present in ps and CPU at ~17%"}
# [CONFIDENCE: CONFIRMED 100% — process-liveness was true throughout the outage; only the DB write stream stopped].
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

# Only act when the fleet is supposed to be running; otherwise a deliberate stop would fight the watchdog forever.
if ! systemctl is-active --quiet waterevents.target; then
  echo "watchdog: waterevents.target inactive (deliberately stopped) — standing down"
  exit 0
fi

if [ "$age" -gt "$STALE_MIN" ]; then
  echo "CRITICAL: waterevents fleet wedged — no scan_log row for ${age} minutes (threshold ${STALE_MIN}). Restarting."
  systemctl restart waterevents.target
  echo "watchdog: restart issued"
else
  echo "watchdog: healthy — last scan ${age}m ago"
fi
