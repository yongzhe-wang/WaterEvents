#!/bin/bash
# Rebuild /root/venv after a pod restart wiped it (/root is ephemeral; only /workspace persists). pip cache on /root
# (/workspace's ~20G quota is full with the 16G model). Model stays cached on /workspace/hf.
# AUDIT FIX #1 (fail-loud): the old version ended with `... | tail -3` then `echo RECOVER_EXIT=$?` — $? was the
# TAIL's exit (always 0), so a failed pip/playwright install reported SUCCESS. Now `set -euo pipefail` makes ANY
# step's failure abort loudly, and an explicit import check is the final proof before declaring success.
# {AUDIT 2026-07-23 "recover.sh RECOVER_EXIT=$? captured tail, not the install → masked failures"}
set -euo pipefail

rm -rf /root/venv
python3 -m venv /root/venv
/root/venv/bin/pip install -q --upgrade pip
# PIP_CACHE_DIR on /workspace (PERSISTENT) now that the volume is resized to 50G (16G model + 9.5G venv-build + ~6G
# wheel cache fits). WHY here not /root: /root is wiped on every restart, so caching wheels there is useless; caching
# on /workspace means the ~6G of vllm+CUDA wheels DOWNLOAD ONCE and every future restart's reinstall is INSTALL-ONLY
# (no re-download) → cold restart drops from ~15min to ~3-4min. venv itself stays on /root (fast local import; a venv
# on MooseFS network FS has slow small-file import). {2026-07-23: volume resized 20->50G enables the persistent cache}
mkdir -p /workspace/.pipcache
PIP_CACHE_DIR=/workspace/.pipcache /root/venv/bin/pip install vllm playwright
PATH=/root/venv/bin:/usr/local/cuda/bin:$PATH /root/venv/bin/playwright install chromium

# Never declare success on a half-built venv — the install must actually import.
if ! /root/venv/bin/python -c "import vllm, playwright" 2>&1; then
  echo "RECOVER FAILED: import check after install — venv is broken" >&2
  exit 1
fi
echo "RECOVER_EXIT=0"
