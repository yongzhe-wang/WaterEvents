#!/usr/bin/env bash
# resmon — 每分钟把 render VM 的资源计数追加一行。存在的唯一理由: 2026-08-10 那次 wedge 无法诊断。
#
# 用一句话讲完: 那次故障里内核一直健康(ping 通、TCP accept 正常、无 OOM、无 hung task、无 I/O 错误), 而所有
# 用户态进程在 06:16:05 同时停止写入 —— 这种形状指向系统级资源耗尽, 但**记录这个错误本身需要那个已经耗尽的
# 资源**, 所以日志里什么都没有。这个脚本把计数写在故障之前, 于是下次有得看。
#
# {2026-08-10 — previous boot's last log line 06:16:05, preceded by rsyslogd 'omfile' suspend/resume thrashing;
#  journalctl -k -b -1 → 0 OOM lines, 0 "blocked for more than", 0 I/O errors; disk 12% and inodes 2% AFTER the
#  reset, and a reset frees neither, so it was never full}
# [CONFIDENCE: CONFIRMED 100% — all four counts read from the wedged host's own logs after recovery. The CAUSE
#  remains unidentified; this file exists precisely because it could not be identified.]
#
# 上游触发: waterevents-resmon.timer(每分钟)。下游连接: 无 —— 只写文件, 出事后人看。
set -uo pipefail
LOG=/var/log/waterevents-resmon.log
PID=$(systemctl show waterevents-render -p MainPID --value 2>/dev/null)
read -r ALLOC _ MAXFD < /proc/sys/fs/file-nr
# 单独数 render 自己的 fd 和线程: 系统级计数被别的进程稀释, 而泄漏几乎总是发生在一个进程里。
RFD=$(ls "/proc/$PID/fd" 2>/dev/null | wc -l)
RTH=$(awk '/^Threads:/{print $2}' "/proc/$PID/status" 2>/dev/null)
RRSS=$(awk '/^VmRSS:/{print $2}' "/proc/$PID/status" 2>/dev/null)
# ESTABLISHED 单独计: 跟随跳转让每次抓取最多开 6 条连接而不是 1 条, 若有 socket 泄漏这里最先长起来。
EST=$(ss -tn state established 2>/dev/null | wc -l)
# 两条车道的在飞数 —— RSS 曲线跟哪条相关, 哪条就是漏的那条。单看总内存分不出 browser 和 fetch。
LANES=$(curl -s --max-time 3 http://127.0.0.1:8100/health 2>/dev/null | python3 /usr/local/bin/waterevents-lanes.py 2>/dev/null || echo "br=? fe=?")
printf '%s fd=%s procs=%s threads=%s render_fd=%s render_threads=%s render_rss_kb=%s est_conn=%s load=%s disk_pct=%s %s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ALLOC" "$(ls /proc | grep -c '^[0-9]')" "$(ps -eLf 2>/dev/null | wc -l)" \
  "${RFD:-?}" "${RTH:-?}" "${RRSS:-?}" "$EST" "$(cut -d' ' -f1 /proc/loadavg)" \
  "$(df --output=pcent / | tail -1 | tr -d ' %')" "$LANES" >> "$LOG" 2>/dev/null
