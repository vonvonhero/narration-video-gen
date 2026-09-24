#!/usr/bin/env bash
# Load the pinned Compose build arguments and image tag from the container lock.
# This file is sourced by both build-image.sh and up.sh so each command works
# independently, including when they are run from different shells.

load_container_env() {
  local root="$1"
  local pins

  pins="$(python3 - "$root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
from narration_video_gen.compat import load_yaml_file
from narration_video_gen.plan import musetalk_build_identity, runtime_build_identity

lock_path = root / "manifests" / "containers.lock.yaml"
lock = load_yaml_file(lock_path)
build_sha = runtime_build_identity(root)
commits = {component["name"]: component["commit"] for component in lock["components"]}

wanted = {
    "COMFYUI_COMMIT": "ComfyUI",
    "WANVIDEOWRAPPER_COMMIT": "ComfyUI-WanVideoWrapper",
    "KJNODES_COMMIT": "ComfyUI-KJNodes",
    "VIDEOHELPER_COMMIT": "ComfyUI-VideoHelperSuite",
    "FRAMEINTERP_COMMIT": "ComfyUI-Frame-Interpolation",
    "GGUF_COMMIT": "ComfyUI-GGUF",
    "SAM2_COMMIT": "ComfyUI-segment-anything-2",
}

print("BASE_IMAGE=%s" % lock["base_image"]["reference"])
print("BASE_DIGEST=%s" % lock["base_image"]["digest"])
print("TRANSFORMERS_VERSION=%s" % lock["python_runtime"]["transformers"])
print("RUNTIME_BUILD_SHA=%s" % build_sha)
print("MUSETALK_BASE_IMAGE=%s" % lock["musetalk"]["base_image"]["reference"])
print("MUSETALK_BASE_DIGEST=%s" % lock["musetalk"]["base_image"]["digest"])
print("MUSETALK_COMMIT=%s" % lock["musetalk"]["commit"])
print("MUSETALK_BUILD_SHA=%s" % musetalk_build_identity(root))
for key, name in wanted.items():
    if name not in commits:
        raise SystemExit("containers.lock.yaml has no commit pinned for %s" % name)
    print("%s=%s" % (key, commits[name]))
PY
)"

  while IFS= read -r line; do
    export "${line?}"
  done <<< "$pins"

  export COMFY_IMAGE="narration-video-gen-comfy:${RUNTIME_BUILD_SHA:0:12}"
  export MUSETALK_IMAGE="narration-video-gen-musetalk:${MUSETALK_BUILD_SHA:0:12}"
}
