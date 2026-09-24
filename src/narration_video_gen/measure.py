"""Measured GPU capability probe.

``detect`` reads what nvidia-smi *claims*. This module measures what the GPU
actually sustains: throughput under a short load, how hot it gets, and whether
the driver reduces clocks while it runs.

The measurement runs inside the pinned base image from
``manifests/containers.lock.yaml`` -- the same image ``plan`` builds the runtime
image from, so nothing downloaded here is wasted. Only the standard library is
used on the host side; torch lives in the container.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import detect

DEFAULT_SECONDS = 30
SCHEMA_VERSION = 1

# nvidia-smi reports the active clock limiters as a bitmask. Only the reasons
# that change what a user should do are named here.
THROTTLE_BITS = (
    (0x0000000000000004, "power_cap"),
    (0x0000000000000008, "hw_slowdown"),
    (0x0000000000000020, "thermal_sw"),
    (0x0000000000000040, "thermal_hw"),
    (0x0000000000000080, "power_brake"),
)

THERMAL_REASONS = ("thermal_sw", "thermal_hw")

# Column index of utilization.gpu in a telemetry row, and the level above which
# a sample is taken to belong to the load rather than to the idle GPU before or
# after it.
UTILISATION_COLUMN = 6
BUSY_UTILISATION_PCT = 50

# Runs inside the container. Samples nvidia-smi once a second while a fp16
# matmul keeps the GPU busy, then prints one JSON object on stdout.
PROBE_SOURCE = r'''
import json, subprocess, sys, threading, time

SECONDS = float(sys.argv[1])
TARGET = None
FIELDS = ("temperature.gpu,clocks.sm,clocks.max.sm,power.draw,power.limit,"
          "fan.speed,utilization.gpu,pcie.link.gen.current,pcie.link.width.current")
samples = []
stop = threading.Event()


def query(fields):
    command = ["nvidia-smi", "--query-gpu=" + fields, "--format=csv,noheader,nounits"]
    if TARGET:
        command += ["-i", TARGET]
    out = subprocess.run(command, capture_output=True, text=True, check=True)
    return [part.strip() for part in out.stdout.strip().splitlines()[0].split(",")]


def throttle_field():
    for name in ("clocks_event_reasons.active", "clocks_throttle_reasons.active"):
        try:
            query(name)
            return name
        except Exception:
            continue
    return None


def sample():
    fields = FIELDS + ("," + REASONS if REASONS else "")
    while not stop.is_set():
        try:
            samples.append(query(fields))
        except Exception:
            pass
        stop.wait(1.0)


import torch

# CUDA numbers devices by speed unless told otherwise, while nvidia-smi numbers
# them by PCI slot, so on a multi-GPU host the two orders disagree. Ask torch
# which card it is about to use and aim every telemetry query at that card,
# rather than at whichever one nvidia-smi happens to list first.
props = torch.cuda.get_device_properties(0)
device_uuid = getattr(props, "uuid", None)
if device_uuid:
    device_uuid = str(device_uuid)
    TARGET = device_uuid if device_uuid.startswith("GPU-") else "GPU-" + device_uuid

# Without a UUID there is no way to tell which nvidia-smi row belongs to the
# card torch chose, and the two orderings can disagree. Reporting another card's
# temperature as this one's would be worse than reporting none.
telemetry_skipped = None
if TARGET is None:
    try:
        listing = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
            capture_output=True, text=True, check=True)
        visible = len(listing.stdout.strip().splitlines())
    except Exception:
        visible = 0
    if visible != 1:
        telemetry_skipped = "the benchmarked GPU could not be identified"

REASONS = None if telemetry_skipped else throttle_field()
if not telemetry_skipped:
    watcher = threading.Thread(target=sample, daemon=True)
    watcher.start()
else:
    watcher = None

device = torch.device("cuda")
size = 8192
a = torch.randn(size, size, device=device, dtype=torch.float16)
b = torch.randn(size, size, device=device, dtype=torch.float16)
torch.cuda.synchronize()

# Warm-up so clocks and the allocator settle before the timed section.
for _ in range(3):
    a @ b
torch.cuda.synchronize()

flop_per_matmul = 2.0 * size ** 3
started = time.time()
count = 0
while time.time() - started < SECONDS:
    for _ in range(10):
        a @ b
    torch.cuda.synchronize()
    count += 10
elapsed = time.time() - started
stop.set()
if watcher is not None:
    watcher.join(timeout=3)

try:
    measured_uuid = query("uuid")[0]
except Exception:
    measured_uuid = TARGET
print("@@PROBE@@" + json.dumps({
    "torch": torch.__version__,
    "cuda": torch.version.cuda,
    "gpu_name": props.name,
    "gpu_uuid": measured_uuid,
    "vram_mib": int(props.total_memory / (1024 * 1024)),
    "matmuls": count,
    "elapsed_seconds": round(elapsed, 3),
    "tflops": round(flop_per_matmul * count / elapsed / 1e12, 3),
    "throttle_field": REASONS,
    "telemetry_skipped": telemetry_skipped,
    "samples": samples,
}))
'''


class MeasureError(RuntimeError):
    """Raised when the measurement cannot be run or produced no result."""


def normalise_gpu_uuid(value):
    """Return a CUDA UUID in the form nvidia-smi accepts for ``-i``.

    ``torch.cuda.get_device_properties(0).uuid`` is a bare UUID on some builds
    and already prefixed with ``GPU-`` on others. Adding a prefix that is
    already there produces an address nvidia-smi rejects, which would silently
    cost every telemetry sample.
    """
    if not value:
        return None
    text = str(value)
    return text if text.startswith("GPU-") else "GPU-%s" % text


def decode_throttle(raw):
    """Turn one nvidia-smi throttle bitmask into the reason names it contains."""
    text = (raw or "").strip()
    if not text or text in ("[N/A]", "N/A"):
        return []
    try:
        mask = int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return []
    return [name for bit, name in THROTTLE_BITS if mask & bit]


def _floats(samples, index):
    values = []
    for row in samples:
        if len(row) <= index:
            continue
        try:
            values.append(float(row[index]))
        except (TypeError, ValueError):
            continue
    return values


def busy_samples(samples):
    """Keep the samples taken while the GPU was actually working.

    The sampler runs from before the warm-up until after the last
    synchronisation, so the first and last rows can be of an idle card. Reading
    an idle clock as the sustained one would report a healthy GPU as throttled.
    """
    busy = []
    for row in samples:
        if len(row) <= UTILISATION_COLUMN:
            continue
        try:
            utilisation = float(row[UTILISATION_COLUMN])
        except (TypeError, ValueError):
            continue
        if utilisation >= BUSY_UTILISATION_PCT:
            busy.append(row)
    return busy or samples


def _summarise(payload):
    """Reduce the per-second samples to the few numbers a reader acts on."""
    observed = payload.get("samples") or []
    samples = busy_samples(observed)
    columns = ("temperature_c", "clocks_sm_mhz", "clocks_max_sm_mhz", "power_w",
               "power_limit_w", "fan_pct", "utilization_pct",
               "pcie_gen", "pcie_width")
    summary = {}
    for index, name in enumerate(columns):
        values = _floats(samples, index)
        if not values:
            continue
        summary[name] = {
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "last": round(values[-1], 1),
        }

    reasons = set()
    if payload.get("throttle_field"):
        for row in samples:
            if len(row) > len(columns):
                reasons.update(decode_throttle(row[len(columns)]))

    temperature = summary.get("temperature_c")
    clocks = summary.get("clocks_sm_mhz")
    ceiling = summary.get("clocks_max_sm_mhz")
    return {
        "tflops_fp16": payload.get("tflops"),
        "seconds": payload.get("elapsed_seconds"),
        "temperature_max_c": temperature["max"] if temperature else None,
        "temperature_rise_c": (round(temperature["max"] - temperature["min"], 1)
                               if temperature else None),
        "clocks_sm_mhz": clocks["last"] if clocks else None,
        "clocks_sm_max_mhz": ceiling["max"] if ceiling else None,
        "clock_ratio_pct": (round(clocks["last"] / ceiling["max"] * 100)
                            if clocks and ceiling and ceiling["max"] else None),
        "power_w": summary.get("power_w", {}).get("max"),
        "power_limit_w": summary.get("power_limit_w", {}).get("max"),
        "fan_pct": summary.get("fan_pct", {}).get("max"),
        "pcie_gen": summary.get("pcie_gen", {}).get("max"),
        "pcie_width": summary.get("pcie_width", {}).get("max"),
        "throttle_reasons": sorted(reasons),
        "thermally_throttled": any(r in reasons for r in THERMAL_REASONS),
        "samples": len(samples),
        "samples_observed": len(observed),
    }


def compute_processes(gpu_uuid=None):
    """Return the CUDA processes already holding the GPU.

    ``run_state`` only knows about generations this CLI is tracking; a run
    started in another shell leaves no record. Asking the driver covers both.
    """
    command = ["nvidia-smi", "--query-compute-apps=pid,used_memory",
               "--format=csv,noheader,nounits"]
    if gpu_uuid:
        command += ["-i", gpu_uuid]
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    processes = []
    for line in completed.stdout.strip().splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) < 2 or not fields[0].isdigit():
            continue
        processes.append({"pid": int(fields[0]), "used_mib": _number(fields[1])})
    return processes


def _number(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@contextlib.contextmanager
def exclusive():
    """Hold a lock so two measurements cannot saturate the GPU at once."""
    directory = Path(tempfile.gettempdir()) / ("narration-video-gen-%d" % os.getuid())
    directory.mkdir(mode=0o700, exist_ok=True)
    handle = open(directory / "measure.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise MeasureError("another measurement is already running")
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def image_present(image, runner=None):
    run = runner or _docker
    try:
        run(["image", "inspect", image], timeout=60)
    except MeasureError:
        return False
    return True


def _docker(args, timeout=1800, stream=False):
    """Run docker. ``stream`` leaves output attached so progress stays visible."""
    if shutil.which("docker") is None:
        raise MeasureError("docker was not found on PATH")
    try:
        completed = subprocess.run(["docker"] + list(args), text=True,
                                   timeout=timeout, check=False,
                                   capture_output=not stream)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasureError(str(exc))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() if not stream else ""
        raise MeasureError(detail.splitlines()[-1] if detail else
                           "docker %s failed" % args[0])
    return completed.stdout if not stream else ""


def pull(image, runner=None):
    """Fetch an image, leaving docker's progress on the terminal."""
    run = runner or _docker
    run(["pull", image], timeout=7200, stream=True)


