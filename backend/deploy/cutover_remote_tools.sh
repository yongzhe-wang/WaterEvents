#!/usr/bin/env bash
# cutover_remote_tools.sh — flip the ir-media-8 fleet from in-process render/docling/whisper to the remote services.
#
# 用一句话讲完: 往 /etc/waterevents/fleet.env 里加三个 URL 变量 → 把**所有** waterevents 单元列全并重启 → 逐个确认新
# 变量真的进了进程环境 → 看一眼三个服务的健康。回滚就是 --revert,把三行注释掉再重启,秒级。
#
# WHY 这个脚本存在而不是手敲: 上一次迁移我只重启了 worker@1-@4,而 worker@5 和 @6 也在跑,继续往旧数据库写了一个多
# 小时才被发现。`systemctl list-units "waterevents*" --all` 是那次的教训 —— **枚举,不要凭记忆列**。
# {INCIDENT 2026-08-03 — worker@5/@6 KEPT WRITING TO THE OLD DATABASE FOR OVER AN HOUR AFTER THE MIGRATION}
# [CONFIDENCE: CONFIRMED — observed during the Supabase migration in this same fleet].
#
# 用法:  ./cutover_remote_tools.sh          # 切到远程
#        ./cutover_remote_tools.sh --revert # 回滚到进程内
set -u
ENVF=/etc/waterevents/fleet.env
REVERT=0
[ "${1:-}" = "--revert" ] && REVERT=1

RENDER=http://10.128.0.11:8100      # ir-render-16, VPC 内网直连, 不过公网
DOCLING=http://127.0.0.1:8101       # tools-tunnel 转发到 RunPod 的 CPU 进程
WHISPER=http://127.0.0.1:8102       # tools-tunnel 转发到 RunPod 的 GPU 进程

echo "=== 1. 切换前: 三个服务必须都是健康的 ==="
# 先探活再改配置。改完才发现服务是死的, 等于把整个 fleet 推进一个所有渲染都返回 transport-error 的状态。
fail=0
for pair in "render:$RENDER" "docling:$DOCLING" "whisper:$WHISPER"; do
  n=${pair%%:*}; u=${pair#*:}
  h=$(curl -s --max-time 10 "$u/health" 2>/dev/null)
  if [ -z "$h" ]; then echo "  ✗ $n ($u) 无响应"; fail=1; else echo "  ✓ $n  $h"; fi
done
# whisper 落到 CPU 是"服务活着但没用" —— 那正是这次拆分要消灭的问题, 不能让它悄悄通过
if curl -s --max-time 10 "$WHISPER/health" 2>/dev/null | grep -q '"device": "cpu"'; then
  echo "  ✗ whisper 起在 CPU 上了 (期望 cuda) — 拒绝切换"; fail=1
fi
if [ "$REVERT" = 0 ] && [ "$fail" = 1 ]; then echo "预检失败, 中止。"; exit 1; fi

echo "=== 2. 改 fleet.env ==="
sudo cp -f "$ENVF" "${ENVF}.bak-$(date -u +%Y%m%d%H%M%S)"
sudo sed -i '/^RENDER_REMOTE_URL=/d;/^DOCLING_REMOTE_URL=/d;/^WHISPER_REMOTE_URL=/d' "$ENVF"
if [ "$REVERT" = 0 ]; then
  printf 'RENDER_REMOTE_URL=%s\nDOCLING_REMOTE_URL=%s\nWHISPER_REMOTE_URL=%s\n' \
    "$RENDER" "$DOCLING" "$WHISPER" | sudo tee -a "$ENVF" >/dev/null
  echo "  已写入 3 个 URL"
else
  echo "  已删除 3 个 URL — 回到进程内实现"
fi

echo "=== 3. 枚举所有 waterevents 单元并重启 ==="
# --all 是关键: 不带它, 一个当时恰好 inactive 的单元不会出现在列表里, 然后被漏掉。
UNITS=$(systemctl list-units 'waterevents*' --all --no-legend --plain 2>/dev/null | awk '{print $1}' | grep -v '^$')
echo "$UNITS" | sed 's/^/  /'
echo "  共 $(echo "$UNITS" | wc -l) 个单元"
for u in $UNITS; do sudo systemctl restart "$u" 2>/dev/null; done
sleep 8

echo "=== 4. 逐个确认新环境真的进了进程 (不是只改了文件) ==="
for u in $UNITS; do
  pid=$(systemctl show "$u" -p MainPID --value 2>/dev/null)
  st=$(systemctl is-active "$u" 2>/dev/null)
  if [ -n "$pid" ] && [ "$pid" != "0" ] && sudo test -r "/proc/$pid/environ"; then
    n=$(sudo tr '\0' '\n' < "/proc/$pid/environ" | grep -c '_REMOTE_URL=')
    echo "  $u  $st  pid=$pid  REMOTE_URL 变量数=$n"
  else
    echo "  $u  $st  (无主进程)"
  fi
done

echo "=== 5. 切换后 90 秒内的 transport-error 计数 (应该是 0) ==="
# transport-error 只有一个来源: 我们自己的服务不可达。非零就说明拆分本身有问题, 而不是某个网站难爬。
sleep 90
n=$(sudo journalctl -u 'waterevents*' --since '-90s' --no-pager 2>/dev/null | grep -c 'transport-error')
echo "  transport-error x $n"
[ "$n" -gt 0 ] && echo "  ⚠ 非零 — 用 --revert 回滚, 然后查三个服务的日志"
echo CUTOVER_DONE
