#!/usr/bin/env bash
# Sync WaterEvents Mac(code) → VM under the yongzhe namespace on the shared CPFS. Usage: ./sync.sh
# Weights/logs live on the VM at /mnt/data/yongzhe/{hf_cache,qwen_logs}; only CODE syncs (weights excluded).
set -euo pipefail
H=h20-1039
DEST=/mnt/data/yongzhe/WaterEvents
rsync -az --delete \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' \
  --exclude '*.safetensors' --exclude 'hf_cache' --exclude '.venv' \
  -e "ssh -o ConnectTimeout=15" \
  /Users/thebigsun/dev/WaterEvents/ "$H:$DEST/"
echo "synced Mac → $H:$DEST"
