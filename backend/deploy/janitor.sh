#!/usr/bin/env bash
# janitor — bound the crawl-trace directory so the disk cannot fill silently. Keeps the newest KEEP traces, deletes
# the rest. Invoked by waterevents-janitor.service (daily via .timer); safe to run by hand.
#
# 用一句话讲完: find 列出所有 trace 目录并带上 mtime → 按时间倒序 → 跳过最新的 500 个 → 其余 rm -rf，全程 NUL 分隔。
# WHY 是一个脚本文件而不是 unit 里的一行: 因为写成一行的两个版本都被 systemd 悄悄改写了，见下。
#
# ── WHY THIS IS A FILE AND NOT AN ExecStart ONE-LINER ─────────────────────────────────────────────────────────────
# The logic lived inline in waterevents-janitor.service and systemd rewrote it, twice, without failing:
#
#   1. The inner double quotes around "$T" terminate systemd's own quoted-string parsing, so the stored command was
#      truncated to `argv[]=/bin/bash -c set -o pipefail` — a shell that sets an option and exits 0. systemd logged
#      "Ignoring unknown escape sequences" for the rest and reported the unit Finished, successfully, in one second.
#   2. `%` is systemd's specifier prefix, so find's `-printf "%T@ %p"` is not even safe to write there; and `\t` / `\0`
#      go through systemd's escape processing before bash ever sees them.
#
# The effect was a janitor that ran daily, exited 0, and deleted nothing — for long enough to accumulate 127,523
# directories and 36GB, on a 97GB disk. A shell script is executed by a shell, so none of this applies to it.
# {JOURNAL 2026-08-01 "systemd[1]: /etc/systemd/system/waterevents-janitor.service:42: Ignoring unknown escape
#  sequences: \"set -o pipefail; T=...\""}
# {SYSTEMCTL 2026-08-01 "systemctl show -p ExecStart" -> "argv[]=/bin/bash -c set -o pipefail"}
# {MEASURED 2026-08-01 "Starting ... 13:54:14" / "Finished ... 13:54:15" while traces held 127,523 dirs / 36G}
# [CONFIDENCE: CONFIRMED 100% — the truncated argv was read back out of systemd after installing the one-liner.]
#
# Upstream trigger: waterevents-janitor.timer (daily). Downstream: deletes trace directories only — never the DB.
set -uo pipefail

T="${WATEREVENTS_TRACES:-/home/thebigsun/WaterEvents/backend/agent/event_agent/crawl/traces}"
KEEP="${WATEREVENTS_TRACE_KEEP:-500}"

# A fresh host has no traces until the first crawl; that is not a failure worth a daily red unit.
[ -d "$T" ] || { echo "janitor: $T does not exist yet — nothing to do"; exit 0; }

before=$(find "$T" -mindepth 1 -maxdepth 1 -type d -printf 'x\n' 2>/dev/null | wc -l)

# find STREAMS its results; it never builds one argv holding every path. The obvious-looking `ls -1dt "$T"/*/` does,
# and at 127,451 directories that argv is ~11MB against an ARG_MAX of 2,097,152 — the exec fails with
# "/usr/bin/ls: Argument list too long". That failure was invisible because a pipeline's exit status comes from its
# LAST stage, and `xargs -r` handed empty input does nothing and returns 0. So the retention job broke precisely when
# the backlog grew large enough to need it, and reported success while doing so.
# `set -o pipefail` above is what makes a repeat of that visible instead of silent.
# {MEASURED 2026-08-01 ir-media-8 "bash -c 'ls -1dt $T/*/ >/dev/null'" -> "/usr/bin/ls: Argument list too long",
#  while the same full pipeline's exit code measured 0 in the same shell}
# [CONFIDENCE: CONFIRMED 100% — both the failure and the false success were reproduced on the host.]
# NUL end-to-end (-printf ...\0, sort -z, tail -z, cut -z, xargs -0) so a path containing a space, a quote or a
# newline cannot split into two arguments and delete something that was never selected.
find "$T" -mindepth 1 -maxdepth 1 -type d -printf '%T@\t%p\0' \
  | sort -zrn \
  | tail -zn "+$((KEEP + 1))" \
  | cut -zf2- \
  | xargs -0 -r rm -rf
rc=$?

after=$(find "$T" -mindepth 1 -maxdepth 1 -type d -printf 'x\n' 2>/dev/null | wc -l)
echo "janitor: $before -> $after directories (keep=$KEEP, removed $((before - after)), rc=$rc)"

# Report the real outcome. A retention job that cannot reduce a backlog it was asked to reduce must not exit 0 — that
# is the whole failure mode above, and exiting non-zero is what puts it in `systemctl --failed` where it belongs.
#
# THE TEST IS "DID IT REMOVE ANYTHING", NOT "DID IT LAND EXACTLY ON KEEP". The first version of this guard demanded
# after <= KEEP and failed the very run that fixed the problem: it removed 127,050 directories and took the tree from
# 36G to 138M, then reported failure because six workers had written 23 fresh traces during the ~3 minutes the delete
# was running. The fleet does not stop for the janitor, so a count taken after a long delete is always slightly above
# the target, and treating that as failure would page someone every night for a job that is working.
# The condition that actually distinguishes the real bug: it was asked to remove things and removed none.
# {MEASURED 2026-08-01 "janitor: 127573 -> 523 directories (keep=500, removed 127050, rc=0)" followed by this guard
#  printing "FAILED to get below keep=500 (still 523)" and systemd logging "Failed with result 'exit-code'"}
# [CONFIDENCE: CONFIRMED 100% — my own guard, observed failing the successful run that produced it.]
if [ "$rc" -ne 0 ]; then exit "$rc"; fi
removed=$((before - after))
if [ "$before" -gt "$KEEP" ] && [ "$removed" -le 0 ]; then
  echo "janitor: removed nothing while $before > keep=$KEEP — retention is not working" >&2
  exit 1
fi
# A second, looser backstop for the partial case: a run that deleted SOME but left the tree still far over target has
# not done its job either. 2×KEEP is wide enough that normal concurrent writes never trip it.
if [ "$after" -gt $((KEEP * 2)) ]; then
  echo "janitor: still $after directories after removing $removed — well over keep=$KEEP" >&2
  exit 1
fi
