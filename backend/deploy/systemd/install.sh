#!/usr/bin/env bash
# install.sh — THE single installer that puts the WaterEvents fleet under systemd. Idempotent; safe to re-run.
#
# 用一句话讲完: 先 VERIFY /etc/waterevents.env 已经由人工带着 secret 装好(缺任何一个 key 就非零退出,绝不再从
# launch_fleet.sh 里 sed 抠密码)→ 装 worker/pacer/webapp/target 四个 unit + watchdog 和 janitor 的 service+timer +
# logrotate 配置 → 杀掉挂在 ssh session scope 里的旧 fleet → enable+start → 最后 assert watchdog.timer 真的 enabled。
# WHY: nohup 起的进程留在 ssh session 的 systemd scope 里, 会话被回收时整棵树一起死 —— 2026-07-28 03:00 就这么丢了一次
# fleet。systemd unit 让它活过会话, 并给了 Restart=always 和 cgroup 内存上限。
#
# WHY this is now the ONLY installer: backend/deploy/install_systemd.sh used to be a SECOND, divergent installer that
# alone carried the watchdog timer, the trace janitor and the logrotate config, while THIS script alone was the one
# actually used in production — so the production host ran with no watchdog, no janitor and unbounded logs. The two are
# consolidated here and the other file has been DELETED (2026-07-28): leaving it as a pointer would still have left a
# second target NAME in the tree, which is precisely what watchdog.sh was matching against and missing.
# {AUDIT 2026-07-28 "grep -c 'watchdog\|janitor\|logrotate' backend/deploy/systemd/install.sh → 0, while the file
#  installs only 4 unit files; the watchdog timer existed solely in the orphaned backend/deploy/install_systemd.sh"}
# [CONFIDENCE: CONFIRMED 100% — both installers read in full at HEAD 9d3402f; the unit lists are disjoint].
#
# Usage:  sudo N=6 bash backend/deploy/systemd/install.sh
# PREREQUISITE: /etc/waterevents.env must already exist with the secrets. See the OPS MIGRATION section in README.md.
set -euo pipefail
N="${N:-6}"
REPO="${EVENTINC_HOME:-/home/thebigsun/WaterEvents}"
SRC="$REPO/backend/deploy/systemd"
DEPLOY="$REPO/backend/deploy"
ENVF=/etc/waterevents.env
RUN_USER="${EVENTINC_USER:-thebigsun}"
LOGD="${EVENTINC_LOGD:-/home/$RUN_USER/eventinc_fleet}"

# ── env file: REQUIRE it, never synthesise it ───────────────────────────────────────────────────────────────────────
# WHY this replaced the old sed-extraction: this script used to reconstruct the secrets by regex-scraping
# `export VAR="${VAR:-<literal>}"` lines out of launch_fleet.sh. That only worked because the credentials were
# committed in the repo. Now that launch_fleet.sh uses fail-loud `${VAR:?}` forms with NO literals, the same sed would
# match nothing and silently write EMPTY values — every unit would then start with an empty DSN and the whole deploy
# would break in a way that looks like a database outage. Requiring a pre-provisioned file makes that impossible:
# either the secrets are present and validated, or the install refuses to proceed.
# {LAUNCH_FLEET.SH:31 (POST-FIX) "EXPORT WATEREVENTS_DB_DSN=\"${WATEREVENTS_DB_DSN:?SET WATEREVENTS_DB_DSN ...}\"" —
#  the old extractor regex `s/^export VAR=\"\${VAR:-\(.*\)}\".*/\1/p` REQUIRES the `:-` default form and cannot match}
# [CONFIDENCE: CONFIRMED 100% — the `:-` vs `:?` distinction is what the sed pattern keys on; verified by reading both].
[ -r "$ENVF" ] || {
  cat >&2 <<MSG
[install] FATAL: $ENVF is missing (or unreadable by root).

Secrets are no longer stored in the repo, so this installer cannot synthesise them any more.
Provision the file out-of-band FIRST, then re-run this script:

  sudo install -m 600 -o root -g root $SRC/waterevents.env.template $ENVF
  sudo \$EDITOR $ENVF     # append WATEREVENTS_DB_DSN=..., QWEN_API_KEY=..., and optionally WEBSHARE_PROXY=...

MSG
  exit 2
}

# Verify each REQUIRED key is present AND non-empty. Collect every failure before exiting so the operator fixes the
# file once instead of rediscovering one missing key per run.
# WEBSHARE_PROXY is deliberately NOT required — unset merely leaves the tier-2 residential render lane dormant, which
# degrades bot-walled hosts to zero events but does not stop the fleet.
# {WATEREVENTS.ENV.EXAMPLE:33-35 "UNSET = TIER DORMANT: EVERY BOT-WALLED / IP-TARPITTED HOST ... IS NEVER RETRIED
#  THROUGH A RESIDENTIAL IP → PERMANENT 0-EVENTS FOR THOSE COMPANIES"} [CONFIDENCE: CONFIRMED 100% — repo env example].
MISSING=()
for K in WATEREVENTS_DB_DSN QWEN_API_KEY QWEN_BASE_URLS WATEREVENTS_DB_SCHEMA PYTHONPATH; do
  # `^K=` then strip the key; -z catches both "key absent" and "key present but empty" (KEY= with nothing after it).
  V=$(sed -n "s/^${K}=//p" "$ENVF" | head -1)
  [ -n "$V" ] || MISSING+=("$K")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "[install] FATAL: $ENVF is missing or has empty values for: ${MISSING[*]}" >&2
  echo "[install] add each as KEY=value (no quotes, no shell expansion — systemd EnvironmentFile is literal)." >&2
  exit 2