def measure(image, seconds=DEFAULT_SECONDS, runner=None, gpu_uuid=None):
    """Run the probe in ``image`` and return the measured record.

    By default the probe sees the GPUs exactly as the generation service does,
    so CUDA picks the same device 0 that a run would use and the verdict
    describes the card that will do the work. ``gpu_uuid`` pins the probe to one
    card instead, for deliberately measuring a specific one.

    The record always names the card that was measured, so the caller never has
    to assume which one CUDA chose.
    """
    run = runner or _docker
    device = "device=%s" % gpu_uuid if gpu_uuid else "all"
    command = ["run", "--rm", "--gpus", device,
               "--entrypoint", "python3", image, "-c", PROBE_SOURCE, str(seconds)]
    output = run(command, timeout=max(600, int(seconds) * 8))

    for line in (output or "").splitlines():
        if line.startswith("@@PROBE@@"):
            payload = json.loads(line[len("@@PROBE@@"):])
            record = _summarise(payload)
            record["schema_version"] = SCHEMA_VERSION
            record["gpu_name"] = payload.get("gpu_name")
            record["gpu_uuid"] = payload.get("gpu_uuid")
            record["telemetry_skipped"] = payload.get("telemetry_skipped")
            record["vram_mib"] = payload.get("vram_mib")
            record["torch"] = payload.get("torch")
            record["cuda"] = payload.get("cuda")
            record["image"] = image
            return record
    raise MeasureError("the probe produced no result")


