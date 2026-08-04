#!/usr/bin/env bash
# supervise_tools.sh — keep ONE tools.service process alive on the RunPod pod.
#
# 用一句话讲完: 拿 flock 独占锁 → 起一个 tools.service 进程 → 它退出就重启 → 连续 3 次秒退就在日志里大声喊
# CRASH-LOOPING。跟 supervise_vl.sh 是同一个模式,因为 pod 是容器,**没有 systemd**,保活只能自己做。
#
# WHY 这个脚本要带参数而不是写死: docling 和 whisper 是同一份 tools/service.py 起两遍,唯一的区别是设备可见性和端口 ——
# docling 要 CUDA_VISIBLE_DEVICES="" 吃 96 个空闲 CPU 核,whisper 要 CUDA_VISIBLE_DEVICES=0 吃 A40。一个进程里
# 做不到给两个模型分配不同设备,所以只能起两个进程,而两个进程只该有一份保活代码。
# {NVIDIA-SMI 2026-08-04 "NVIDIA A40, 46068 MIB, 40299 MIB USED, 5190 MIB FREE, 100% UTILIZATION"}
# [CONFIDENCE: CONFIRMED — read off the live pod; vLLM owns the VRAM, so Docling has nowhere to go but CPU].
#
# 用法:  ./supervise_tools.sh docling   8101  ""   24
#        ./supervise_tools.sh whisper   8102  "0"   2
#        参数: <名字> <端口> <CUDA_VISIBLE_DEVICES> <并发>
#
# 上游触发: 手工启动或 onstart.sh。下游连接: tools/service.py,再往下是 Docling 单例 / faster-whisper 单例。

set -u
NAME="${1:?usage: supervise_tools.sh <name> <port> <cuda_visible_devices> <concurrency>}"
PORT="${2:?}"
CUDA="${3-}"
CONC="${4:-8}"

# TWO roots, split by file SHAPE, not by importance:
#   V (local overlay disk) — the venv and the code. Thousands of small files; pip on the network volume crawled and a
#     `pip install --upgrade pip wheel` did not finish in 15 minutes.
#   W (/workspace network volume) — model weights, caches, logs. Few large files, and the local disk cannot hold them.
# {DF 2026-08-04 "OVERLAY 20G 14G 6.6G 68% /" vs "MFS#CA-MTL-1.RUNPOD.NET:9421 965T 722T 244T 75% /WORKSPACE"}
# {OBSERVED 2026-08-04 — venv on /workspace: `pip install --upgrade pip wheel` still running after 15+ minutes}
# [CONFIDENCE: CONFIRMED — the stall was observed directly; /workspace is MooseFS over the network].
V=/opt/we
W=/workspace/waterevents
LOG=$W/${NAME}.log
SUP=$W/${NAME}_supervisor.log

# One supervisor per NAME. Without this an onstart re-run would stack a second supervisor on the same port and the two
# would fight: whichever lost the bind would die instantly, be counted as a fast-fail, and trip a false crash-loop
# warning while the healthy process kept serving. Same guard supervise_vl.sh uses.
mkdir -p "$W"
exec 9>"$W/.${NAME}.supervisor.lock"
if ! flock -n 9; then
  echo "[sup:$NAME $(date -u +%FT%TZ)] another supervisor holds the lock — exiting" >> "$SUP"
  exit 0
fi

# Models and caches must live on /workspace: the pod's overlay root has 6.6 GB free, and whisper large-v3 plus
# Docling's layout + TableFormer weights are ~4 GB of that. /workspace is a 965T network volume with 244T available.
# {DF 2026-08-04 "OVERLAY 20G 14G 6.6G 68% /" and "MFS#CA-MTL-1.RUNPOD.NET:9421 965T 722T 244T 75% /WORKSPACE"}
# [CONFIDENCE: CONFIRMED — read off the live pod].
export HF_HOME=$W/hf
export XDG_CACHE_HOME=$W/cache
export PYTHONPATH=$V/WaterEvents/backend
export TOOLS_SERVICE_PORT="$PORT"
export TOOLS_SERVICE_CONCURRENCY="$CONC"
export CUDA_VISIBLE_DEVICES="$CUDA"

