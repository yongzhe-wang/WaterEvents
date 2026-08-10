#!/usr/bin/env bash
# sync_fleet — 把 backend/ 的**全部** Python 源码同步到机队, 或者只报告漂移。
#
# 用一句话讲完: 拿本地 backend/ 里每个 .py 算 md5 → 逐台机器算同一份 md5 → 不一致的列出来;
# 带 --apply 就把整个 backend/ 打包 scp 过去解开, 不带就只报告。机队上没有 git 仓库, 文件一直是
# 一个一个 scp 上去的, 所以"漏拷一个文件"是静默的 —— 这个脚本让它不再静默。
#
# WHY this exists. The fleet hosts have no git checkout at all; every deploy has been a per-file scp. A file nobody
# remembered to copy simply stays at whatever version it was, forever, and nothing anywhere reports it. That is not a
# hypothetical: tools/officeall/extract.py sat 67 lines behind the repo on BOTH hosts and 21 lines apart from EACH
# OTHER, so the legacy-.xls fallback written to fix a measured failure had never once executed in production.
# {2026-08-10 wc -l tools/officeall/extract.py — ir-media-8 222 / ir-render-16 203 / repo 289;
#  `grep -c _xls_tables` — 0 / 0 / 2}
# {2026-08-10 the same day, tools/officeall/fetch.py was scp'd to /tmp on both hosts but `cp`'d into the tree on only
#  one, so the production /fetch_doc endpoint kept returning "wrong-magic-b'\xd0\xcf\x11\xe0…'" against code that had
#  already been fixed and tested — a second instance of the same failure mode within one hour}
# [CONFIDENCE: CONFIRMED 100% — line counts and grep counts read from all three trees; the endpoint's stale answer and
#  its correct answer after the second copy were both captured.]
#
# 上游触发: 手动, 在任何 backend/ 改动落地之后。下游连接: 各服务重启才真正生效 —— 本脚本只同步文件, 不重启。
set -euo pipefail

PROJECT="${GCP_PROJECT:-focusalpha-ir-pipeline}"
ZONE="${GCP_ZONE:-us-central1-a}"
HOSTS="${FLEET_HOSTS:-ir-media-8 ir-render-16}"
REMOTE_ROOT="${FLEET_ROOT:-\$HOME/WaterEvents/backend}"

# THE THIRD TARGET. Deploying to the two GCE boxes and calling it done is wrong, and the way it is wrong is invisible:
# `tools/officeall/__init__.py` rebinds docling_extract to an HTTP client when DOCLING_REMOTE_URL is set, and it IS set
# on ir-render-16 — so the whole extraction, fallbacks included, executes on the RunPod pod against the pod's own copy
# of the code. A fix can be deployed to both GCE hosts, verified by running it in a shell there, and still not be what
# production runs.
# {2026-08-10 — extract.py deployed to ir-media-8 + ir-render-16 and correct in a local shell on both, while
#  POST /fetch_doc kept returning the old '## 1(J)' headings and an untrimmed 56x21 grid; the pod's copy was 289 lines
#  with `_sheet_title` absent. After copying the same file to the pod: '## 1.連結サマリー（NTT連結業績）', 52x16.}
# {POD /proc/<pid>/environ — "PYTHONPATH=/opt/we/WaterEvents/backend", and a SECOND stale tree sits at
#  /workspace/WaterEvents/backend/tools/officeall/extract.py (157 lines) which nothing on PYTHONPATH reaches}
# [CONFIDENCE: CONFIRMED 100% — same request before and after the pod copy, same bytes, different answer.]
# Reached over the same ssh hop the tunnels use, read off the running tunnel rather than hard-coded, so a pod
# re-provision (new host/port) is picked up automatically.
POD_VIA="${POD_VIA:-ir-media-8}"                # the GCE box that holds the runpod key and the live tunnel
POD_ROOT="${POD_ROOT:-/opt/we/WaterEvents/backend}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

# 脚本可能从任何地方被调用, 所以路径从脚本自身位置推出来, 不依赖 cwd。
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$LOCAL_ROOT"

SSH=(gcloud compute ssh --project="$PROJECT" --zone="$ZONE" --tunnel-through-iap --quiet)
SCP=(gcloud compute scp --project="$PROJECT" --zone="$ZONE" --tunnel-through-iap --quiet)

# 排除运行时产物: 缓存 / 日志 / 抓取 trace / 虚拟环境。这些在机器上本就该和本地不同, 拿它们比对只会制造噪音。
FIND_PY=(find . -name '*.py' -not -path './__pycache__/*' -not -path '*/__pycache__/*'
         -not -path './venv/*' -not -path './.venv/*' -not -path '*/traces/*')

echo "══ 本地 $(cd "$LOCAL_ROOT" && "${FIND_PY[@]}" | wc -l | tr -d ' ') 个 .py, 逐台比对 ══"
"${FIND_PY[@]}" -print0 | xargs -0 md5 -r 2>/dev/null | sort -k2 > /tmp/_fleet_local.md5 \
  || "${FIND_PY[@]}" -print0 | xargs -0 md5sum | sort -k2 > /tmp/_fleet_local.md5