def measured_gpu(env, record):
    """Return the environment's entry for the GPU the probe actually measured."""
    measured_uuid = record.get("gpu_uuid")
    for gpu in env.get("gpus") or []:
        if measured_uuid and gpu.get("uuid") == measured_uuid:
            return gpu
    if record.get("gpu_name"):
        matches = [gpu for gpu in env.get("gpus") or []
                   if gpu.get("name") == record["gpu_name"]]
        if len(matches) == 1:
            return matches[0]
    gpus = env.get("gpus") or []
    # With nothing to match on, only an unambiguous machine can answer.
    return gpus[0] if len(gpus) == 1 else {}


def vram_gib(env, record):
    """Return the measured card's VRAM in whole gibibytes.

    nvidia-smi reports the card's nameplate capacity; torch reports what is
    addressable, which is lower (a 16 GiB card comes back as 15.99 GiB, and a
    6 GiB card as 5.66 GiB). Cards are sold in whole gibibytes, so the value is
    rounded before it is compared against one.
    """
    reported = measured_gpu(env, record).get("vram_gib")
    if not reported:
        reported = (record.get("vram_mib") or 0) / 1024
    return round(reported)


def warnings_for(env, record):
    """Return the short, actionable warnings for this machine.

    Each entry is a (ja, en) pair. Anything that does not change what the
    reader should do next is left out.
    """
    messages = []
    if record.get("thermally_throttled"):
        messages.append((
            "冷却で性能が制限されています。ヒートシンクの清掃とケースのエアフローを確認してください。",
            "Cooling is limiting performance; check heatsink dust and case airflow.",
        ))

    ram = (env.get("memory") or {}).get("ram_gib") or 0
    if env.get("platform") == "windows-wsl2" and vram_gib(env, record) <= 12 and ram <= 32:
        messages.append((
            "この構成ではフル尺が完走しない例があります。先に短尺で確認してください。",
            "Full-length runs are known to fail on this combination; try a short run first.",
        ))
    return messages


def assess(env, record):
    """Return what the measurement says the reader should act on.

    What this machine can run is the catalog's answer, given by ``select``
    against the real profile requirements. This only reports what a load
    reveals and nvidia-smi cannot say at rest.
    """
    return {"warnings": [{"ja": ja, "en": en} for ja, en in warnings_for(env, record)]}
