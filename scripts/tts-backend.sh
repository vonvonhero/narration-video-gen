#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CONTAINER=narration-video-gen-tts
SERVER_COMMIT=841fb7c6ec57729c56b9b75c0ef2562249b13a10
MODEL_ID=Aratako/Irodori-TTS-v4.1-Small
MODEL_REVISION=2b28324dc263ed5e6638b3cf3dd94c82ead07b4b
CODEC_ID=Aratako/Semantic-DACVAE-Japanese-32dim
CODEC_REVISION=47376ee24834d7a05a48ebabfe3cde29b3c5e214
MODEL_SHA256=c85de88c01700cb53538e706f128ebcb1b8513ad21d7d0e75f58bc82cdbf89f6
CODEC_SHA256=db120339c5ee7eca1912cdf29bc612b947a0808e69c3cebfb4936b45a762c1d5
WHISPER_SHA256=ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e
SILENTCIPHER_ID=sony/silentcipher
SILENTCIPHER_REVISION=a1c4d021905e0dc5b24be5f68db5fc4dba410ee1
SILENTCIPHER_TREE_SHA256=a1f5215fcb9d5fbc7c17841b124841cb5a0a7daf202632b69f8f6368f115eb08
MODEL_ROOT="$ROOT/models/irodori-tts-v4.1-small"
SILENTCIPHER_SNAPSHOT="$MODEL_ROOT/huggingface/hub/models--sony--silentcipher/snapshots/$SILENTCIPHER_REVISION"
BACKEND_RECORD="$ROOT/outputs/tts/.backend"
PROFILE_RECORD="$ROOT/outputs/tts/.backend-profile"
USER_VOICES="$ROOT/voices"
RUNTIME_VOICES="$ROOT/outputs/tts/.voices"

usage() {
  echo "usage: scripts/tts-backend.sh build|prepare|start|stop|status [--device auto|cuda|rocm|cpu] [--profile <hardware.json>]" >&2
  echo "       scripts/tts-backend.sh convert --input <file> --output <file.wav>" >&2
  exit 2
}

command_name=${1:-}
[[ -n "$command_name" ]] || usage
shift
device=auto
convert_input=
convert_output=
tts_profile=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --device) device=${2:-}; shift 2 ;;
    --input) convert_input=${2:-}; shift 2 ;;
    --output) convert_output=${2:-}; shift 2 ;;
    --profile) tts_profile=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ "$device" == auto || "$device" == cuda || "$device" == rocm || "$device" == cpu ]] || usage

# An explicit hardware profile remains selected for later WebUI auto-starts.
# Explicit device selection still takes precedence over the saved profile.
if [[ "$command_name" == start && -z "$tts_profile" && "$device" == auto && -s "$PROFILE_RECORD" ]]; then
  IFS= read -r tts_profile < "$PROFILE_RECORD"
fi
profile_model_precision=
profile_codec_precision=
if [[ -n "$tts_profile" ]]; then
  # Hardware belongs to a named profile, never one-off precision flags.
  profile_values=$(python3 - "$tts_profile" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
assert p['model_device'] == p['codec_device'] and p['model_device'] in ('cuda', 'cpu')
assert p['model_precision'] in ('fp32', 'bf16') and p['codec_precision'] in ('fp32', 'bf16')
print(p['model_device'], p['model_precision'], p['codec_precision'])
PY
  )
  read -r device profile_model_precision profile_codec_precision <<< "$profile_values"
fi

require_docker() {
  command -v docker >/dev/null 2>&1 || { echo "Docker is required for local TTS." >&2; exit 1; }
  docker info >/dev/null 2>&1 || { echo "Docker is installed but not available." >&2; exit 1; }
}

choose_backend() {
  if [[ "$device" == cuda ]]; then
    echo cu128
  elif [[ "$device" == rocm ]]; then
    echo rocm
  elif [[ "$device" == cpu ]]; then
    echo cpu
  elif command -v nvidia-smi >/dev/null 2>&1 \
      && nvidia-smi -L >/dev/null 2>&1 \
      && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia; then
    echo cu128
  elif [[ -c /dev/kfd && -d /dev/dri ]]; then
    echo rocm
  else
    echo cpu
  fi
}

image_for() {
  echo "narration-video-gen-tts:${SERVER_COMMIT}-${1}"
}