DRIFT_TOTAL=0
for H in $HOSTS; do
  # 同一条 find 在远端跑, 用同样的排除规则, 否则两边文件集合不同, 差异会被 __pycache__ 淹没。
  "${SSH[@]}" "$H" --command="cd $REMOTE_ROOT && find . -name '*.py' -not -path '*/__pycache__/*' -not -path './venv/*' -not -path '*/traces/*' -print0 | xargs -0 md5sum | sort -k2" \
    > "/tmp/_fleet_$H.md5" 2>/dev/null || { echo "  $H: 读不到, 跳过"; continue; }

  # join 只能发现两边都有的文件的内容差异, comm 负责发现"一边有一边没有"的整文件缺失。
  DIFF=$(join -j 2 -o 0,1.1,2.1 <(awk '{print $1, $2}' /tmp/_fleet_local.md5 | sort -k2) \
                                 <(awk '{print $1, $2}' "/tmp/_fleet_$H.md5" | sort -k2) \
         | awk '$2 != $3 {print "    内容不同  " $1}')
  MISSING=$(comm -23 <(awk '{print $2}' /tmp/_fleet_local.md5 | sort) <(awk '{print $2}' "/tmp/_fleet_$H.md5" | sort) \
            | sed 's/^/    远端缺失  /')
  N=$(printf '%s\n%s\n' "$DIFF" "$MISSING" | grep -c . || true)
  DRIFT_TOTAL=$((DRIFT_TOTAL + N))
  if [ "$N" -eq 0 ]; then
    echo "  ✅ $H 与本地一致"
  else
    echo "  ⚠️  $H 有 $N 个文件漂移:"
    [ -n "$DIFF" ] && echo "$DIFF"
    [ -n "$MISSING" ] && echo "$MISSING"
  fi
done

# ── POD — 同一套比对, 但要多跳一层 ssh, 所以命令拼在 GCE 侧再转发进去。
# 只比 tools/ 下的文件: pod 上跑的是 tools.service, agent/ 和 api_service/ 那些它根本不 import, 拿来比只会
# 报出一堆与生产无关的漂移, 把真正要紧的那几行淹掉。
POD_CMD='K=$HOME/.ssh/runpod_key
A=$(ps -eo args | grep ExitOnForwardFailure | grep -v grep | head -1)
PT=$(echo "$A" | grep -oE "\-p [0-9]+" | head -1 | cut -d" " -f2)
HP=$(echo "$A" | grep -oE "root@[0-9.]+" | head -1)
[ -z "$HP" ] && { echo "NO_TUNNEL"; exit 0; }
ssh -i $K -p $PT -o StrictHostKeyChecking=no -o ConnectTimeout=20 $HP \
  "cd '"$POD_ROOT"' && find tools -name \"*.py\" -not -path \"*/__pycache__/*\" -print0 | xargs -0 md5sum | sort -k2"'
POD_MD5=$("${SSH[@]}" "$POD_VIA" --command="$POD_CMD" 2>/dev/null || true)
if [ -z "$POD_MD5" ] || [ "$POD_MD5" = "NO_TUNNEL" ]; then
  echo "  ⚠️  runpod pod: 读不到(隧道不在?), 未比对 —— 它是第三个部署目标, 别当它不存在"
else
  printf '%s\n' "$POD_MD5" > /tmp/_fleet_pod.md5
  ( cd "$LOCAL_ROOT" && find tools -name '*.py' -not -path '*/__pycache__/*' -print0 | xargs -0 md5 -r 2>/dev/null \
      || find tools -name '*.py' -not -path '*/__pycache__/*' -print0 | xargs -0 md5sum ) | sort -k2 > /tmp/_fleet_local_tools.md5
  PD=$(join -j 2 -o 0,1.1,2.1 <(awk '{print $1, $2}' /tmp/_fleet_local_tools.md5 | sort -k2) \
                               <(awk '{print $1, $2}' /tmp/_fleet_pod.md5 | sort -k2) \
       | awk '$2 != $3 {print "    内容不同  " $1}')
  PN=$(printf '%s' "$PD" | grep -c . || true)
  DRIFT_TOTAL=$((DRIFT_TOTAL + PN))
  if [ "$PN" -eq 0 ]; then echo "  ✅ runpod pod (tools/) 与本地一致"
  else echo "  ⚠️  runpod pod (tools/) 有 $PN 个文件漂移:"; echo "$PD"; fi
fi

if [ "$APPLY" -eq 0 ]; then
  echo "══ 只报告, 未改动。加 --apply 同步 ══"
  # 有漂移就非零退出, 这样 CI / watchdog 可以直接把它当断言用。
  [ "$DRIFT_TOTAL" -eq 0 ] || exit 1
  exit 0
fi

echo "══ --apply: 打包整个 backend/ 推到机队 ══"
# 整包推送而不是逐文件 —— 逐文件正是造成这次漂移的做法。COPYFILE_DISABLE 抑制 macOS 的 ._ 资源分叉文件,
# 否则解包时远端会为每个文件多出一个垃圾伴随文件。
COPYFILE_DISABLE=1 tar czf /tmp/_fleet_backend.tgz \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='venv' --exclude='.venv' --exclude='*/traces/*' \
  --exclude='*.log' .
echo "  包大小 $(wc -c < /tmp/_fleet_backend.tgz) bytes"
for H in $HOSTS; do
  "${SCP[@]}" /tmp/_fleet_backend.tgz "$H:/tmp/_fleet_backend.tgz" >/dev/null 2>&1 || { echo "  $H: scp 失败"; continue; }
  # tar 直接覆盖到目标目录; 不删除远端多出来的文件, 因为机器上可能有本地生成的运行时产物。
  "${SSH[@]}" "$H" --command="cd $REMOTE_ROOT && tar xzf /tmp/_fleet_backend.tgz && echo ok" >/dev/null 2>&1 \
    && echo "  ✅ $H 已同步" || echo "  ✗ $H 解包失败"
done
echo "══ 文件已同步。相关服务需要各自重启才生效 ══"
