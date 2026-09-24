#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
JOB_ID=${1:-}
[[ "$JOB_ID" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid TTS job id" >&2; exit 2; }
[[ -f "$ROOT/outputs/tts/$JOB_ID/manifest.json" ]] || { echo "TTS job not found" >&2; exit 2; }
docker exec -u "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e XDG_CACHE_HOME=/tmp/.cache \
  -e TRITON_CACHE_DIR=/tmp/.triton \
  -e PYTHONPATH=/workspace/src \
  narration-video-gen-tts \
  /app/.venv/bin/python /workspace/scripts/tts-asr.py --job "/work/$JOB_ID"