fi
chmod 600 "$ENVF"; chown root:root "$ENVF"
echo "[install] verified $ENVF ($(grep -c . "$ENVF") lines, all required keys present)"

mkdir -p "$LOGD" && chown "$RUN_USER" "$LOGD"

# ── units ───────────────────────────────────────────────────────────────────────────────────────────────────────────
install -m 644 "$SRC/waterevents-worker@.service" "$SRC/waterevents-pacer.service" \
               "$SRC/waterevents-webapp.service"  "$SRC/waterevents-fleet.target" \
               "$SRC/waterevents-watchdog.service" "$SRC/waterevents-watchdog.timer" \
               "$SRC/waterevents-janitor.service"  "$SRC/waterevents-janitor.timer" \
               "$SRC/waterevents-reaper.service"   "$SRC/waterevents-reaper.timer" /etc/systemd/system/

# The watchdog script is installed to a stable absolute path because the unit's ExecStart cannot depend on the repo
# checkout being present/readable by root at activation time.
install -m 755 "$DEPLOY/watchdog.sh" /usr/local/bin/waterevents-watchdog
# Same for the reaper. This installer did not previously mention the reaper AT ALL — not a broken path, an omission —
# while the timer ran every two minutes in production. A rebuild from this repo would have produced a host with no
# reaper, and the symptom is rows sitting in 'running' with nothing working them, which reads as a busy fleet.
# {SHELL 2026-07-29 "grep -c reaper backend/deploy/systemd/install.sh → 0, while the host timer showed ACTIVE"}
# [CONFIDENCE: CONFIRMED 100% — the omission was found by grepping this file for the unit name.]
install -m 755 "$DEPLOY/reaper.sh" /usr/local/bin/waterevents-reaper

# Watchdog state dir — the debounce counter and the post-restart suppression timestamp persist here BETWEEN timer
# firings (each firing is a fresh oneshot process, so in-memory state cannot survive). Root-owned: the watchdog runs
# as root because it calls systemctl restart.
install -d -m 755 -o root -g root /var/lib/waterevents

# logrotate: launch_fleet.sh APPENDS to worker logs (it must never truncate — a `>` redirect destroyed the crash-window
# evidence during the 2026-07-27 incident recovery), so something else has to bound them. That something is this file.
# {LAUNCH_FLEET.SH:64 "SIZE IS BOUNDED BY LOGROTATE (DEPLOY/WATEREVENTS-LOGROTATE.CONF) RATHER THAN BY
#  TRUNCATION-ON-START"} [CONFIDENCE: CONFIRMED 100% — the script documents the dependency; the file it named did not
#  exist until this change, so logs were in fact unbounded].
install -m 644 "$DEPLOY/waterevents-logrotate.conf" /etc/logrotate.d/waterevents

systemctl daemon-reload
echo "[install] units + watchdog + janitor + logrotate installed"

# --- retire any session-scoped fleet started by launch_fleet.sh (this is what we are replacing) ---
pkill -f '[e]vent_agent.scheduler.worker'       2>/dev/null || true
pkill -f '[e]vent_agent.scheduler.solver.pacer' 2>/dev/null || true
pkill -f '[d]ev-api-server'                     2>/dev/null || true
sleep 3

# --- enable + start ---
for i in $(seq 1 "$N"); do systemctl enable -q --now "waterevents-worker@$i.service"; done
systemctl enable -q --now waterevents-pacer.service waterevents-webapp.service waterevents-fleet.target
systemctl enable -q --now waterevents-watchdog.timer waterevents-janitor.timer waterevents-reaper.timer
echo "[install] started $N workers + pacer + webapp + watchdog/janitor/reaper timers"

# ── assert the supervision actually landed ──────────────────────────────────────────────────────────────────────────
# WHY assert instead of trusting the enable above: the previous installer ended with an unconditional success message
# while installing NO watchdog at all, so the operator's only signal ("[install] started ...") was true and useless.
# An install that silently produces an unsupervised fleet is the precondition for another multi-hour unnoticed wedge —
# the 2026-07-27 outage ran 4h27m specifically because nothing was watching. Fail the install loudly instead.
# {COMMIT 5b91b6a "NOTHING SUPERVISED IT AND NOTHING ALERTED, SO IT LAY DEAD FOR 4H27M"}
# [CONFIDENCE: CONFIRMED 100% — quoted from the incident commit message].
FAILED=()
systemctl is-enabled --quiet waterevents-watchdog.timer || FAILED+=("waterevents-watchdog.timer")
systemctl is-enabled --quiet waterevents-janitor.timer  || FAILED+=("waterevents-janitor.timer")
# The reaper gets the same assertion as the other two. It is the component whose absence is hardest to notice — a
# missing reaper leaves rows in 'running' that nothing is working, and a queue full of claimed-but-idle rows looks
# exactly like a busy fleet from every other number on the dashboard.
systemctl is-enabled --quiet waterevents-reaper.timer   || FAILED+=("waterevents-reaper.timer")
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "[install] FATAL: these units are NOT enabled: ${FAILED[*]}" >&2
  echo "[install] the fleet would run UNSUPERVISED — refusing to report success." >&2
  exit 3
fi
echo "[install] verified: watchdog + janitor timers are enabled"

systemctl --no-pager --no-legend list-units 'waterevents*' | sed 's/^/  /'
