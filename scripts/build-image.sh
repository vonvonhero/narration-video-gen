#!/usr/bin/env bash
# Build the ComfyUI image from the pins in manifests/containers.lock.yaml.
#
# The lock file is the single source of truth: this script reads it and passes
# every pin as a build argument, so the Dockerfile cannot drift from it. If a
# pin is missing the build fails here rather than producing an image that
# quietly tracks upstream HEAD.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
with_musetalk=false
if [ "${1:-}" = "--with-musetalk" ]; then
  with_musetalk=true
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "usage: $0 [--with-musetalk]" >&2
  exit 2
fi
# shellcheck source=scripts/lib/container-env.sh
source "$root/scripts/lib/container-env.sh"
load_container_env "$root"

echo "building ${COMFY_IMAGE}"
echo "  base            ${BASE_IMAGE}@${BASE_DIGEST}"
echo "  Transformers    ${TRANSFORMERS_VERSION}"
echo "  ComfyUI         ${COMFYUI_COMMIT}"
echo "  WanVideoWrapper ${WANVIDEOWRAPPER_COMMIT}"
echo

docker compose -f "$root/docker/compose.yaml" -f "$root/docker/compose.linux.yaml" build comfy

if $with_musetalk; then
  echo
  echo "building ${MUSETALK_IMAGE}"
  docker compose --profile musetalk \
    -f "$root/docker/compose.yaml" -f "$root/docker/compose.linux.yaml" \
    build musetalk
fi

echo
echo "built ${COMFY_IMAGE}"
if $with_musetalk; then
  echo "built ${MUSETALK_IMAGE}"
fi