build_backend() {
  local backend=$1 image
  image=$(image_for "$backend")
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker build \
      --file "$ROOT/docker/tts.Dockerfile" \
      --build-arg "IRODORI_TTS_BACKEND=$backend" \
      --build-arg "IRODORI_SERVER_COMMIT=$SERVER_COMMIT" \
      --tag "$image" "$ROOT"
  fi
  mkdir -p "$(dirname "$BACKEND_RECORD")"
  printf '%s\n' "$backend" > "$BACKEND_RECORD"
}

models_ready() {
  [[ -s "$MODEL_ROOT/checkpoint/model.safetensors" \
     && -s "$MODEL_ROOT/checkpoint/tokenizer/tokenizer_config.json" \
     && -s "$MODEL_ROOT/codec/weights.pth" \
     && -s "$MODEL_ROOT/whisper/base.pt" \
     && -s "$SILENTCIPHER_SNAPSHOT/config.json" ]] \
    && [[ "$(cat "$MODEL_ROOT/huggingface/hub/models--sony--silentcipher/refs/main" 2>/dev/null)" == "$SILENTCIPHER_REVISION" ]]
}

verify_models() {
  printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_ROOT/checkpoint/model.safetensors" \
    | sha256sum --check --status -
  printf '%s  %s\n' "$CODEC_SHA256" "$MODEL_ROOT/codec/weights.pth" \
    | sha256sum --check --status -
  printf '%s  %s\n' "$WHISPER_SHA256" "$MODEL_ROOT/whisper/base.pt" \
    | sha256sum --check --status -
  local silentcipher_tree_sha256
  # The digest covers the sorted file list, so the sort must not depend on the
  # caller's locale: en_US.UTF-8 collates ".gitattributes" and "16_khz"
  # differently from C and produced a mismatch on intact, byte-identical files.
  silentcipher_tree_sha256=$(
    cd "$SILENTCIPHER_SNAPSHOT"
    find -L . -type f -print0 | LC_ALL=C sort -z \
      | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
  )
  [[ "$silentcipher_tree_sha256" == "$SILENTCIPHER_TREE_SHA256" ]]
}

prepare_models() {
  local backend=$1 image
  image=$(image_for "$backend")
  mkdir -p "$MODEL_ROOT/checkpoint" "$MODEL_ROOT/codec" "$MODEL_ROOT/huggingface"
  if ! models_ready; then
    # Phase markers let the WebUI tell a download from an image build.
    echo "nvg-phase: download"
    docker run --rm \
      -e "MODEL_ID=$MODEL_ID" -e "MODEL_REVISION=$MODEL_REVISION" \
      -e "CODEC_ID=$CODEC_ID" -e "CODEC_REVISION=$CODEC_REVISION" \
      -e "SILENTCIPHER_ID=$SILENTCIPHER_ID" \
      -e "SILENTCIPHER_REVISION=$SILENTCIPHER_REVISION" \
      -e HF_HOME=/models/huggingface \
      -v "$MODEL_ROOT:/models" "$image" \
      /app/.venv/bin/python -c \
      'import os; from pathlib import Path; from huggingface_hub import hf_hub_download, snapshot_download; snapshot_download(repo_id=os.environ["MODEL_ID"], revision=os.environ["MODEL_REVISION"], allow_patterns=["model.safetensors", "tokenizer/*"], local_dir="/models/checkpoint"); hf_hub_download(repo_id=os.environ["CODEC_ID"], revision=os.environ["CODEC_REVISION"], filename="weights.pth", local_dir="/models/codec"); snapshot_download(repo_id=os.environ["SILENTCIPHER_ID"], revision=os.environ["SILENTCIPHER_REVISION"]); ref=Path("/models/huggingface/hub/models--sony--silentcipher/refs/main"); ref.parent.mkdir(parents=True, exist_ok=True); ref.write_text(os.environ["SILENTCIPHER_REVISION"]); import whisper; whisper.load_model("base", device="cpu", download_root="/models/whisper")'
  fi
  models_ready || { echo "Pinned TTS model download is incomplete." >&2; exit 1; }
  echo "nvg-phase: verify"
  verify_models || { echo "A downloaded TTS/ASR model failed SHA-256 verification." >&2; exit 1; }
}

