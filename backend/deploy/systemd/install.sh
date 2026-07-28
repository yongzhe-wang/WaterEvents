#!/usr/bin/env bash
# install.sh — put the WaterEvents fleet under systemd. Idempotent; safe to re-run.
#
# 用一句话讲完: 把 unit 文件装到 /etc/systemd/system → 从 launch_fleet.sh 里抽出三个 secret 拼成 600 权限的
# /etc/waterevents.env → 杀掉旧的「挂在 ssh session scope 里」的 fleet → enable+start N 个 worker + pacer + webapp。
# WHY: nohup 起的进程留在 ssh session 的 systemd scope 里,会话被回收时整棵树一起死 —— 2026-07-28 03:00 就这么丢了
# 一次 fleet。systemd unit 既让它活过会话,又给了 Restart=always 和 cgroup 内存上限。
#
# Usage:  sudo N=6 bash backend/deploy/systemd/install.sh
set -euo pipefail
N="${N:-6}"
REPO="${EVENTINC_HOME:-/home/thebigsun/WaterEvents}"
SRC="$REPO/backend/deploy/systemd"
FLEET_SH="$REPO/backend/deploy/launch_fleet.sh"
ENVF=/etc/waterevents.env

[ -r "$FLEET_SH" ] || { echo "missing $FLEET_SH — cannot source secrets"; exit 2; }

# --- env file: template + the three secrets, lifted verbatim out of launch_fleet.sh so there is exactly ONE
# --- place secrets live in the repo (they are still committed there; rotating them is a separate task).
install -m 600 -o root -g root "$SRC/waterevents.env.template" "$ENVF"
for K in WATEREVENTS_DB_DSN QWEN_API_KEY WEBSHARE_PROXY; do
  V=$(sed -n "s/^export ${K}=\"\${${K}:-\(.*\)}\".*/\1/p" "$FLEET_SH" | head -1)
  [ -n "$V" ] && echo "${K}=${V}" >> "$ENVF"
done
chmod 600 "$ENVF"
echo "[install] wrote $ENVF ($(grep -c . "$ENVF") vars)"

# --- units ---
install -m 644 "$SRC/waterevents-worker@.service" "$SRC/waterevents-pacer.service" \
               "$SRC/waterevents-webapp.service"  "$SRC/waterevents-fleet.target" /etc/systemd/system/
systemctl daemon-reload
echo "[install] units installed"

# --- retire any session-scoped fleet started by launch_fleet.sh (this is what we are replacing) ---
pkill -f '[e]vent_agent.scheduler.worker'       2>/dev/null || true
pkill -f '[e]vent_agent.scheduler.solver.pacer' 2>/dev/null || true
pkill -f '[d]ev-api-server'                     2>/dev/null || true
sleep 3

# --- enable + start ---
for i in $(seq 1 "$N"); do systemctl enable -q --now "waterevents-worker@$i.service"; done
systemctl enable -q --now waterevents-pacer.service waterevents-webapp.service waterevents-fleet.target
echo "[install] started $N workers + pacer + webapp"
systemctl --no-pager --no-legend list-units 'waterevents*' | sed 's/^/  /'
