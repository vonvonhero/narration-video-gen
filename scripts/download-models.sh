#!/usr/bin/env bash
# Download the model weights a profile needs into models/.
#
# Downloads use curl; model hashes are checked after transfer.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile=""
profile_dirs=()
model_ids=()
check_only=false
usage() {
  echo "Usage: scripts/download-models.sh [--profile <profile-id>] [--profile-dir <directory>]... [--check]"
  echo "       scripts/download-models.sh --model <locked-model-id>... [--check]"
}
require_value() {
  if [ "$#" -lt 2 ] || [ -z "$2" ] || [[ "$2" == --* ]]; then
    echo "error: $1 requires a value" >&2
    exit 2
  fi
}
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) require_value "$@"; profile="$2"; shift 2 ;;
    --profile-dir) require_value "$@"; profile_dirs+=("$2"); shift 2 ;;
    --model) require_value "$@"; model_ids+=("$2"); shift 2 ;;
    --check) check_only=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; echo "error: unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "${#model_ids[@]}" -gt 0 ] && { [ -n "$profile" ] || [ "${#profile_dirs[@]}" -gt 0 ]; }; then
  echo "error: --model cannot be combined with profile selection" >&2
  exit 2
fi

# `plan --json` already resolves the recipe's model ids against the lock file
# and reports which files are missing, so the download list comes from there
# rather than from a second copy of the same knowledge.
plan_args=()
global_plan_args=()
if [ -n "$profile" ]; then
  plan_args=(--profile "$profile")
fi
for directory in "${profile_dirs[@]}"; do
  global_plan_args+=(--profile-dir "$directory")
done
plan_status=0
if [ "${#model_ids[@]}" -gt 0 ]; then
  # Human-facing model acquisition plan: no hardware profile or GPU execution.
  # New weights can be acquired before a compatible generation recipe exists.
  plan_json="$(python3 - "$root" "${model_ids[@]}" <<'PY'
import json, pathlib, shutil, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "src"))
from narration_video_gen.compat import load_yaml_file
locked = {m["id"]: m for m in load_yaml_file(root / "manifests" / "models.lock.yaml")["models"]}
models = []
for model_id in dict.fromkeys(sys.argv[2:]):
    if model_id not in locked:
        raise SystemExit("error: unknown locked model id: " + model_id)
    entry = locked[model_id]
    path = (root / "models" / entry["path"]).resolve()
    try:
        path.relative_to((root / "models").resolve())
    except ValueError:
        raise SystemExit("error: model path escapes models directory")
    models.append({key: entry[key] for key in ("id", "path", "bytes", "sha256", "license")})
    models[-1]["license_url"] = entry.get("license_url")
    models[-1]["present"] = path.is_file() and path.stat().st_size == entry["bytes"]
total = sum(m["bytes"] for m in models)
missing = sum(m["bytes"] for m in models if not m["present"])
free = shutil.disk_usage(root).free
print(json.dumps({"operation": "model-acquisition-only", "downloads": {
    "models": models, "total_bytes": total, "missing_bytes": missing,
    "total_gib": total / 2**30, "missing_gib": missing / 2**30},
    "runtime_estimate": {"known": False},
    "disk": {"free_gib": free / 2**30, "sufficient": free >= missing},
    "notes": ["Check physical Windows free space separately for WSL VHD storage.",
              "Review listed licenses and approve acquisition before downloading.",
              "No generation recipe or hardware qualification is implied."]}, indent=2))
if free < missing:
    raise SystemExit("error: insufficient filesystem space")
PY
)"
else
plan_json="$("$root/bin/narration-video-gen" --root "$root" "${global_plan_args[@]}" \
  --json plan "${plan_args[@]}")" || plan_status=$?
fi
if [ "$plan_status" -ne 0 ]; then
  if [ -n "$plan_json" ]; then
    python3 -c '
import json, sys
try:
    plan = json.load(sys.stdin)
    message = plan.get("error")
except (ValueError, AttributeError):
    message = None
print(message or "error: choose an available video configuration with narration-video-gen plan", file=sys.stderr)
' <<<"$plan_json"
  fi
  exit "$plan_status"
fi
if $check_only; then
  printf '%s\n' "$plan_json"
  exit 0
fi
python3 -c '
import json, os, pathlib, shutil, subprocess, sys, time

plan = json.load(sys.stdin)
if plan.get("error"):
    raise SystemExit("error: %s" % plan["error"])
if "downloads" not in plan:
    raise SystemExit("error: choose an available video configuration with narration-video-gen plan")
root = pathlib.Path(sys.argv[1])
all_models = plan["downloads"]["models"]
models = [m for m in all_models if not m["present"]]

def verify_models():
    sys.path.insert(0, str(root / "src"))
    from narration_video_gen.verify import sha256_file
    print("Verifying model files...", flush=True)
    for model in all_models:
        path = root / "models" / model["path"]
        try:
            valid = path.stat().st_size == model["bytes"] and sha256_file(path) == model["sha256"]
        except OSError as exc:
            raise SystemExit("Cannot verify %s: %s" % (path, exc.strerror))
        if not valid:
            raise SystemExit("Model verification failed: %s. Remove this file and run plan again." % path)
        print("  [verified] %s" % model["path"], flush=True)

