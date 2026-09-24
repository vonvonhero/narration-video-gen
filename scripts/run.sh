#!/usr/bin/env bash
# Generate a video from a profile. Thin wrapper over `narration-video-gen run`.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$root/bin/narration-video-gen" --root "$root" run "$@"
