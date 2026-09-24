#!/usr/bin/env bash
# Record GPU and host memory during a run.
#
# Writes results/<run-id>/metrics.csv. Run it in a second terminal alongside
# the generation; stop it with Ctrl-C when the run finishes.
set -euo pipefail

usage() { echo "Usage: scripts/monitor.sh [run-id] (INTERVAL defaults to 10 seconds)"; }
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi
[ "$#" -le 1 ] || { usage >&2; exit 2; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run_id="${1:-$(date +%Y%m%d-%H%M%S)}"
interval="${INTERVAL:-10}"
if [[ ! "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "error: use a run ID containing letters, numbers, dots, underscores, or hyphens" >&2
  exit 2
fi
if [[ ! "$interval" =~ ^[0-9]+(\.[0-9]+)?$ ]] \
  || ! awk -v interval="$interval" 'BEGIN { exit !(interval > 0) }'; then
  echo "error: INTERVAL must be a positive number of seconds" >&2
  exit 2
fi
command -v nvidia-smi >/dev/null || { echo "error: nvidia-smi is required; check your NVIDIA driver" >&2; exit 1; }
out="$root/results/$run_id"
mkdir -p "$out"
csv="$out/metrics.csv"

if [ ! -s "$csv" ]; then
  echo "timestamp,vram_used_mib,vram_total_mib,gpu_util_pct,ram_used_gib,ram_available_gib,swap_used_gib" >> "$csv"
fi
echo "recording to $csv every ${interval}s -- Ctrl-C to stop"

while true; do
  # -i 0 matters: without it a second GPU's rows land in the same log and the
  # numbers stop meaning what they look like they mean.
  gpu="$(nvidia-smi -i 0 --query-gpu=memory.used,memory.total,utilization.gpu \
         --format=csv,noheader,nounits | tr -d ' ')"
  mem="$(awk '
    /^MemTotal:/     {total=$2}
    /^MemAvailable:/ {avail=$2}
    /^SwapTotal:/    {swtot=$2}
    /^SwapFree:/     {swfree=$2}
    END {printf "%.2f,%.2f,%.2f", (total-avail)/1048576, avail/1048576, (swtot-swfree)/1048576}
  ' /proc/meminfo)"
  echo "$(date -Is),${gpu},${mem}" >> "$csv"
  sleep "$interval"
done