# The OCR fallback stays ON: text-first is the fast path, and a scanned pdf still needs the one OCR retry to be read
# at all. Turning it off here would trade a rare slow document for a permanently unreadable one.
export OFFICE_OCR_FALLBACK=1

# On the GPU process, say the device out loud rather than letting transcribe.py infer it. Inference reads
# torch.cuda.is_available(), which is exactly what CUDA_VISIBLE_DEVICES manipulates — correct, but it means a typo in
# the unit args would silently produce a CPU whisper that still answers 200 while running ~100x too slow.
if [ -n "$CUDA" ]; then
  export WHISPER_DEVICE=cuda
  export WHISPER_MAX_DURATION_S=7200          # 2h — a GPU decodes far faster than realtime, unlike the 900s CPU cap

  # int8_float16, NOT float16. large-v3 at float16 is ~3.1 GB, and vLLM leaves exactly 5,190 MiB free — the model
  # loaded fine and then the DECODE had nothing left for activations:
  #   {NVIDIA-SMI 2026-08-04 AFTER THE ATTEMPT: "46068 MIB TOTAL, 45480 MIB USED, 9 MIB FREE";
  #    VLLM::EngineCore 40290 MIB + WHISPER PROCESS 5176 MIB}
  #   {WHISPER.LOG 2026-08-04 "LARGE-V3 TRANSCRIBE FAILED (RUNTIMEERROR: CUDA FAILED WITH ERROR OUT OF MEMORY)
  #    → FALLBACK MEDIUM" then "WHISPER MEDIUM LOAD FAILED (RUNTIMEERROR: CUDA FAILED WITH ERROR OUT OF MEMORY)"}
  # int8_float16 halves the weights to ~1.6 GB and leaves ~3.5 GB for activations and the CUDA context, WITHOUT
  # touching vLLM's allocation — vLLM is the tenant that is currently earning, and shrinking it to make room for
  # transcription would trade a known-good service for an unproven one.
  # [CONFIDENCE: CONFIRMED — the OOM and the 9 MiB free reading are both from the live pod].
  export WHISPER_COMPUTE=${WHISPER_COMPUTE:-int8_float16}

  # And only ONE decode at a time on a card we do not own. Two concurrent decodes double the activation footprint
  # against a 3.5 GB ceiling that is already shared with a 100%-utilized vLLM.
  export TOOLS_SERVICE_CONCURRENCY=1
fi

fails=0                                        # consecutive FAST (<30s) exits = crash-loop signal, not a transient
while true; do
  echo "[sup:$NAME $(date -u +%FT%TZ)] starting on :$PORT (CUDA='$CUDA' conc=$CONC)" >> "$SUP"
  t0=$(date +%s)
  "$V/venv/bin/python" -u -m tools.service >> "$LOG" 2>&1
  rc=$?
  dt=$(( $(date +%s) - t0 ))                   # how long it stayed up — under 30s means it never really served
  if [ "$dt" -lt 30 ]; then fails=$((fails + 1)); else fails=0; fi
  echo "[sup:$NAME $(date -u +%FT%TZ)] exited rc=$rc after ${dt}s (fast-fails=$fails)" >> "$SUP"
  if [ "$fails" -ge 3 ]; then                  # three instant deaths in a row → not transient, be LOUD
    echo "[sup:$NAME $(date -u +%FT%TZ)] WARNING $NAME CRASH-LOOPING ($fails fast fails, rc=$rc) — CHECK $LOG" >> "$SUP"
  fi
  if [ "$fails" -gt 0 ]; then
    s=$(( fails * 10 )); [ "$s" -gt 60 ] && s=60   # linear backoff capped at 60s, same shape as supervise_vl.sh
  else
    s=5
  fi
  sleep "$s"
done
