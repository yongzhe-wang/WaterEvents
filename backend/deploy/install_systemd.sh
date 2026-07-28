#!/usr/bin/env bash
# install_systemd — put the fleet under systemd so it is BOUNDED, SUPERVISED and OBSERVED.
#
# 用一句话讲完: 把原来 nohup 起的 6 worker + pacer 换成 systemd 单元 —— 每个 worker 一个实例、各自带 MemoryMax cgroup
# 上限(超了只杀这一个并 10 秒后自动重启,而不是把整机拖进 thrash 活锁)、Restart=always 保证进程被杀能自愈、
# 再加一个每 5 分钟查 scan_log 的 watchdog 和一个清 traces 的 janitor。装完 `systemctl start waterevents.target` 即可。
#
# WHY this exists — the 2026-07-27 22:11 incident: 6 workers × (3 browsers, 24 pages) on a 32GB box with NO swap drove
# memory to exhaustion; with no swap the kernel could only reclaim page cache, so it evicted the running processes' own
# executable pages and immediately re-faulted them — disk reads pinned at exactly 3600.0 ops/s (~140-176 MB/s, ~40KB/op:
# the refault+readahead signature) for 4.5 hours while writes starved to ~0. journald could not log, systemd-networkd
# could not renew the DHCP lease (ens4: Failed), sshd could not complete auth, and every worker blocked — yet the box
# stayed "up" at ~17% CPU (pure iowait) and the OOM killer never fired, because reclaim always technically succeeded.
# Nothing supervised it and nothing alerted, so it lay dead for 4h27m.
# {INCIDENT 2026-07-27: last scan 22:11:11Z, read ops pinned 3600.0/s 22:30→02:40, write ops 0.0-0.6/s, CPU flat ~17%,
#  zero oom-kill lines, zero host-maintenance operations, zero cron/timer activity in the window}
# [CONFIDENCE: CONFIRMED 100% for the disk/CPU/host facts (GCE metrics + persistent journal); the memory-exhaustion link
#  is INFERRED 85% — this VM had no Ops Agent so no memory series exists, and the crash-window logs were unwritable.
#  MemoryMax below makes the inference moot: if memory is the cause the cgroup now bounds it, and if it is not, the
#  limit costs nothing.]
#
# Usage (on the VM):  sudo bash backend/deploy/install_systemd.sh
set -euo pipefail

HOME_DIR="${EVENTINC_HOME:-/home/thebigsun/WaterEvents}"
CODE_DIR="$HOME_DIR/backend"
PY="${EVENTINC_PY:-/home/thebigsun/venv/bin/python}"
RUN_USER="${EVENTINC_USER:-thebigsun}"
LOGD="${EVENTINC_LOGD:-/home/$RUN_USER/eventinc_fleet}"
N="${N:-6}"
WORKER_MEM="${WORKER_MEM:-3G}"        # 6 × 3G = 18G ceiling, leaving 14G for pacer + webapp + browsers' shared pages + OS
WORKER_MEM_HIGH="${WORKER_MEM_HIGH:-2500M}"   # throttle-first threshold: reclaim pressure kicks in BELOW the hard kill
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"

[ -x "$PY" ] || { echo "python not found at $PY" >&2; exit 2; }
[ -d "$CODE_DIR" ] || { echo "code dir not found at $CODE_DIR" >&2; exit 2; }

# ── env snapshot ────────────────────────────────────────────────────────────────────────────────────────────────────
# Resolve the fleet env EXACTLY as launch_fleet.sh would (its `export FOO="${FOO:-default}"` lines), then freeze it into
# a root-only EnvironmentFile. WHY eval the real script's lines instead of restating them: one source of truth, so the
# systemd path can never drift from the manual path — and no second copy of the credentials enters git.
mkdir -p /etc/waterevents
set -a; eval "$(grep -E '^export ' "$DEPLOY_DIR/launch_fleet.sh")"; set +a
: > /etc/waterevents/fleet.env
for v in WATEREVENTS_DB_DSN WATEREVENTS_DB_SCHEMA WATEREVENTS_RUN_ID QWEN_BASE_URLS QWEN_SERVED_NAME QWEN_API_KEY \
         WATERCRAWL_NO_SHOT EVENT_USE_IMAGE WATERCRAWL_HTTP_FIRST WEBSHARE_PROXY EVENT_MAX_PAGES EVENT_BATCH \
         EVENT_COMPANY_BUDGET_S EVENTINC_WORKERS EVENTINC_TOP_K EVENTINC_PROFILE \
         IR_WATERCRAWL_MAX_PAGES IR_WATERCRAWL_BROWSERS; do
  printf '%s=%s\n' "$v" "${!v-}" >> /etc/waterevents/fleet.env
