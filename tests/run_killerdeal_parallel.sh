#!/usr/bin/env bash
# Manifest-slice MULTI-PROCESS launcher for thekillerdeal — the real fix for the single-event-loop ceiling.
# 用一句话讲完: 先杀光一切 worker/supervisor/chromium/旧 thekillerdeal(硬规矩:每个脚本开头必清场)→ 起 NSLICES 个
# thekillerdeal 进程,每个 taskset 绑一段独立 CPU 核 + 自己的 browser + 跑 100 家的一个 round-robin slice → 真 N 核并行、
# 完美 scoped 到 sample_100、零队列/生产污染 → 全部跑完后聚合 all_events + summary。{USER 2026-07-24 "manifest-slice
# multi-process; every script should remove all the workers first"}.
set -uo pipefail

NSLICES="${KD_NSLICES:-8}"                                   # how many parallel processes (default 8)
CONC="${KD_CONC:-3}"                                         # companies in flight PER process (render-limited anyway)
CODE=/workspace/WaterEvents
OUT="${KD_OUT:-$CODE/tests/thekillerdeal}"                    # respect KD_OUT (a rerun writes elsewhere → don't rm the huge prior full-run trace dir)
# NO SECRET LITERALS — this harness writes to the REAL waterevents schema, so a committed DSN here is both a leaked
# production credential and a loaded gun: anyone running the script from a clone hits live data with no opt-in step.
# `${VAR:?msg}` aborts before the clear-all-workers pkill block below, so a missing secret cannot leave the operator
# with a killed fleet and no run to show for it — the failure happens first, not halfway through.
# {GIT GREP 2026-07-28 "RUN_KILLERDEAL_PARALLEL.SH:13 DSN=\"POSTGRESQL://POSTGRES.EZUVMOLYFGSADKEHJNEF:FOCUSALPHA2026
#  @AWS-1-US-EAST-1.POOLER.SUPABASE.COM:6543/POSTGRES\"; :14 KEY=\"SK-WATEREVENTS-0B1307FDF041607D7E55838C2773204...\""}
# [CONFIDENCE: CONFIRMED 100% — read off the tracked file at HEAD 9d3402f].
DSN="${WATEREVENTS_DB_DSN:?set WATEREVENTS_DB_DSN — this harness writes to the real waterevents schema, so it will not guess a target}"
# No apostrophe — see media_loop.sh: bash applies quote rules to the word of `${VAR:?word}` even within double quotes,
# so "pod's" left the remainder of this file inside an unterminated single-quoted string.
# {BASH -N 2026-07-28 "RUN_KILLERDEAL_PARALLEL.SH: LINE 78: UNEXPECTED EOF WHILE LOOKING FOR MATCHING `''"}
KEY="${QWEN_API_KEY:?set QWEN_API_KEY (the vLLM --api-key of the pod)}"

# ── STANDING RULE: every launcher CLEARS ALL WORKERS first — no ghost fleet draining the queue / competing for the GPU ──
echo "[parallel] clearing ALL workers/supervisors/chromium/old-thekillerdeal…"
pkill -9 -f "agent.event_agent.worker" 2>/dev/null || true
pkill -9 -f "supervise_workers"        2>/dev/null || true
pkill -9 -f "thekillerdeal.py"         2>/dev/null || true
pkill -9 -f "chrome-headless-shell"    2>/dev/null || true
sleep 3
echo "[parallel] cleared. workers=$(pgrep -f '[a]gent.event_agent.worker' | wc -l) chromium=$(pgrep -f '[c]hrome-headless-shell' | wc -l)"

rm -rf "$OUT"; mkdir -p "$OUT/companies"                     # fresh output folder