def gib(value):
    return "%.2f GiB" % (value / 1024**3)

def rate(value):
    if value < 1024**2:
        return "%.0f KiB/s" % (value / 1024)
    return "%.1f MiB/s" % (value / 1024**2)

def compact_gib(value):
    return "%.2f" % (value / 1024**3)

def compact_rate(value):
    return rate(value).replace(" ", "")

def duration(value):
    value = max(0, int(round(value)))
    if value >= 3600:
        return "%dh%02dm" % divmod(value // 60, 60)
    return "%dm%02ds" % divmod(value, 60)

def shorten(value, width):
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[:width - 3] + "..."

def progress_text(index, total, model, current_bytes, downloaded_bytes,
                  missing_bytes, transfer_rate, eta, columns):
    file_percent = round(current_bytes * 100 / model["bytes"])
    all_percent = round(downloaded_bytes * 100 / missing_bytes)
    count = "[downloading] %d/%d" % (index, total)
    details = ("%s/%sGiB %d%% all:%d%% %s ETA:%s"
               % (compact_gib(current_bytes), compact_gib(model["bytes"]),
                  file_percent, all_percent,
                  compact_rate(transfer_rate) if transfer_rate else "calculating",
                  eta))
    # A wrapped carriage-return line grows vertically in Windows consoles.
    # Keep one column free to avoid an automatic wrap at the right edge.
    available = columns - len(count) - len(details) - 4
    if available >= 8:
        name = pathlib.PurePosixPath(model["path"].replace("\\", "/")).name
        return "%s  %s  %s" % (count, shorten(name, available), details)
    return "%s %d%% all:%d%% ETA:%s" % (
        count, file_percent, all_percent, eta)

if not models:
    verify_models()
    raise SystemExit(0)

if not shutil.which("curl"):
    raise SystemExit("error: curl is required to download models")

missing_bytes = sum(m["bytes"] for m in models)
present_count = len(all_models) - len(models)
print("Model download: %d required, %d already present, %d to download (%s)"
      % (len(all_models), present_count, len(models), gib(missing_bytes)))
for model in all_models:
    state = "done" if model["present"] else "queued"
    print("  [%-6s] %-68s %s" % (state, model["path"], gib(model["bytes"])))

lock_path = root / "manifests" / "models.lock.yaml"
sys.path.insert(0, str(root / "src"))
from narration_video_gen.compat import load_yaml_file
urls = {e["id"]: e["url"] for e in load_yaml_file(lock_path)["models"]}

tty = sys.stdout.isatty()
inline = tty or os.environ.get("NVG_DOWNLOAD_PROGRESS") == "inline"
columns = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)
rendered_width = 0
completed_bytes = 0
for index, model in enumerate(models, 1):
    dest = root / "models" / model["path"]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit("cannot create model directory %s: %s" % (dest.parent, exc))
    if not os.access(dest.parent, os.W_OK):
        raise SystemExit(
            "cannot write model directory %s. It may have been created by Docker as root; "
            "restore ownership, then re-run plan: sudo chown %s:%s %s"
            % (dest.parent, os.getuid(), os.getgid(), dest.parent))
    if dest.exists() and not os.access(dest, os.W_OK):
        raise SystemExit(
            "cannot resume model file %s; restore ownership, then re-run plan: "
            "sudo chown %s:%s %s"
            % (dest, os.getuid(), os.getgid(), dest))
    starting_bytes = min(dest.stat().st_size, model["bytes"]) if dest.exists() else 0
    started = time.monotonic()
    last_log = 0.0
    process = subprocess.Popen([
        "curl", "-fsSL", "--retry", "5", "--retry-delay", "5", "-C", "-",
        "-o", str(dest), urls[model["id"]],
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while process.poll() is None:
        current_bytes = min(dest.stat().st_size, model["bytes"]) if dest.exists() else 0
        elapsed = max(time.monotonic() - started, 0.001)
        downloaded = completed_bytes + current_bytes
        transfer_rate = max(current_bytes - starting_bytes, 0) / elapsed
        remaining = max(missing_bytes - downloaded, 0)
        eta = duration(remaining / transfer_rate) if transfer_rate else "calculating"
        line = progress_text(
            index, len(models), model, current_bytes, downloaded,
            missing_bytes, transfer_rate, eta, columns)
        now = time.monotonic()
        if inline:
            width = max(rendered_width, len(line))
            print("\r" + line.ljust(width), end="", flush=True)
            rendered_width = width
        elif now - last_log >= 10:
            print(line, flush=True)
            last_log = now
        time.sleep(0.5)
    _stdout, stderr = process.communicate()
    if inline:
        print("\r" + (" " * rendered_width) + "\r", end="", flush=True)
        rendered_width = 0
    if process.returncode:
        message = stderr.decode("utf-8", "replace").strip()
        raise SystemExit("download failed for %s: %s" % (model["path"], message))
    completed_bytes += model["bytes"]
    all_percent = round(completed_bytes * 100 / missing_bytes)
    print("  [done]   %d/%d  %s  %s | all %d%% | %d remaining"
          % (index, len(models), model["path"], gib(model["bytes"]),
             all_percent, len(models) - index))

print("Model download complete: %s" % gib(missing_bytes))
verify_models()
' "$root" <<<"$plan_json"
