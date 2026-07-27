#!/bin/bash
# Drive the 14B + 32B collapse-sweep eval on h20 free GPUs, resilient to ssh drops. Waits for each model's download,
# serves it (own GPU + port, low gpu-mem-util so it coexists with the wan_va work), waits until ready, runs qeval, then
# moves to the next. All output → drive_eval.log. Poll that log; do NOT re-run.
set -u
LOG=/mnt/data/yongzhe/qwen_logs/drive_eval.log
MODELS=/mnt/data/yongzhe/models
WE=/mnt/data/yongzhe/WaterEvents
echo "=== drive_eval start $(date) ===" >> "$LOG"

run_one() {  # name  minMB  gpu  port  util
  local name=$1 minmb=$2 gpu=$3 port=$4 util=$5
  local dir="$MODELS/Qwen2.5-$name-Instruct-AWQ"
  echo "[$name] waiting for download (need ${minmb}MB) ..." >> "$LOG"
  while [ "$(du -sm "$dir" 2>/dev/null | cut -f1)" -lt "$minmb" ]; do sleep 20; done
  echo "[$name] download done ($(du -sh "$dir"|cut -f1)); launching serve on gpu$gpu:$port" >> "$LOG"
  CUDA_VISIBLE_DEVICES=$gpu nohup python -m vllm.entrypoints.openai.api_server \
    --model "$dir" --served-model-name "q$name" --port "$port" \
    --quantization awq_marlin --max-model-len 16384 --gpu-memory-utilization "$util" --enforce-eager \
    > "/mnt/data/yongzhe/qwen_logs/serve_q$name.log" 2>&1 &
  local spid=$!
  echo "[$name] serve pid $spid; waiting ready ..." >> "$LOG"
  local tries=0
  until [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$port/v1/models 2>/dev/null)" = "200" ]; do
    sleep 10; tries=$((tries+1)); if [ $tries -gt 90 ]; then echo "[$name] SERVE TIMEOUT" >> "$LOG"; return 1; fi
  done
  echo "[$name] SERVING. running qeval ↓" >> "$LOG"
  echo "########## MODEL q$name ##########" >> "$LOG"
  EVAL_MODEL="q$name" EVAL_BASE="http://127.0.0.1:$port/v1" WE_ROOT="$WE" python -u "$WE/tests/qeval.py" >> "$LOG" 2>&1
  echo "[$name] qeval done; stopping serve pid $spid" >> "$LOG"
  kill "$spid" 2>/dev/null
  sleep 5
}

run_one 14B 8500 4 8001 0.35
run_one 32B 18000 5 8002 0.55
echo "=== drive_eval ALL DONE $(date) ===" >> "$LOG"