done
printf 'PYTHONPATH=%s\n' "$CODE_DIR" >> /etc/waterevents/fleet.env
chmod 600 /etc/waterevents/fleet.env
echo "[install] wrote /etc/waterevents/fleet.env (mode 600, $(wc -l < /etc/waterevents/fleet.env) vars)"

mkdir -p "$LOGD" && chown "$RUN_USER" "$LOGD"

# ── target ──────────────────────────────────────────────────────────────────────────────────────────────────────────
cat > /etc/systemd/system/waterevents.target <<EOF
[Unit]
Description=WaterEvents crawl fleet (workers + pacer)
Wants=network-online.target
After=network-online.target

[Install]
WantedBy=multi-user.target
EOF

# ── worker template ─────────────────────────────────────────────────────────────────────────────────────────────────
# MemoryMax is the whole point: the incident's failure mode was UNBOUNDED growth with nowhere to shed it. With a cgroup
# ceiling the kernel reclaims/kills INSIDE one worker's cgroup, so a leak costs one worker + a 10s restart instead of
# the entire host. MemoryHigh throttles first so a spike gets back-pressure before it gets killed.
cat > /etc/systemd/system/waterevents-worker@.service <<EOF
[Unit]
Description=WaterEvents crawl worker %i
PartOf=waterevents.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$CODE_DIR
EnvironmentFile=/etc/waterevents/fleet.env
ExecStart=$PY -m agent.event_agent.scheduler.worker
Restart=always
RestartSec=10
MemoryHigh=$WORKER_MEM_HIGH
MemoryMax=$WORKER_MEM
CPUQuota=100%
OOMPolicy=continue
StandardOutput=append:$LOGD/w%i.log
StandardError=append:$LOGD/w%i.log

[Install]
WantedBy=waterevents.target
EOF

# ── pacer ───────────────────────────────────────────────────────────────────────────────────────────────────────────
cat > /etc/systemd/system/waterevents-pacer.service <<EOF
[Unit]
Description=WaterEvents packing-solver pacer
PartOf=waterevents.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$CODE_DIR
EnvironmentFile=/etc/waterevents/fleet.env
ExecStart=$PY -m agent.event_agent.scheduler.solver.pacer --loop
Restart=always
RestartSec=30
MemoryMax=512M
StandardOutput=append:$LOGD/pacer.log
StandardError=append:$LOGD/pacer.log

[Install]
WantedBy=waterevents.target
EOF

# ── watchdog (liveness by OUTPUT, not by process presence) ──────────────────────────────────────────────────────────
install -m 755 "$DEPLOY_DIR/watchdog.sh" /usr/local/bin/waterevents-watchdog
cat > /etc/systemd/system/waterevents-watchdog.service <<EOF
[Unit]
Description=WaterEvents fleet liveness watchdog

[Service]
Type=oneshot
EnvironmentFile=/etc/waterevents/fleet.env
Environment=PSQL_BIN=$(command -v psql || echo /usr/bin/psql)
ExecStart=/usr/local/bin/waterevents-watchdog
EOF
cat > /etc/systemd/system/waterevents-watchdog.timer <<EOF
[Unit]
Description=Run the WaterEvents liveness watchdog every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

# ── janitor (traces grew to 6,314 dirs / 56,665 files / 2.4GB before the first manual prune) ────────────────────────
cat > /etc/systemd/system/waterevents-janitor.service <<EOF
[Unit]
Description=WaterEvents trace retention (keep newest 500 crawl traces)

[Service]
Type=oneshot
User=$RUN_USER
ExecStart=/bin/bash -c 'T=$CODE_DIR/agent/event_agent/crawl/traces; [ -d "\$T" ] || exit 0; ls -1dt "\$T"/*/ 2>/dev/null | tail -n +501 | tr "\\n" "\\0" | xargs -0 -r rm -rf'
EOF
cat > /etc/systemd/system/waterevents-janitor.timer <<EOF
[Unit]
Description=Prune WaterEvents crawl traces daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── logrotate (bounds the now-appending logs) ───────────────────────────────────────────────────────────────────────
cat > /etc/logrotate.d/waterevents <<EOF
$LOGD/*.log {
    daily
    rotate 7
    size 100M
    compress
    missingok
    notifempty
    copytruncate
}
EOF

systemctl daemon-reload
systemctl enable waterevents.target waterevents-pacer.service >/dev/null 2>&1 || true
for i in $(seq 1 "$N"); do systemctl enable "waterevents-worker@$i.service" >/dev/null 2>&1 || true; done
systemctl enable --now waterevents-watchdog.timer waterevents-janitor.timer >/dev/null 2>&1 || true

echo "[install] units installed. N=$N workers, MemoryMax=$WORKER_MEM each"
echo "[install] start:  sudo systemctl start waterevents.target"
echo "[install] status: systemctl status 'waterevents-worker@*' waterevents-pacer"
