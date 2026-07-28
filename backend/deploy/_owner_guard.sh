#!/usr/bin/env bash
# _owner_guard — refuse to hand-launch a service that systemd already owns.
#
# 用一句话讲完: 每个 launch_*.sh 开头 source 这个文件并调 guard_owner <unit>,如果 systemd 已经在跑那个 unit 就直接
# exit 3 并告诉你该用哪条 systemctl 命令 —— 因为这个系统真正的事故来源不是崩溃,是**同一个服务有两个所有者**。
#
# WHY this exists. On 2026-07-28 systemd was running worker@1..6 while launch_fleet.sh started six MORE from a shell.
# Twelve workers doubled GPU demand; two pacers then fought over scheduler_state and drove T* from 13.87h to 89.95h,
# which drained the incremental pool so workers claimed almost nothing but `full` (~16 VLM calls each) and the vLLM
# queue climbed to 391 waiting. The webapp had the same collision in a milder form: the hand-started process held :8080
# so the systemd unit sat in a restart loop. None of that was a crash — every process was healthy on its own. The fault
# was purely that two supervisors believed they were in charge.
# {INCIDENT 2026-07-28 "workers=12 (systemd 6 + manual 6), pacers=2, webapp status=1/FAILURE restart-looping,
#  vLLM num_requests_waiting=391, pacer T*=89.95h vs 13.87h before"}
# [CONFIDENCE: CONFIRMED 100% — process counts read off the live host; collapsing back to systemd-only took the vLLM
#  backlog from 391 to 43 without changing a line of application code].
#
# WHY a guard rather than "remember to check": the check is one `systemctl is-active` away, and it was skipped anyway —
# by someone who had just written the systemd units. A rule that depends on remembering is not a control. This makes
# the mistake structurally impossible instead.
#
# Escape hatch: ALLOW_MANUAL=1 bypasses the guard, for the genuine case where systemd is broken and you need the
# process up now. It prints what it is overriding so the override is never silent.

# guard_owner <systemd-unit> [<human hint>]
#   exit 3 if the unit is active (or activating — a restart loop still means systemd owns the port/lease)
guard_owner() {
  local unit="$1"
  local hint="${2:-sudo systemctl restart $unit}"
  command -v systemctl >/dev/null 2>&1 || return 0        # not a systemd host (RunPod container) → nothing to collide with

  # `is-active` returns "activating" for a unit in a restart loop. That still counts as owned: the classic failure was
  # a hand-started process squatting the port so the unit could never finish activating. Treating "activating" as
  # free-to-launch would let the guard wave through the exact collision it exists to stop.
  local state
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  case "$state" in
    active|activating|reloading)
      if [ "${ALLOW_MANUAL:-}" = "1" ]; then
        echo "[guard] ⚠ ALLOW_MANUAL=1 — starting a SECOND $unit by hand while systemd reports '$state'." >&2
        echo "[guard]   this is how the 2026-07-28 double-fleet happened; make sure you meant it." >&2
        return 0
      fi
      echo "[guard] REFUSING: systemd already owns $unit (state=$state)." >&2
      echo "[guard]   use: $hint" >&2
      echo "[guard]   override (rarely right): ALLOW_MANUAL=1 $0" >&2
      exit 3
      ;;
  esac
  return 0
}
