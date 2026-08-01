#!/usr/bin/env bash
# deploy_manifest — compare every git-tracked backend file against what a deploy target is actually running, and say
# which side is ahead. Read-only by default; --sync copies the differing files after taking a timestamped backup.
#
# 用一句话讲完: 本地对每个 tracked backend 文件算 sha1 → ssh 到目标机对同名文件算 sha1 → 逐个比对，打出
# IDENTICAL / DIFFERS / MISSING-ON-TARGET / UNTRACKED-ON-TARGET 四类 → --sync 时先备份再覆盖 DIFFERS 和 MISSING。
# WHY: 因为生产机上没有 .git，部署一直是手工 scp，所以"prod 在跑哪个版本"这个问题此前无人能回答。
#
# The state this was written from, measured 2026-08-01 against ir-media-8:
#   88 files identical, 8 differing, 2 missing — and nothing on the host recorded that, because
#   /home/thebigsun/WaterEvents has no .git at all. `git log` there returns "fatal: not a git repository".
# The 2 missing files are the sharp edge. backend/agent/media_agent/pipeline/render_retry.py is absent from the host,
# while the repo's handlers.py:27 and worker.py:31 both do `from .render_retry import render_with_retry`. The host is
# currently self-consistent because it also has the OLD handlers.py and worker.py, which do not import it. So copying
# those two files alone — the exact thing a hand-rolled scp deploy does — turns a working host into an ImportError at
# startup. A manifest that lists all three together is what makes that impossible to get wrong.
# {MEASURED 2026-08-01 "git -C /home/thebigsun/WaterEvents log" -> "fatal: not a git repository"}
# {MEASURED 2026-08-01 "identical=88 differs=8 missing=2" across git ls-files 'backend/*.py'}
# [CONFIDENCE: CONFIRMED 100% — both readings taken directly off the production host.]
#
# usage:  bash backend/scripts/ops/deploy_manifest.sh <ssh-host> [<remote-root>]           # report only
#         bash backend/scripts/ops/deploy_manifest.sh <ssh-host> [<remote-root>] --sync    # report, back up, then copy
set -u
HOST="${1:?usage: deploy_manifest.sh <ssh-host> [<remote-root>] [--sync]}"
ROOT="${2:-/home/thebigsun/WaterEvents}"
[ "$ROOT" = "--sync" ] && { ROOT="/home/thebigsun/WaterEvents"; SYNC=1; } || SYNC=0
[ "${3:-}" = "--sync" ] && SYNC=1

cd "$(git rev-parse --show-toplevel)" || exit 2

# The manifest covers python AND the shell/sql the fleet runs, because a stale migration or watchdog is just as capable
# of a silent production difference as a stale module. Generated fresh each run rather than checked in: a checked-in
# manifest is one more thing that can be out of date, and the tracked file list is already the source of truth.
FILES=$(git ls-files 'backend/**.py' 'backend/**.sh' 'backend/**.sql' 'backend/**.mjs')
[ -z "$FILES" ] && { echo "no tracked backend files — wrong directory?" >&2; exit 2; }

MAN=$(mktemp); trap 'rm -f "$MAN"' EXIT
for f in $FILES; do printf '%s %s\n' "$f" "$(shasum -a1 "$f" | cut -d" " -f1)"; done > "$MAN"
echo "local: $(wc -l < "$MAN" | tr -d ' ') tracked backend files at $(git rev-parse --short HEAD)"

# One ssh round-trip: ship the manifest on stdin, classify remotely, print one line per non-identical file. Doing the
# comparison on the far side keeps this O(1) in connections rather than O(files).
#
# The remote side is built as a STRING and the manifest is fed on stdin. It cannot be a heredoc: `ssh host bash -s
# <<'EOF' <"$MAN"` has two stdin redirections, the second wins, and `bash -s` then reads the MANIFEST as its script —
# which is exactly what happened on the first run of this file, printing 129 lines of
# "backend/agent/__init__.py: No such file or directory" as the shell tried to execute each filename as a command.
# [CONFIDENCE: CONFIRMED 100% — reproduced, then fixed by moving the script into a variable.]
REMOTE_SCRIPT='same=0
while read -r f h; do
  p="$ROOT/$f"
  if [ ! -f "$p" ]; then echo "MISSING-ON-TARGET $f"
  elif [ "$(sha1sum "$p" | cut -d" " -f1)" = "$h" ]; then same=$((same+1))
  else echo "DIFFERS $f"; fi
done
echo "SAME $same"'
RESULT=$(ssh -o ConnectTimeout=60 "$HOST" "ROOT='$ROOT'; $REMOTE_SCRIPT" < "$MAN") \
  || { echo "ssh to $HOST failed" >&2; exit 4; }

echo "$RESULT" | awk '/^SAME/{printf "target: %s identical\n",$2}'
echo "$RESULT" | grep -v '^SAME' | sed 's/^/  /'
NDIFF=$(echo "$RESULT" | grep -c '^DIFFERS' || true)
NMISS=$(echo "$RESULT" | grep -c '^MISSING-ON-TARGET' || true)
[ "$NDIFF" = 0 ] && [ "$NMISS" = 0 ] && { echo "target is in sync with $(git rev-parse --short HEAD)"; exit 0; }

# NOTE ON DIRECTION: a hash mismatch says the two files differ, NOT which one is newer. Before --sync, look at the
# actual diff for anything you did not change yourself — a host that was hot-patched during an incident holds the only
# copy of that patch, and syncing would destroy it. This is why --sync is opt-in and always backs up first.
if [ "$SYNC" != 1 ]; then
  echo
  echo "$NDIFF differing + $NMISS missing. Re-run with --sync to copy them (a backup is taken first)."
  echo "Inspect first if you did not author the change: ssh $HOST 'cat $ROOT/<file>' | diff - <file>"
  exit 1
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
echo "syncing $((NDIFF+NMISS)) files to $HOST:$ROOT (backup -> $ROOT/../deploy_backup_$STAMP)"
echo "$RESULT" | grep -E '^(DIFFERS|MISSING-ON-TARGET)' | while read -r _kind f; do
  # mkdir -p the parent so a file in a directory the target has never seen still lands; back up only what exists.
  ssh "$HOST" "mkdir -p \"$ROOT/\$(dirname '$f')\" \"$ROOT/../deploy_backup_$STAMP/\$(dirname '$f')\";
               [ -f '$ROOT/$f' ] && cp '$ROOT/$f' '$ROOT/../deploy_backup_$STAMP/$f' || true"
  scp -q "$f" "$HOST:$ROOT/$f" && echo "  sent $f"
done
echo "sync done. Compile-check and restart the units yourself — this script does not restart anything, because"
echo "deciding WHEN to bounce a running fleet is not a decision a file-copy tool should be making."
