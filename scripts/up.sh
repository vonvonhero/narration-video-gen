#!/usr/bin/env bash
# Start the ComfyUI container with the overlay that matches this machine.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Docker creates a missing bind-mount source as root. Create the RIFE checkpoint
# directory here, while this launcher is still running as the project user, so
# the later model download can write the locked checkpoint.
mkdir -p "$root/models/rife"
# Compose interpolates the image tag and build pins even for commands such as
# `up`, `ps`, and `logs`. Reconstruct them here rather than relying on exports
# from a previous build-image.sh process, which cannot survive into this shell.
# shellcheck source=scripts/lib/container-env.sh
source "$root/scripts/lib/container-env.sh"
load_container_env "$root"
platform="$("$root/bin/narration-video-gen" --json detect | python3 -c 'import sys,json; print(json.load(sys.stdin)["platform"])')"

case "$platform" in
  linux)        overlay=docker/compose.linux.yaml ;;
  windows-wsl2) overlay=docker/compose.wsl2.yaml ;;
  *) echo "unsupported platform: $platform" >&2; exit 1 ;;
esac

echo "platform: $platform -> $overlay"
compose_files=(-f "$root/docker/compose.yaml" -f "$root/$overlay")
if [ "$platform" = "windows-wsl2" ]; then
  workaround_default=1
else
  workaround_default=0
fi
workaround="${NVG_WSL2_ALLOCATOR_WORKAROUND:-$workaround_default}"
case "$workaround" in
  0) ;;
  1)
    if [ "$platform" != "windows-wsl2" ]; then
      echo "NVG_WSL2_ALLOCATOR_WORKAROUND is only valid under Windows WSL2" >&2
      exit 2
    fi
    legacy_overlay=docker/compose.wsl2-rdma-workaround.yaml
    compose_files+=(-f "$root/$legacy_overlay")
    echo "WSL2 allocator compatibility workaround: enabled -> $legacy_overlay"
    ;;
  *)
    echo "NVG_WSL2_ALLOCATOR_WORKAROUND must be 0 or 1" >&2
    exit 2
    ;;
esac

exec docker compose "${compose_files[@]}" "$@"