# ── spawn NSLICES pinned processes with DYNAMIC core width (leave 0-7 for OS/vLLM; split the rest evenly) ──
CORES_START=8
NCORES=$(nproc); AVAIL=$((NCORES - CORES_START))            # cores available for slices
WIDTH=$((AVAIL / NSLICES)); [ "$WIDTH" -lt 1 ] && WIDTH=1    # cores per slice (≥1)
echo "[parallel] ${NCORES} cores, ${WIDTH} cores/slice for ${NSLICES} slices"
# WEBSHARE_PROXY passes through UNSET-as-empty rather than `:?`-required: the residential lane is an optional render
# tier (empty → dormant, the run still completes on the datacenter path), so requiring it would block a legitimate
# no-proxy run. The credential literal formerly defaulted here was the third committed secret in this file.
# {GIT GREP 2026-07-28 "RUN_KILLERDEAL_PARALLEL.SH:38 WEBSHARE_PROXY=\"${WEBSHARE_PROXY:-HTTP://NKNJGKPV:36OO15UCTFHL
#  @192.46.200.43:5713}\""} [CONFIDENCE: CONFIRMED 100% — read off the tracked file at HEAD 9d3402f].
PIDS=()
for i in $(seq 0 $((NSLICES-1))); do
  lo=$((CORES_START + i*WIDTH)); hi=$((lo + WIDTH - 1)); [ "$hi" -ge "$NCORES" ] && hi=$((NCORES-1))
  KD_NSLICES=$NSLICES KD_SLICE_IDX=$i KD_CONC=$CONC KD_RUN_ID="${KD_RUN_ID:-killerdeal}" \
  KD_ALL="${KD_ALL:-}" KD_N="${KD_N:-0}" KD_URLS_FILE="${KD_URLS_FILE:-}" \
  WATERCRAWL_HTTP_FIRST=0 IR_WATERCRAWL_BROWSERS=1 \
  WEBSHARE_PROXY="${WEBSHARE_PROXY:-}" \
  WATEREVENTS_DB_DSN="$DSN" WATEREVENTS_DB_SCHEMA=waterevents \
  QWEN_API_KEY="$KEY" QWEN_BASE_URLS="http://127.0.0.1:8000/v1" PYTHONPATH="$CODE" \
    taskset -c ${lo}-${hi} /root/venv/bin/python "$CODE/tests/thekillerdeal.py" \
    > "$CODE/tests/kd_slice_$i.log" 2>&1 &
  PIDS+=($!)
  echo "[parallel] slice $i → cores ${lo}-${hi} (pid $!)"
done

echo "[parallel] ${NSLICES} slices running; waiting…"
for p in "${PIDS[@]}"; do wait "$p"; done                    # block until every slice finishes

# ── aggregate: per-process files → one all_events.jsonl + one summary ──
cat "$OUT"/all_events_s*.jsonl > "$OUT/all_events.jsonl" 2>/dev/null || true
PYTHONPATH="$CODE" /root/venv/bin/python - <<PY
import glob, json, os
OUT="$OUT"
comps=[json.load(open(f)) for f in glob.glob(os.path.join(OUT,"companies","*.json"))]
ok=[c for c in comps if "error" not in c]
tev=sum(c.get("n_events") or 0 for c in ok); tpg=sum(c.get("pages") or 0 for c in ok)
withev=[c for c in ok if (c.get("n_events") or 0)>0]
top=sorted(ok,key=lambda c:-(c.get("n_events") or 0))[:20]
s=("=== THE KILLER DEAL — multi-process aggregate ===\n"
   f"companies         : {len(comps)}\n"
   f"companies WITH ev : {len(withev)}  ({100*len(withev)//max(len(ok),1)}%)\n"
   f"TOTAL events      : {tev}\n"
   f"TOTAL pages       : {tpg}\n"
   f"avg events/company: {tev/max(len(ok),1):.1f}\n"
   "top 20 by events:\n"+"".join(f"   {c['n_events']:4}ev {c.get('pages','-'):>3}pg  {c['slug']}\n" for c in top))
open(os.path.join(OUT,"summary.txt"),"w").write(s)
print(s)
PY
echo "[parallel] DONE → $OUT/ (companies/<slug>.json · all_events.jsonl · summary.txt)"
