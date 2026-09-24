#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: nvg-musetalk <source-video> <audio> <output-video> <bbox-shift>" >&2
  exit 2
fi

source_video="$1"
audio="$2"
output="$3"
bbox_shift="$4"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "${HOME:-$work/home}"

python - "$work/inference.json" "$source_video" "$audio" "$bbox_shift" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump({"task_0": {
        "video_path": sys.argv[2],
        "audio_path": sys.argv[3],
        "bbox_shift": int(sys.argv[4]),
    }}, stream)
PY

python -m scripts.inference \
  --inference_config "$work/inference.json" \
  --result_dir "$work/results" \
  --unet_model_path models/musetalkV15/unet.pth \
  --unet_config models/musetalkV15/musetalk.json \
  --version v15

result="$(find "$work/results" -type f -name '*.mp4' -print -quit)"
if [ -z "$result" ]; then
  echo "MuseTalk produced no MP4 output" >&2
  exit 1
fi
mkdir -p "$(dirname "$output")"
cp "$result" "$output"
