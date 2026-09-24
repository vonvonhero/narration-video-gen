#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ $# -gt 0 ]] || { echo "invalid audio path" >&2; exit 2; }
targets=()
for TARGET in "$@"; do
  [[ -n "$TARGET" && "$TARGET" != /* && "$TARGET" != *..* ]] || { echo "invalid audio path" >&2; exit 2; }
  [[ -f "$ROOT/$TARGET" ]] || { echo "audio not found" >&2; exit 2; }
  targets+=("/workspace/$TARGET")
done
docker exec -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e XDG_CACHE_HOME=/tmp/.cache -e TRITON_CACHE_DIR=/tmp/.triton \
  -e TORCHINDUCTOR_CACHE_DIR=/tmp/.inductor -e USER=tts -e LOGNAME=tts \
  narration-video-gen-tts \
  /app/.venv/bin/python /workspace/scripts/tts-audio-check.py "${targets[@]}"
