#!/usr/bin/env bash
# Sync WaterEvents Mac → VM (nmx-h20-4). Usage: ./sync.sh
set -euo pipefail
H=h20-1038
rsync -az --delete \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' \
  --exclude '*.safetensors' --exclude 'hf_cache' --exclude '.venv' \
  -e "ssh -o ConnectTimeout=15" \
  /Users/thebigsun/dev/WaterEvents/ "$H:~/WaterEvents/"
echo "synced Mac → $H:~/WaterEvents"