# The container resolves voices under IRODORI_VOICES_DIR. Generate the merged
# alias file into a directory that is mounted as a directory: replacing a file
# that is bind-mounted on its own does not reach a running container.
write_runtime_voices() {
  mkdir -p "$USER_VOICES" "$RUNTIME_VOICES"
  PYTHONPATH="$ROOT/src" python3 -c \
    'import sys; from narration_video_gen import tts_service; tts_service.write_runtime_voices(sys.argv[1])' \
    "$ROOT"
}

# Convert any recording into the reference contract using the ffmpeg already
# pinned inside the TTS image, so the host never needs audio tooling.
convert_audio() {
  local backend=$1 image in_dir out_dir
  image=$(image_for "$backend")
  [[ -n "$convert_input" && -n "$convert_output" ]] || usage
  [[ -f "$convert_input" ]] || { echo "input file not found: $convert_input" >&2; exit 1; }
  docker image inspect "$image" >/dev/null 2>&1 \
    || { echo "The TTS image is not built yet. Run the prepare command first." >&2; exit 1; }
  in_dir=$(cd "$(dirname "$convert_input")" && pwd)
  mkdir -p "$(dirname "$convert_output")"
  out_dir=$(cd "$(dirname "$convert_output")" && pwd)
  docker run --rm --user "$(id -u):$(id -g)" \
    --network none \
    -v "$in_dir:/in:ro" -v "$out_dir:/out" \
    --entrypoint ffmpeg "$image" \
    -v error -y -i "/in/$(basename "$convert_input")" \
    -vn -ac 1 -ar 48000 -c:a pcm_s16le -t 120 \
    "/out/$(basename "$convert_output")"
}

start_backend() {
  local backend=$1 image gpu_args=() precision=fp32 model_device=cpu
  image=$(image_for "$backend")
  models_ready && verify_models || { echo "Pinned TTS models are not prepared or failed verification. Run the prepare command first." >&2; exit 1; }
  if [[ "$backend" == cu128 ]]; then
    gpu_args=(--gpus all)
    precision=bf16
    model_device=cuda
  elif [[ "$backend" == rocm ]]; then
    gpu_args=(--device /dev/kfd --device /dev/dri --group-add video --ipc host --security-opt seccomp=unconfined)
    precision=bf16
    model_device=cuda
  fi
  write_runtime_voices
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" \
    "${gpu_args[@]}" \
    -p 127.0.0.1:8088:8088 \
    -e IRODORI_CHECKPOINT=/models/checkpoint/model.safetensors \
    -e "IRODORI_HF_CHECKPOINT=$MODEL_ID" \
    -e IRODORI_CODEC_REPO=/models/codec/weights.pth \
    -e IRODORI_VOICES_DIR=/voices \
    -e IRODORI_VOICE_ALIASES_FILE=/voices/aliases.json \
    -e "IRODORI_MODEL_DEVICE=$model_device" \
    -e "IRODORI_CODEC_DEVICE=$model_device" \
    -e "IRODORI_MODEL_PRECISION=${profile_model_precision:-$precision}" \
    -e "IRODORI_CODEC_PRECISION=${profile_codec_precision:-$precision}" \
    -e IRODORI_DEFAULT_CHUNKING_ENABLED=false \
    -e HF_HOME=/models/huggingface \
    -e HF_HUB_OFFLINE=1 \
    -v "$MODEL_ROOT:/models:ro" \
    -v "$RUNTIME_VOICES:/voices:ro" \
    -v "$ROOT/assets/characters:/voices/characters:ro" \
    -v "$USER_VOICES:/voices/user:ro" \
    -v "$ROOT:/workspace:ro" \
    -v "$ROOT/outputs/tts:/work" \
    "$image"
  if [[ -n "$tts_profile" ]]; then
    python3 - "$tts_profile" "$PROFILE_RECORD" <<'PYPROFILE'
from pathlib import Path
import sys
source, record = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
temporary = record.with_suffix('.tmp')
temporary.write_text(str(source) + '\n')
temporary.replace(record)
PYPROFILE
  fi
  echo "TTS backend: http://127.0.0.1:8088"
}

require_docker
backend=$(choose_backend)
case "$command_name" in
  build) build_backend "$backend" ;;
  prepare) echo "nvg-phase: build"; build_backend "$backend"; prepare_models "$backend" ;;
  start) build_backend "$backend"; start_backend "$backend" ;;
  convert) convert_audio "$backend" ;;
  stop) docker rm -f "$CONTAINER" >/dev/null 2>&1 || true ;;
  status)
    docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo stopped
    ;;
  *) usage ;;
esac
