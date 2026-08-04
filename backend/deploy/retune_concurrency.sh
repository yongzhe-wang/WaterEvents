#!/usr/bin/env bash
# retune_concurrency.sh — raise fleet concurrency now that render/docling/whisper no longer compete for ir-media-8's cores.
#
# 用一句话讲完: 量 5 分钟基线 → 改 EVENTINC_WORKERS → 重启 worker → 再量 5 分钟 → 把吞吐和 VLM 抖动指标并排打出来。
# 变差就用 --revert 退回去。**不是"改完就走"** —— 上一次 24→36 让 VLM 单元慢了 2.8 倍,那种事必须当场看见。
#
# WHY 现在可以提: 三种重负载搬走之后, ir-media-8 只剩发请求和写 DB, 而 EVENTINC_WORKERS=4 是当初"渲染 + Docling +
# whisper 共享 8 个核"时调出来的数。拆分后的实测:
#   {2026-08-04 ir-render-16 load 2.78 / 16 cores = 17% — 渲染侧有 5.8 倍余量}
#   {2026-08-04 vLLM /metrics: num_requests_running=47, num_requests_waiting{reason="capacity"}=0,
#    num_preemptions_total=0 — 忙但不排队不抖动}
#   {2026-08-04 ir-media-8 load 0.47, chrome 进程 0 个}
# [CONFIDENCE: CONFIRMED — 三处都是活机器上读的].
#
# WHY 只提到 6 而不是一步到位: 渲染侧有 5.8 倍余量, 但 VLM 是未知数 —— 它已经有 47 个并发在跑, 而重构前的实测是
# 24→36 会让 VLM 单元慢 2.8 倍(那时 VLM 饱和)。VLM 之后升级过, 但"升级过"不等于"量过"。先 1.5 倍, 看数据, 再决定。
#
# 用法:  ./retune_concurrency.sh 6        # 每 worker 6 个 asyncio slot → 6×6=36
#        ./retune_concurrency.sh --revert # 退回 4
set -u
ENVF=/etc/waterevents/fleet.env
D=~/eventinc_fleet
WINDOW=${RETUNE_WINDOW_S:-300}

if [ "${1:-}" = "--revert" ]; then NEW=4; else NEW="${1:?usage: retune_concurrency.sh <N|--revert>}"; fi
OLD=$(sudo grep -oP '^EVENTINC_WORKERS=\K[0-9]+' "$ENVF" 2>/dev/null || echo 4)
NW=$(systemctl list-units 'waterevents-worker@*' --all --no-legend --plain 2>/dev/null | wc -l)

count() { cat $D/w*.log 2>/dev/null | grep -c "^\[eventinc\].*incremental"; }
vllm()  { curl -s --max-time 8 http://127.0.0.1:8000/metrics 2>/dev/null \
          | grep -E "^vllm:num_(requests_running|requests_waiting\{|preemptions_total)" | awk '{printf "%s=%s ", $1, $2}'; }

echo "=== 基线 ($OLD × $NW = $((OLD*NW)) slots), ${WINDOW}s 窗口 ==="
b0=$(count); v0=$(vllm); sleep "$WINDOW"; b1=$(count)
BASE=$(( b1 - b0 ))
echo "  完成 $BASE 单元 / ${WINDOW}s  →  $(( BASE * 3600 / WINDOW )) 单元/小时"
echo "  VLM 前: $v0"
echo "  VLM 后: $(vllm)"

echo "=== 改到 $NEW × $NW = $((NEW*NW)) slots ==="
sudo cp -f "$ENVF" "${ENVF}.bak-retune-$(date -u +%Y%m%d%H%M%S)"
sudo sed -i "s/^EVENTINC_WORKERS=.*/EVENTINC_WORKERS=$NEW/" "$ENVF"
sudo grep -q '^EVENTINC_WORKERS=' "$ENVF" || echo "EVENTINC_WORKERS=$NEW" | sudo tee -a "$ENVF" >/dev/null
for u in $(systemctl list-units 'waterevents-worker@*' --all --no-legend --plain 2>/dev/null | awk '{print $1}'); do
  sudo systemctl restart "$u"
done
sleep 30                                   # 让 worker 起来并进入稳态再开始计数, 否则重启空窗会污染读数

echo "=== 新配置, 同样 ${WINDOW}s 窗口 ==="
a0=$(count); sleep "$WINDOW"; a1=$(count)
NEWC=$(( a1 - a0 ))
echo "  完成 $NEWC 单元 / ${WINDOW}s  →  $(( NEWC * 3600 / WINDOW )) 单元/小时"
echo "  VLM: $(vllm)"

echo "=== 对比 ==="
echo "  $((OLD*NW)) slots: $BASE 单元    →    $((NEW*NW)) slots: $NEWC 单元"
if [ "$BASE" -gt 0 ]; then
  echo "  吞吐变化: $(( (NEWC - BASE) * 100 / BASE ))%   (理想线性应为 $(( (NEW - OLD) * 100 / OLD ))%)"
fi
# 抖动才是真正的判据, 不是吞吐 —— 吞吐可以在 VLM 开始抢占的同时短暂上升, 然后崩掉
P=$(curl -s --max-time 8 http://127.0.0.1:8000/metrics 2>/dev/null | grep -oP '^vllm:num_preemptions_total\S* \K[0-9.]+' | head -1)
echo "  VLM 抢占累计: ${P:-?}   ← 非 0 说明显存开始抖, 应该退回去"
echo RETUNE_DONE
