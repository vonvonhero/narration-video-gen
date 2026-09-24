#!/usr/bin/env python3
"""Local block-swap calibration for ``narration-video-gen calibrate``.

``calibrate`` materialises named candidate profiles and locates a stable
``blocks_to_swap`` boundary with short, fresh-process attempts.  The CLI starts
this harness in a one-off container built from the normal generation image and
imports the qualified result as a machine-local calibration record.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import copy
import csv
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import resource
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_ROOT = Path(os.environ.get("NVG_ROOT", "/opt/nvg"))
DEFAULT_WORKSPACE = Path(os.environ.get("NVG_WORKSPACE", "/workspace"))
DEFAULT_MODELS_DIR = (Path(os.environ["NVG_MODELS_DIR"])
                      if os.environ.get("NVG_MODELS_DIR") else None)
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IMAGE_DIGEST_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")
VERIFY_TIMEOUT_SECONDS = 600
DEFAULT_RUNTIME_DISK_HEADROOM_GIB = 20
GPU_FIELDS = [
    "timestamp", "name", "uuid", "driver_version", "memory.total",
    "memory.used", "memory.free", "utilization.gpu", "utilization.memory",
    "temperature.gpu", "power.draw", "power.limit", "clocks.sm",
    "clocks.mem", "pstate",
]
ENVIRONMENT_WHITELIST = (
    "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES",
    "NVIDIA_DRIVER_CAPABILITIES", "NVG_IMAGE_REFERENCE",
    "NVG_RUNTIME_BUILD_SHA", "NVG_SOURCE_REVISION",
    "NVG_WORKSPACE", "NVG_MODELS_DIR", "NVG_RESULTS_ROOT",
    "NVG_RUNTIME_DISK_HEADROOM_GIB", "HF_HOME",
    "PYTORCH_CUDA_ALLOC_CONF", "PYTHONUNBUFFERED",
    "HF_HUB_ENABLE_HF_TRANSFER",
)
REQUIRED_CUDA_ALLOCATOR = "expandable_segments:True"
RUNTIME_PACKAGES = (
    "torch", "torchvision", "torchaudio", "xformers", "transformers",
    "diffusers", "accelerate", "safetensors", "numpy", "huggingface-hub",
)
_CALIBRATION_PROFILE = contextvars.ContextVar("calibration_profile", default=None)


def utc_stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def models_dir_for_args(args, workspace):
    configured = getattr(args, "models_dir", None)
    return (Path(configured).resolve()
            if configured else (workspace / "models").resolve())


def runtime_disk_headroom_gib():
    raw = os.environ.get("NVG_RUNTIME_DISK_HEADROOM_GIB")
    if raw is None:
        return DEFAULT_RUNTIME_DISK_HEADROOM_GIB
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("NVG_RUNTIME_DISK_HEADROOM_GIB must be an integer") from exc
    if value < 1:
        raise RuntimeError("NVG_RUNTIME_DISK_HEADROOM_GIB must be at least 1")
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_output(command, timeout=None):
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return {
            "command": command,
            "error": "timed out after %s seconds" % timeout,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        }
    except OSError as exc:
        return {"command": command, "error": str(exc)}
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def captured_command(command, timeout):
    """Run a benchmark child in its own process group and reap it on timeout."""
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr, True


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def validate_run_id(run_id):
    if not RUN_ID_PATTERN.fullmatch(str(run_id)):
        raise ValueError(
            "run id must start with an alphanumeric character and contain only "
            "letters, numbers, dot, underscore, or hyphen")
    return str(run_id)


def bounded_run_id(value):
    """Keep child run ids valid without constraining the caller's parent id."""
    value = str(value)
    if len(value) <= 128:
        return validate_run_id(value)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return validate_run_id("%s-%s" % (value[:111], digest))


def bytes_to_gib(value):
    return round(value / (1024 ** 3), 3) if value is not None else None


def bytes_to_requirement_gib(value):
    """Round a measured capacity down to the CLI detector's 0.01 GiB unit."""
    return ((int(value) * 100) // (1024 ** 3)) / 100 if value is not None else None


def source_revision(root):
    configured = os.environ.get("NVG_SOURCE_REVISION")
    if configured:
        return configured
    result = command_output(["git", "-C", str(root), "rev-parse", "HEAD"])
    return result.get("stdout") or "unknown"


def load_catalog(root):
    sys.path.insert(0, str(root / "src"))
    from narration_video_gen.catalog import Catalog
    from narration_video_gen.compat import load_yaml_file

    scenario_set = load_yaml_file(root / "calibration" / "scenarios.yaml")
    catalog = Catalog(root)
    override = _CALIBRATION_PROFILE.get()
    if override is not None:
        reference_id, profile = override
        catalog.profiles[reference_id] = copy.deepcopy(profile)
    return (
        catalog,
        load_yaml_file(root / "manifests" / "models.lock.yaml"),
        scenario_set,
    )


def selected_scenarios(scenario_set, requested):
    scenarios = list(scenario_set["scenarios"])
    if not requested:
        return scenarios
    wanted = set(requested)
    known = {item["id"] for item in scenarios}
    missing = sorted(wanted - known)
    if missing:
        raise ValueError("unknown scenario(s): %s" % ", ".join(missing))
    return [item for item in scenarios if item["id"] in wanted]


MAIN_STAGES = ("infinitetalk", "s2v")


def pipeline_scope(main_only):
    return "main" if main_only else "e2e"


def main_stage(recipe):
    stages = list(recipe.get("pipeline_stages") or [])
    matches = [stage for stage in stages if stage in MAIN_STAGES]
    if len(matches) != 1:
        raise ValueError(
            "recipe %s must declare exactly one main generation stage"
            % recipe.get("id", "<unknown>"))
    return matches[0]


def scoped_recipe(recipe, scope):
    if scope == "e2e":
        return recipe
    if scope != "main":
        raise ValueError("unknown pipeline scope: %s" % scope)
    resolved = copy.deepcopy(recipe)
    resolved["pipeline_stages"] = [main_stage(recipe)]
    return resolved


def selected_models(root, all_models, scenarios=None, scope="e2e"):
    catalog, lock, scenario_set = load_catalog(root)
    lock_by_id = {item["id"]: item for item in lock["models"]}
    sys.path.insert(0, str(root / "src"))
    from narration_video_gen.runner import MODEL_ROLES, STAGE_ROLES
    wanted = []
    for scenario in scenarios or scenario_set["scenarios"]:
        profile = catalog.profiles[scenario["profile"]]
        recipe = scoped_recipe(catalog.recipe_for(profile), scope)
        # Match runner.resolve_models exactly: when more than one recipe entry
        # offers a role, the later entry is the file the workflow addresses.
        # Downloading every role candidate would preserve an unused weight.
        role_model_ids = {}
        for model_id in recipe.get("models", []):
            for role in MODEL_ROLES.get(model_id, ()):
                role_model_ids[role] = model_id
        needed_roles = set()
        for stage in recipe.get("pipeline_stages") or []:
            needed_roles.update(STAGE_ROLES.get(stage, ()))
        unresolved_roles = sorted(needed_roles - set(role_model_ids))
        if unresolved_roles:
            raise RuntimeError(
                "recipe %s has no model for role(s): %s"
                % (recipe["id"], ", ".join(unresolved_roles)))
        resolved_ids = {
            role_model_ids[role] for role in needed_roles if role in role_model_ids}
        for model_id in recipe.get("models", []):
            if model_id not in resolved_ids:
                continue
            if model_id not in wanted:
                wanted.append(model_id)
    if all_models:
        wanted = [item["id"] for item in lock["models"]]
    missing = [model_id for model_id in wanted if model_id not in lock_by_id]
    if missing:
        raise RuntimeError("models missing from lock: %s" % ", ".join(missing))
    return [lock_by_id[model_id] for model_id in wanted], scenario_set


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip(), None
    except OSError as exc:
        return None, "%s: %s" % (path, exc)


def _parse_meminfo(path="/proc/meminfo"):
    text, error = _read_text(path)
    if error:
        return {}, error
    values = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        match = re.search(r"\d+", raw)
        if match:
            values[key] = int(match.group()) * 1024
    return values, None


def _limit_value(path):
    text, error = _read_text(path)
    if error:
        return None, error
    if text == "max":
        return "unlimited", None
    try:
        return int(text), None
    except ValueError:
        return text, None


def _os_release():
    text, error = _read_text("/etc/os-release")
    if error:
        return {"status": "unavailable", "reason": error}
    allowed = {
        "NAME", "ID", "ID_LIKE", "VERSION", "VERSION_ID", "PRETTY_NAME",
        "BUILD_ID", "VARIANT", "VARIANT_ID",
    }
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in allowed:
            values[key.lower()] = value.strip().strip("\"").strip("'")
    return {"status": "available", **values}


def _cpu_details():
    text, error = _read_text("/proc/cpuinfo")
    model = None
    if text:
        for line in text.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in ("model name", "Hardware", "Processor"):
                model = value.strip()
                if model:
                    break
    nodes = []
    node_root = Path("/sys/devices/system/node")
    try:
        node_paths = sorted(
            node_root.glob("node[0-9]*"), key=lambda item: int(item.name[4:]))
    except OSError:
        node_paths = []
    for node_path in node_paths:
        cpulist, cpulist_error = _read_text(node_path / "cpulist")
        meminfo, meminfo_error = _read_text(node_path / "meminfo")
        total = None
        if meminfo:
            match = re.search(r"MemTotal:\s*(\d+)\s*kB", meminfo)
            if match:
                total = int(match.group(1)) * 1024
        node = {
            "node": int(node_path.name[4:]),
            "cpu_list": cpulist,
            "memory_total_bytes": total,
        }
        errors = [item for item in (cpulist_error, meminfo_error) if item]
        if errors:
            node["collection_errors"] = errors
        nodes.append(node)
    try:
        affinity_count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity_count = None
    result = {
        "status": "available" if model or os.cpu_count() else "partial",
        "architecture": platform.machine() or None,
        "model": model,
        "logical_cpu_count": os.cpu_count(),
        "affinity_logical_cpu_count": affinity_count,
        "numa_node_count": len(nodes) if nodes else None,
        "numa_nodes": nodes,
    }
    if error:
        result["collection_errors"] = [error]
    return result


def _cgroup_details():
    v2 = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    root = Path("/sys/fs/cgroup")
    membership, membership_error = _read_text("/proc/self/cgroup")
    v1 = bool(membership and any(
        len(line.split(":", 2)) == 3 and line.split(":", 2)[1]
        for line in membership.splitlines()))
    if membership:
        for line in membership.splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
                relative = Path(fields[2].lstrip("/"))
                if ".." not in relative.parts:
                    candidate = root / relative
                    if candidate.is_dir():
                        root = candidate
                break
    files = {
        "memory_max_bytes": "memory.max",
        "memory_swap_max_bytes": "memory.swap.max",
        "cpu_max": "cpu.max",
        "cpuset_cpus_effective": "cpuset.cpus.effective",
        "cpuset_mems_effective": "cpuset.mems.effective",
        "pids_max": "pids.max",
    }
    result = {
        "status": "available" if v2 else ("partial" if v1 else "unavailable"),
        "version": 2 if v2 else (1 if v1 else None),
    }
    errors = []
    if not v2:
        result["reason"] = (
            "cgroup v1 detected; unified v2 limit files unavailable" if v1
            else "cgroup mount not detected")
        if membership_error:
            result["collection_errors"] = [membership_error]
        return result
    if membership_error:
        errors.append(membership_error)
    for key, relative in files.items():
        value, error = _limit_value(root / relative)
        result[key] = value
        if error:
            errors.append(error)
    if errors:
        result["status"] = "partial"
        result["collection_errors"] = errors
    return result


def _decode_mount_path(value):
    return re.sub(
        r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _workspace_filesystem(workspace):
    resolved = Path(workspace).resolve()
    result = {"status": "available", "workspace": str(resolved)}
    try:
        usage = shutil.disk_usage(str(resolved))
        result.update({
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        })
    except OSError as exc:
        result.update({"status": "partial", "capacity_error": str(exc)})
    text, error = _read_text("/proc/self/mountinfo")
    if error:
        result.update({"status": "partial", "mount_error": error})
        return result
    matches = []
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        trailing = after.split()
        if not separator or len(fields) < 6 or not trailing:
            continue
        mount_point = Path(_decode_mount_path(fields[4]))
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        matches.append((len(str(mount_point)), mount_point, fields[5], trailing[0]))
    if matches:
        _length, mount_point, options, filesystem_type = max(matches)
        result.update({
            "mount_point": str(mount_point),
            "filesystem_type": filesystem_type,
            "mount_options": sorted(options.split(",")),
        })
    else:
        result.update({"status": "partial", "mount_error": "workspace mount not found"})
    return result


def _runtime_details(root):
    try:
        from importlib import metadata as importlib_metadata
    except ImportError:  # pragma: no cover - Python 3.8+ always has this
        importlib_metadata = None
    packages = {}
    for package in RUNTIME_PACKAGES:
        try:
            packages[package] = importlib_metadata.version(package)
        except importlib_metadata.PackageNotFoundError:
            packages[package] = None
        except Exception as exc:  # metadata corruption must not stop a run
            packages[package] = {"status": "unavailable", "reason": str(exc)}
    comfy_root = Path("/opt/ComfyUI")
    comfy_revision = command_output(
        ["git", "-C", str(comfy_root), "rev-parse", "HEAD"], timeout=10)
    cuda_version = None
    nvidia_header = command_output(["nvidia-smi"], timeout=15)
    if nvidia_header.get("returncode") == 0:
        match = re.search(r"CUDA Version:\s*([0-9.]+)", nvidia_header.get("stdout", ""))
        cuda_version = match.group(1) if match else None
    torch_probe = command_output([
        sys.executable, "-c",
        "import json, torch; print(json.dumps({"
        "'torch_version': torch.__version__, "
        "'cuda_compiled_version': torch.version.cuda, "
        "'cudnn_version': torch.backends.cudnn.version(), "
        "'cuda_available': torch.cuda.is_available()}))",
    ], timeout=30)
    torch_runtime = None
    if torch_probe.get("returncode") == 0:
        try:
            torch_runtime = json.loads(torch_probe.get("stdout", ""))
        except ValueError:
            pass
    toolkit_probe = command_output(["nvcc", "--version"], timeout=10)
    toolkit_version = None
    if toolkit_probe.get("returncode") == 0:
        matches = re.findall(r"release\s+([0-9.]+)", toolkit_probe.get("stdout", ""))
        toolkit_version = matches[-1] if matches else None
    container_cli = command_output(
        ["nvidia-container-cli", "--version"], timeout=10)
    return {
        "status": "available",
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "packages": packages,
        "cuda_reported_by_driver": cuda_version,
        "cuda_driver_query": {
            "status": "available" if nvidia_header.get("returncode") == 0 else "unavailable",
            "reason": nvidia_header.get("stderr") or nvidia_header.get("error"),
        },
        "torch_runtime": torch_runtime or {
            "status": "unavailable",
            "reason": torch_probe.get("stderr") or torch_probe.get("error")
                      or "torch runtime probe returned invalid output",
        },
        "cuda_toolkit": {
            "status": "available" if toolkit_probe.get("returncode") == 0 else "unavailable",
            "version": toolkit_version,
            "reason": (toolkit_probe.get("stderr") or toolkit_probe.get("error")
                       if toolkit_probe.get("returncode") != 0 else None),
        },
        "nvidia_container_cli": {
            "status": "available" if container_cli.get("returncode") == 0 else "unavailable",
            "version_output": (container_cli.get("stdout")
                               if container_cli.get("returncode") == 0 else None),
            "reason": (container_cli.get("stderr") or container_cli.get("error")
                       if container_cli.get("returncode") != 0 else None),
        },
        "comfyui_revision": (
            comfy_revision.get("stdout")
            if comfy_revision.get("returncode") == 0 else None),
        "comfyui_revision_error": (
            comfy_revision.get("stderr") or comfy_revision.get("error")
            if comfy_revision.get("returncode") != 0 else None),
        "source_revision": source_revision(root),
    }


def _resource_limits():
    names = (
        "RLIMIT_AS", "RLIMIT_CORE", "RLIMIT_CPU", "RLIMIT_DATA",
        "RLIMIT_FSIZE", "RLIMIT_MEMLOCK", "RLIMIT_NOFILE", "RLIMIT_NPROC",
        "RLIMIT_STACK",
    )
    limits = {}
    errors = []
    for name in names:
        limit_id = getattr(resource, name, None)
        if limit_id is None:
            continue
        try:
            soft, hard = resource.getrlimit(limit_id)
            limits[name] = {
                "soft": "unlimited" if soft == resource.RLIM_INFINITY else soft,
                "hard": "unlimited" if hard == resource.RLIM_INFINITY else hard,
            }
        except (OSError, ValueError) as exc:
            errors.append("%s: %s" % (name, exc))
    result = {"status": "available" if limits else "unavailable", "limits": limits}
    if errors:
        result["status"] = "partial" if limits else "unavailable"
        result["collection_errors"] = errors
    return result


def collect_host_system(root, workspace):
    """Collect non-secret, best-effort execution-platform evidence."""
    uname = platform.uname()
    meminfo, meminfo_error = _parse_meminfo()
    gpu_rows, gpu_result = nvidia_rows()
    memory = {
        "status": "available" if meminfo else "unavailable",
        "memory_total_bytes": meminfo.get("MemTotal"),
        "swap_total_bytes": meminfo.get("SwapTotal"),
    }
    if meminfo_error:
        memory["reason"] = meminfo_error
    gpu = {
        "status": "available" if gpu_rows else "unavailable",
        "devices": gpu_rows,
    }
    if not gpu_rows:
        gpu["reason"] = gpu_result.get("stderr") or gpu_result.get("error") or "no GPU found"
    return {
        "schema_version": 1,
        "captured_at": utc_now(),
        "kernel": {
            "status": "available" if uname.system else "unavailable",
            "system": uname.system or None,
            "release": uname.release or None,
            "version": uname.version or None,
            "machine": uname.machine or None,
        },
        "container_os": _os_release(),
        "container": {
            "docker_marker": Path("/.dockerenv").exists(),
            "containerenv_marker": Path("/run/.containerenv").exists(),
        },
        "cpu": _cpu_details(),
        "memory": memory,
        "cgroup": _cgroup_details(),
        "workspace_filesystem": _workspace_filesystem(workspace),
        "runtime": _runtime_details(root),
        "gpu": gpu,
        "resource_limits": _resource_limits(),
        "environment": {
            key: os.environ[key] for key in ENVIRONMENT_WHITELIST if key in os.environ
        },
    }


def begin_run(root, workspace, kind, run_id=None):
    run_id = validate_run_id(run_id or "%s-%s" % (kind, utc_stamp()))
    run_dir = workspace / "results" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "schema_version": 2,
        "run_id": run_id,
        "kind": kind,
        "started_at": utc_now(),
        "source_revision": source_revision(root),
        "runtime_build_sha": os.environ.get("NVG_RUNTIME_BUILD_SHA", "unknown"),
        "image_reference": os.environ.get("NVG_IMAGE_REFERENCE", "unknown"),
        "workspace": str(workspace),
    }
    write_json(run_dir / "benchmark.json", metadata)
    try:
        host_system = collect_host_system(root, workspace)
    except Exception as exc:  # evidence collection must never prevent a benchmark
        host_system = {
            "schema_version": 1,
            "captured_at": utc_now(),
            "status": "unavailable",
            "reason": "%s: %s" % (type(exc).__name__, exc),
        }
    write_json(run_dir / "host-system.json", host_system)
    return run_dir, metadata


def update_metadata(run_dir, **updates):
    path = Path(run_dir) / "benchmark.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    write_json(path, payload)


def nvidia_rows():
    result = command_output([
        "nvidia-smi", "--query-gpu=" + ",".join(GPU_FIELDS),
        "--format=csv,noheader,nounits",
    ], timeout=15)
    if result.get("returncode") != 0:
        return [], result
    rows = []
    for values in csv.reader(result["stdout"].splitlines()):
        values = [value.strip() for value in values]
        if len(values) == len(GPU_FIELDS):
            rows.append(dict(zip(GPU_FIELDS, values)))
    return rows, result


def gpu_snapshot():
    rows, result = nvidia_rows()
    return {
        "captured_at": utc_now(),
        "gpus": rows,
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "stderr": result.get("stderr", ""),
    }


def nvidia_diagnostics():
    """Capture driver state useful after a native GPU failure."""
    return command_output(["nvidia-smi", "-q"], timeout=20)


def kernel_gpu_events():
    """Return only NVIDIA/Xid kernel messages, or why they are unavailable."""
    result = command_output(["dmesg", "--color=never"], timeout=15)
    record = {
        "captured_at": utc_now(),
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "error": result.get("error"),
        "stderr": result.get("stderr", ""),
        "available": result.get("returncode") == 0,
        "events": [],
    }
    if record["available"]:
        pattern = re.compile(r"(?:NVRM|\bXid\b|GPU has fallen off the bus)", re.I)
        record["events"] = [
            line for line in result.get("stdout", "").splitlines()
            if pattern.search(line)
        ]
    return record


def process_memory_snapshot(pid):
    """Read non-secret ComfyUI process state without cmdline or environment."""
    record = {
        "captured_at": utc_now(), "pid": pid, "available": False,
        "state": None, "threads": None, "vm_rss_bytes": None,
        "vm_swap_bytes": None, "rss_anon_bytes": None,
        "rss_file_bytes": None, "rss_shmem_bytes": None,
        "smaps_rss_bytes": None, "smaps_pss_bytes": None,
        "smaps_private_bytes": None,
    }
    if pid is None:
        return record
    status_path = Path("/proc") / str(pid) / "status"
    status = {}
    try:
        for line in status_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                status[key] = value.strip()
    except OSError as exc:
        record["error"] = str(exc)
        return record

    def kibibytes(key):
        match = re.search(r"\d+", status.get(key, ""))
        return int(match.group()) * 1024 if match else None

    record.update({
        "available": True,
        "state": status.get("State"),
        "threads": int(status["Threads"]) if status.get("Threads", "").isdigit() else None,
        "vm_rss_bytes": kibibytes("VmRSS"),
        "vm_swap_bytes": kibibytes("VmSwap"),
        "rss_anon_bytes": kibibytes("RssAnon"),
        "rss_file_bytes": kibibytes("RssFile"),
        "rss_shmem_bytes": kibibytes("RssShmem"),
    })
    try:
        smaps = {}
        for line in (Path("/proc") / str(pid) / "smaps_rollup").read_text(
                encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                match = re.search(r"\d+", value)
                if match:
                    smaps[key] = int(match.group()) * 1024
        record.update({
            "smaps_rss_bytes": smaps.get("Rss"),
            "smaps_pss_bytes": smaps.get("Pss"),
            "smaps_private_bytes": (
                smaps.get("Private_Clean", 0) + smaps.get("Private_Dirty", 0)
                if "Private_Clean" in smaps or "Private_Dirty" in smaps else None),
        })
    except OSError as exc:
        record["smaps_error"] = str(exc)
    return record


def comfy_process_observation(process):
    returncode = process.poll()
    signal_number = -returncode if returncode is not None and returncode < 0 else None
    try:
        signal_name = signal.Signals(signal_number).name if signal_number else None
    except ValueError:
        signal_name = "UNKNOWN"
    return {
        "captured_at": utc_now(),
        "pid": process.pid,
        "running": returncode is None,
        "returncode": returncode,
        "signal_number": signal_number,
        "signal_name": signal_name,
        "memory": process_memory_snapshot(process.pid),
    }


def _cgroup_memory_stat(root, version):
    values = {}
    try:
        for line in (Path(root) / "memory.stat").read_text(
                encoding="utf-8").splitlines():
            key, value = line.split(None, 1)
            values[key] = int(value)
    except (OSError, ValueError):
        return {}
    if version == 2:
        return {
            "cgroup_anon_bytes": values.get("anon"),
            "cgroup_file_bytes": values.get("file"),
            "cgroup_kernel_bytes": values.get("kernel"),
            "cgroup_shmem_bytes": values.get("shmem"),
        }
    return {
        "cgroup_anon_bytes": values.get("total_rss", values.get("rss")),
        "cgroup_file_bytes": values.get("total_cache", values.get("cache")),
        "cgroup_kernel_bytes": (
            values.get("total_kernel_stack", values.get("kernel_stack", 0))
            + values.get("total_slab", values.get("slab", 0))),
        "cgroup_shmem_bytes": values.get("total_shmem", values.get("shmem")),
    }


def _read_int(path):
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def _memory_pressure(path):
    result = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if not fields:
                continue
            category = fields[0]
            for field in fields[1:]:
                key, separator, value = field.partition("=")
                if separator:
                    result["%s_%s" % (category, key)] = float(value)
    except (OSError, ValueError):
        pass
    return result


def _cgroup_memory_snapshot():
    """Return comparable memory evidence for cgroup v2 or legacy v1."""
    v2_root = Path("/sys/fs/cgroup")
    if (v2_root / "memory.current").is_file():
        events = {}
        try:
            for line in (v2_root / "memory.events").read_text(
                    encoding="utf-8").splitlines():
                key, value = line.split(None, 1)
                events[key] = int(value)
        except (OSError, ValueError):
            pass
        pressure = _memory_pressure(v2_root / "memory.pressure")
        return {
            "cgroup_version": 2,
            "cgroup_memory_current_bytes": _read_int(v2_root / "memory.current"),
            "cgroup_memory_peak_bytes": _read_int(v2_root / "memory.peak"),
            "cgroup_memory_max_bytes": _read_int(v2_root / "memory.max"),
            "cgroup_swap_current_bytes": _read_int(v2_root / "memory.swap.current"),
            "cgroup_swap_peak_bytes": _read_int(v2_root / "memory.swap.peak"),
            "cgroup_swap_max_bytes": _read_int(v2_root / "memory.swap.max"),
            "cgroup_v2_available": True,
            "cgroup_memory_pressure_available": (v2_root / "memory.pressure").is_file(),
            "cgroup_oom": events.get("oom"),
            "cgroup_oom_kill": events.get("oom_kill"),
            "cgroup_pressure_some_avg10": pressure.get("some_avg10"),
            "cgroup_pressure_full_avg10": pressure.get("full_avg10"),
            **_cgroup_memory_stat(v2_root, 2),
        }

    v1_root = Path("/sys/fs/cgroup/memory")
    if not (v1_root / "memory.usage_in_bytes").is_file():
        return {"cgroup_version": None, "cgroup_v2_available": False,
                "cgroup_memory_pressure_available": False}
    memory_current = _read_int(v1_root / "memory.usage_in_bytes")
    memory_peak = _read_int(v1_root / "memory.max_usage_in_bytes")
    memory_max = _read_int(v1_root / "memory.limit_in_bytes")
    memsw_current = _read_int(v1_root / "memory.memsw.usage_in_bytes")
    memsw_peak = _read_int(v1_root / "memory.memsw.max_usage_in_bytes")
    memsw_max = _read_int(v1_root / "memory.memsw.limit_in_bytes")
    oom_control = {}
    try:
        for line in (v1_root / "memory.oom_control").read_text(
                encoding="utf-8").splitlines():
            key, value = line.split(None, 1)
            oom_control[key] = int(value)
    except (OSError, ValueError):
        pass

    def difference(total, memory):
        return max(total - memory, 0) if total is not None and memory is not None else None

    return {
        "cgroup_version": 1,
        "cgroup_memory_current_bytes": memory_current,
        "cgroup_memory_peak_bytes": memory_peak,
        "cgroup_memory_max_bytes": memory_max,
        # cgroup v1 reports memory+swap (memsw), so expose swap-only values.
        "cgroup_swap_current_bytes": difference(memsw_current, memory_current),
        "cgroup_swap_peak_bytes": difference(memsw_peak, memory_peak),
        "cgroup_swap_max_bytes": difference(memsw_max, memory_max),
        "cgroup_v2_available": False,
        "cgroup_memory_pressure_available": False,
        "cgroup_oom": _read_int(v1_root / "memory.failcnt"),
        "cgroup_oom_kill": oom_control.get("oom_kill"),
        "cgroup_pressure_some_avg10": None,
        "cgroup_pressure_full_avg10": None,
        **_cgroup_memory_stat(v1_root, 1),
    }


def host_snapshot(workspace):
    meminfo = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            match = re.search(r"\d+", value)
            if match:
                meminfo[key] = int(match.group()) * 1024
    except OSError:
        pass
    try:
        disk_free = shutil.disk_usage(str(workspace)).free
    except OSError:
        disk_free = None
    cgroup = _cgroup_memory_snapshot()
    system_pressure = _memory_pressure("/proc/pressure/memory")
    return {
        "mem_total_bytes": meminfo.get("MemTotal"),
        "mem_available_bytes": meminfo.get("MemAvailable"),
        "swap_total_bytes": meminfo.get("SwapTotal"),
        "swap_free_bytes": meminfo.get("SwapFree"),
        **cgroup,
        "system_pressure_some_avg10": system_pressure.get("some_avg10"),
        "system_pressure_full_avg10": system_pressure.get("full_avg10"),
        "workspace_free_bytes": disk_free,
    }


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ResourceMonitor:
    """Write attempt-labelled GPU/host samples and retain peak summaries."""

    def __init__(self, destination, interval, workspace):
        self.destination = Path(destination)
        self.interval = interval
        self.workspace = Path(workspace)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.attempt_id = None
        self.comfy_pid = None
        self.summaries = {}

    def start(self):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def set_attempt(self, attempt_id):
        self.attempt_id = attempt_id

    def set_comfy_process(self, process):
        self.comfy_pid = process.pid if process is not None else None

    def close(self):
        self.stop.set()
        self.thread.join(timeout=self.interval + 5)

    def summary(self, attempt_id=None):
        key = attempt_id or "unassigned"
        return copy.deepcopy(self.summaries.get(key, {}))

    def _update_summary(self, key, gpu, host):
        summary = self.summaries.setdefault(key, {"gpu_max_memory_used_mib": {}})
        summary["sample_count"] = summary.get("sample_count", 0) + 1
        gpu_key = gpu.get("uuid") or gpu.get("name") or "unknown"
        used = _number(gpu.get("memory.used"))
        if used is not None:
            previous = summary["gpu_max_memory_used_mib"].get(gpu_key, 0)
            summary["gpu_max_memory_used_mib"][gpu_key] = max(previous, used)
        for source, target, choose in (
                ("mem_available_bytes", "min_mem_available_bytes", min),
                ("workspace_free_bytes", "min_workspace_free_bytes", min),
                ("cgroup_memory_current_bytes", "max_cgroup_memory_current_bytes", max),
                ("cgroup_memory_peak_bytes", "max_cgroup_memory_peak_bytes", max),
                ("cgroup_memory_max_bytes", "cgroup_memory_max_bytes", max),
                ("cgroup_swap_current_bytes", "max_cgroup_swap_current_bytes", max),
                ("cgroup_swap_peak_bytes", "max_cgroup_swap_peak_bytes", max),
                ("cgroup_swap_max_bytes", "cgroup_swap_max_bytes", max),
                ("cgroup_oom", "max_cgroup_oom", max),
                ("cgroup_oom_kill", "max_cgroup_oom_kill", max),
                ("cgroup_pressure_some_avg10", "max_cgroup_pressure_some_avg10", max),
                ("cgroup_pressure_full_avg10", "max_cgroup_pressure_full_avg10", max),
                ("system_pressure_some_avg10", "max_system_pressure_some_avg10", max),
                ("system_pressure_full_avg10", "max_system_pressure_full_avg10", max),
                ("cgroup_anon_bytes", "max_cgroup_anon_bytes", max),
                ("cgroup_file_bytes", "max_cgroup_file_bytes", max),
                ("comfy_vm_rss_bytes", "max_comfy_vm_rss_bytes", max),
                ("comfy_vm_swap_bytes", "max_comfy_vm_swap_bytes", max),
                ("comfy_rss_anon_bytes", "max_comfy_rss_anon_bytes", max),
                ("comfy_smaps_pss_bytes", "max_comfy_smaps_pss_bytes", max)):
            value = host.get(source)
            if value is None:
                continue
            previous = summary.get(target)
            summary[target] = value if previous is None else choose(previous, value)
        total, free = host.get("swap_total_bytes"), host.get("swap_free_bytes")
        if total is not None and free is not None:
            summary["max_host_swap_used_bytes"] = max(
                summary.get("max_host_swap_used_bytes", 0), total - free)
            summary["host_swap_total_bytes"] = total
        total, available = host.get("mem_total_bytes"), host.get("mem_available_bytes")
        if total is not None and available is not None:
            summary["max_host_memory_used_bytes"] = max(
                summary.get("max_host_memory_used_bytes", 0), total - available)
            summary["host_memory_total_bytes"] = total

    def _run(self):
        fields = [
            "captured_at", "attempt_id", "stage", "gpu_index", *GPU_FIELDS,
            "mem_total_bytes", "mem_available_bytes", "swap_total_bytes",
            "swap_free_bytes", "cgroup_memory_current_bytes",
            "cgroup_memory_peak_bytes", "cgroup_swap_current_bytes",
            "cgroup_memory_max_bytes", "cgroup_swap_peak_bytes",
            "cgroup_swap_max_bytes", "cgroup_oom", "cgroup_oom_kill",
            "cgroup_version", "cgroup_v2_available",
            "cgroup_memory_pressure_available",
            "cgroup_pressure_some_avg10", "cgroup_pressure_full_avg10",
            "system_pressure_some_avg10", "system_pressure_full_avg10",
            "workspace_free_bytes", "cgroup_anon_bytes", "cgroup_file_bytes",
            "cgroup_kernel_bytes", "cgroup_shmem_bytes", "comfy_pid",
            "comfy_process_available", "comfy_state", "comfy_threads",
            "comfy_vm_rss_bytes", "comfy_vm_swap_bytes",
            "comfy_rss_anon_bytes", "comfy_rss_file_bytes",
            "comfy_rss_shmem_bytes", "comfy_smaps_rss_bytes",
            "comfy_smaps_pss_bytes", "comfy_smaps_private_bytes",
        ]
        with self.destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            while not self.stop.is_set():
                captured = utc_now()
                attempt_id = self.attempt_id or "unassigned"
                stage = None
                if self.attempt_id:
                    state_path = (self.destination.parent / "outputs" /
                                  self.attempt_id / "run-state.json")
                    try:
                        stage = json.loads(
                            state_path.read_text(encoding="utf-8")).get("stage")
                    except (OSError, ValueError):
                        pass
                gpus, _result = nvidia_rows()
                host = host_snapshot(self.workspace)
                process = process_memory_snapshot(self.comfy_pid)
                process_fields = {
                    "comfy_pid": process.get("pid"),
                    "comfy_process_available": process.get("available"),
                    "comfy_state": process.get("state"),
                    "comfy_threads": process.get("threads"),
                    "comfy_vm_rss_bytes": process.get("vm_rss_bytes"),
                    "comfy_vm_swap_bytes": process.get("vm_swap_bytes"),
                    "comfy_rss_anon_bytes": process.get("rss_anon_bytes"),
                    "comfy_rss_file_bytes": process.get("rss_file_bytes"),
                    "comfy_rss_shmem_bytes": process.get("rss_shmem_bytes"),
                    "comfy_smaps_rss_bytes": process.get("smaps_rss_bytes"),
                    "comfy_smaps_pss_bytes": process.get("smaps_pss_bytes"),
                    "comfy_smaps_private_bytes": process.get("smaps_private_bytes"),
                }
                for index, gpu in enumerate(gpus or [{}]):
                    row = {"captured_at": captured, "attempt_id": attempt_id,
                           "stage": stage, "gpu_index": index, **gpu, **host,
                           **process_fields}
                    writer.writerow(row)
                    self._update_summary(attempt_id, gpu, {**host, **process_fields})
                handle.flush()
                self.stop.wait(self.interval)


def remove_bundled_output_marker(path):
    """Remove only ComfyUI's known empty placeholder before mounting output."""
    marker = path / "_output_images_will_be_put_here"
    if (marker.is_file() and not marker.is_symlink()
            and marker.stat().st_size == 0):
        marker.unlink()


def activate_output(root, run_dir):
    output = run_dir / "outputs"
    output.mkdir(parents=True, exist_ok=True)
    for path in (Path("/opt/ComfyUI/output"), root / "outputs"):
        if path == Path("/opt/ComfyUI/output") and path.is_dir():
            remove_bundled_output_marker(path)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            if any(path.iterdir()):
                raise RuntimeError("refusing to replace non-empty output directory: %s" % path)
            path.rmdir()
        path.symlink_to(output)
    return output


def wait_for_comfy(log_path, process=None, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                "ComfyUI exited with %s; inspect %s" % (process.returncode, log_path))
        try:
            with urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(2)
    raise RuntimeError("ComfyUI did not become ready; inspect %s" % log_path)


def start_comfy(run_dir, label, timeout):
    log_path = run_dir / "comfy" / (label + ".log")
    lifecycle_path = run_dir / "comfy" / (label + "-process.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONFAULTHANDLER"] = "1"
    started_at = utc_now()
    started_epoch = time.time()
    kernel_before = kernel_gpu_events()
    nvidia_before = nvidia_diagnostics()
    cgroup_before = _cgroup_memory_snapshot()
    process = subprocess.Popen([
        sys.executable, "/opt/ComfyUI/main.py", "--listen", "127.0.0.1",
        "--port", "8188", "--disable-pinned-memory", "--disable-cuda-malloc",
        "--disable-async-offload",
    ], stdout=handle, stderr=subprocess.STDOUT, env=environment)
    process._nvg_started_at = started_at
    process._nvg_started_epoch = started_epoch
    process._nvg_lifecycle_path = lifecycle_path
    process._nvg_kernel_before = kernel_before
    process._nvg_nvidia_before = nvidia_before
    process._nvg_cgroup_before = cgroup_before
    try:
        wait_for_comfy(log_path, process=process, timeout=timeout)
    except Exception:
        stop_comfy(process, handle)
        raise
    return process, handle, log_path


def stop_comfy(process, handle):
    before_cleanup = comfy_process_observation(process)
    controller_action = "none"
    try:
        if process.poll() is None:
            controller_action = "sigterm"
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                controller_action = "sigkill-after-timeout"
                process.kill()
                process.wait(timeout=20)
    finally:
        handle.close()
    after_cleanup = comfy_process_observation(process)
    kernel_after = kernel_gpu_events()
    kernel_before = getattr(process, "_nvg_kernel_before", {})
    previous_events = set(kernel_before.get("events") or [])
    lifecycle = {
        "schema_version": 1,
        "started_at": getattr(process, "_nvg_started_at", None),
        "finished_at": utc_now(),
        "elapsed_seconds": round(
            max(time.time() - getattr(process, "_nvg_started_epoch", time.time()), 0), 3),
        "python_faulthandler": True,
        "exit_observed_before_cleanup": not before_cleanup["running"],
        "controller_action": controller_action,
        "before_cleanup": before_cleanup,
        "after_cleanup": after_cleanup,
        "cgroup_before": getattr(process, "_nvg_cgroup_before", {}),
        "cgroup_after": _cgroup_memory_snapshot(),
        "nvidia_before": getattr(process, "_nvg_nvidia_before", {}),
        "nvidia_after": nvidia_diagnostics(),
        "kernel_gpu_events_before": kernel_before,
        "kernel_gpu_events_after": kernel_after,
        "new_kernel_gpu_events": [
            item for item in kernel_after.get("events") or []
            if item not in previous_events
        ],
    }
    lifecycle_path = getattr(process, "_nvg_lifecycle_path", None)
    if lifecycle_path is not None:
        write_json(lifecycle_path, lifecycle)
    return lifecycle


def require_gpu_context(metadata, allow_unknown_image):
    rows, result = nvidia_rows()
    if not rows:
        raise RuntimeError(
            "GPU benchmark requires a visible NVIDIA GPU: %s" % result.get("stderr", ""))
    if len(rows) != 1:
        raise RuntimeError(
            "GPU benchmark requires exactly one visible NVIDIA GPU; found %d" % len(rows))
    allocator = os.environ.get("PYTORCH_CUDA_ALLOC_CONF")
    if allocator != REQUIRED_CUDA_ALLOCATOR:
        raise RuntimeError(
            "PYTORCH_CUDA_ALLOC_CONF must be %s; got %s" % (
                REQUIRED_CUDA_ALLOCATOR, allocator or "unset"))
    image_reference = metadata.get("image_reference", "unknown")
    if not allow_unknown_image and not IMAGE_DIGEST_PATTERN.search(image_reference):
        raise RuntimeError(
            "NVG_IMAGE_REFERENCE must be an immutable image@sha256 digest")
    return rows


def verify_selected_models(models, models_dir, destination):
    records = []
    failures = []
    for item in models:
        path = models_dir / item["path"]
        record = {"id": item["id"], "path": str(path), "expected_bytes": item["bytes"]}
        if not path.is_file():
            record["status"] = "missing"
        elif path.stat().st_size != item["bytes"]:
            record.update({"status": "size-mismatch", "actual_bytes": path.stat().st_size})
        else:
            digest = sha256_file(path)
            record.update({"status": "ok" if digest == item["sha256"] else "hash-mismatch",
                           "sha256": digest})
        if record["status"] != "ok":
            failures.append(record["id"])
        records.append(record)
    write_json(destination, {"checked_at": utc_now(), "models": records})
    if failures:
        raise RuntimeError(
            "GPU model preflight failed; run prepare again: %s" % ", ".join(failures))
    return records


def verify_runtime_disk(workspace, destination):
    """Require generation headroom, not the profile's install-time budget."""
    usage = shutil.disk_usage(str(workspace))
    required_gib = runtime_disk_headroom_gib()
    required_bytes = required_gib * 1024 ** 3
    record = {
        "checked_at": utc_now(),
        "workspace": str(workspace),
        "free_bytes": usage.free,
        "free_gib": bytes_to_gib(usage.free),
        "required_free_bytes": required_bytes,
        "required_free_gib": required_gib,
        "status": "ok" if usage.free >= required_bytes else "insufficient",
        "requirement_kind": "generation-headroom",
    }
    write_json(destination, record)
    if record["status"] != "ok":
        raise RuntimeError(
            "GPU runtime disk preflight failed: workspace has %.2f GiB free; "
            "generation requires at least %d GiB of headroom"
            % (record["free_gib"], required_gib))
    return record


def input_evidence(root, scenario_set):
    evidence = {}
    for key in ("image", "audio"):
        path = root / scenario_set["input"][key]
        if not path.is_file():
            raise RuntimeError("benchmark input is missing: %s" % path)
        evidence[key] = {
            "path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return evidence


def clean_profile(profile):
    result = copy.deepcopy(profile)
    result.pop("_path", None)
    return result


def gpu_slug(gpu):
    name = re.sub(r"[^a-z0-9]+", "-", gpu.get("name", "gpu").lower()).strip("-")
    total = _number(gpu.get("memory.total")) or 0
    vram = max(1, int(round(total / 1024)))
    return "%s-vram%d" % (name or "gpu", vram), vram


def framepack_decode_offload_required(recipe, gpu):
    """Whether this GPU needs the 720p FramePack decode VRAM workaround."""
    resolution = recipe.get("resolution") or []
    total_mib = _number(gpu.get("memory.total"))
    return bool(
        recipe.get("model_family") == "wan22-s2v"
        and len(resolution) >= 2
        and _number(resolution[1]) is not None
        and _number(resolution[1]) >= 720
        and total_mib is not None
        and 0 < total_mib <= 16 * 1024
    )


def load_profile_hypotheses(root, scenario_set):
    from narration_video_gen.compat import load_yaml_file

    relative = scenario_set["calibration"]["candidate_profile_set"]
    payload = load_yaml_file(root / relative)
    hypotheses = payload.get("profile_hypotheses") or []
    if not hypotheses:
        raise ValueError("candidate profile set is empty: %s" % relative)
    seen = set()
    resolved = []
    for hypothesis in hypotheses:
        hypothesis_id = hypothesis.get("id")
        settings = hypothesis.get("settings") or {}
        blocks = settings.get("blocks_to_swap")
        if (not hypothesis_id or hypothesis_id in seen or not isinstance(blocks, int)
                or blocks < 0):
            raise ValueError("invalid block-swap profile hypothesis: %r" % hypothesis)
        seen.add(hypothesis_id)
        resolved.append({"id": hypothesis_id, "settings": dict(settings)})
    resolved.sort(key=lambda item: item["settings"]["blocks_to_swap"])
    return payload["id"], resolved


def load_search_start_table(root, scenario_set):
    """Load updateable VRAM/scenario hints used only to order calibration."""
    from narration_video_gen.compat import load_yaml_file

    relative = scenario_set["calibration"]["search_start_table"]
    payload = load_yaml_file(root / relative)
    if payload.get("schema_version") != 1 or not payload.get("id"):
        raise ValueError("invalid block-swap search start table: %s" % relative)
    expected_scenarios = {item["id"] for item in scenario_set["scenarios"]}
    tiers = payload.get("tiers") or []
    resolved = []
    seen_vram = set()
    for item in tiers:
        vram_gib = item.get("vram_gib")
        starts = item.get("starts") or {}
        if (not isinstance(vram_gib, int) or vram_gib <= 0
                or vram_gib in seen_vram
                or set(starts) != expected_scenarios
                or any(not isinstance(value, int) or value < 0
                       for value in starts.values())):
            raise ValueError("invalid block-swap search tier: %r" % item)
        seen_vram.add(vram_gib)
        resolved.append({"vram_gib": vram_gib, "starts": dict(starts)})
    resolved.sort(key=lambda item: item["vram_gib"])
    if not resolved:
        raise ValueError("block-swap search start table is empty: %s" % relative)
    for scenario_id in expected_scenarios:
        starts = [item["starts"][scenario_id] for item in resolved]
        if starts != sorted(starts, reverse=True):
            raise ValueError(
                "%s block-swap search starts must not increase with VRAM"
                % scenario_id)
    above_max = payload.get("above_max_vram_blocks_to_swap", 0)
    if not isinstance(above_max, int) or above_max < 0:
        raise ValueError("invalid above-max block-swap search start: %r" % above_max)
    return {
        "id": payload["id"],
        "path": relative,
        "tiers": resolved,
        "above_max_vram_blocks_to_swap": above_max,
    }


def select_search_start(search_table, scenario_id, gpu, available_blocks):
    """Choose an OOM-biased seed without treating the hint as evidence."""
    total_mib = _number(gpu.get("memory.total"))
    nominal_vram = None
    selected_tier = None
    if total_mib is not None and total_mib > 0:
        nominal_vram = max(1, int(math.ceil(total_mib / 1024)))
        selected_tier = next(
            (item for item in search_table["tiers"]
             if item["vram_gib"] >= nominal_vram), None)
    if selected_tier is None:
        blocks = search_table["above_max_vram_blocks_to_swap"]
    else:
        blocks = selected_tier["starts"][scenario_id]
    if blocks not in set(available_blocks):
        raise ValueError(
            "%s search start blocks_to_swap=%d is absent from the candidate grid"
            % (scenario_id, blocks))
    return {
        "scenario": scenario_id,
        "table": search_table["id"],
        "gpu_memory_total_mib": total_mib,
        "nominal_vram_gib": nominal_vram,
        "matched_vram_tier_gib": (
            selected_tier["vram_gib"] if selected_tier is not None else None),
        "blocks_to_swap": blocks,
    }


def load_face_detailer_profiles(root, scenario_set):
    """Load independently calibrated Face Detailer settings for VRAM tiers."""
    from narration_video_gen.compat import load_yaml_file

    relative = scenario_set["calibration"].get("face_detailer_profile_set")
    if not relative:
        return {"id": None, "profiles": []}
    payload = load_yaml_file(root / relative)
    profiles = payload.get("profiles") or []
    known_scenarios = {item["id"] for item in scenario_set["scenarios"]}
    seen = set()
    for profile in profiles:
        settings = profile.get("settings") or {}
        key = (profile.get("scenario"), profile.get("gpu_vram_gib"))
        if (not profile.get("id") or profile.get("status") not in {
                "provisional", "qualified"}
                or key[0] not in known_scenarios
                or not isinstance(key[1], int) or key[1] <= 0
                or key in seen
                or not isinstance(settings.get("face_detailer_size"), int)
                or settings["face_detailer_size"] <= 0
                or settings["face_detailer_size"] % 16
                or not isinstance(settings.get("face_detailer_blocks_to_swap"), int)
                or not 0 <= settings["face_detailer_blocks_to_swap"] <= 40):
            raise ValueError("invalid Face Detailer profile: %r" % profile)
        seen.add(key)
    if payload.get("schema_version") != 1 or not payload.get("id"):
        raise ValueError("invalid Face Detailer profile set: %s" % relative)
    return {"id": payload["id"], "profiles": profiles}


def select_face_detailer_profile(profile_set, scenario_id, nominal_vram):
    """Return an exact scenario/VRAM match; never guess across resolutions."""
    return next((profile for profile in profile_set.get("profiles") or []
                 if profile["scenario"] == scenario_id
                 and profile["gpu_vram_gib"] == nominal_vram), None)


def materialize_candidate_profiles(root, run_dir, scenarios, gpu, scenario_set,
                                   base_profile=None):
    catalog, _lock, _set = load_catalog(root)
    if base_profile is not None:
        if not isinstance(base_profile, dict):
            raise ValueError("calibration base profile must be a profile mapping")
        catalog._validate_profile(base_profile, "calibration base profile")
        if (len(scenarios) != 1 or base_profile["recipe"]
                != catalog.profiles[scenarios[0]["profile"]]["recipe"]):
            raise ValueError("calibration base profile must match the selected scenario")
    candidate_set_id, hypotheses = load_profile_hypotheses(root, scenario_set)
    search_table = load_search_start_table(root, scenario_set)
    face_profiles = load_face_detailer_profiles(root, scenario_set)
    available_blocks = [item["settings"]["blocks_to_swap"] for item in hypotheses]
    available_block_set = set(available_blocks)
    invalid_starts = sorted({
        value for item in search_table["tiers"]
        for value in item["starts"].values()
        if value not in available_block_set
    })
    if search_table["above_max_vram_blocks_to_swap"] not in available_block_set:
        invalid_starts.append(search_table["above_max_vram_blocks_to_swap"])
    if invalid_starts:
        raise ValueError("search starts are absent from the candidate grid: %r"
                         % sorted(set(invalid_starts)))
    destination = run_dir / "profiles" / "candidates"
    slug, nominal_vram = gpu_slug(gpu)
    result = {}
    plan = []
    for scenario in scenarios:
        base = clean_profile(catalog.profiles[scenario["profile"]])
        recipe = catalog.recipe_for(base)
        candidate_settings = dict(base.get("settings") or {})
        face_profile = select_face_detailer_profile(
            face_profiles, scenario["id"], nominal_vram)
        if face_profile:
            candidate_settings.update(face_profile["settings"])
        if recipe.get("model_family") == "wan22-s2v":
            # The catalog's 16 GiB profile carries the safe default. Recompute it
            # from the actual GPU so 480p and cards above 16 GiB avoid an
            # unnecessary transformer reload between FramePack windows.
            candidate_settings["offload_transformer_before_vae_decode"] = (
                framepack_decode_offload_required(recipe, gpu))
        # Explicit settings in the selected named profile take precedence over
        # the generic scenario defaults, including the GPU-derived S2V hint.
        supplied_settings = (base_profile or {}).get("settings") or {}
        for key in ("vae_native_upsample", "offload_transformer_before_vae_decode",
                    "vram_ballast_mib", "vram_ballast_device_index",
                    "encode_tiled", "decode_tiled", "use_non_blocking"):
            if key in supplied_settings:
                candidate_settings[key] = supplied_settings[key]
        discovery_requirements = {
            "gpu_vram_gib_min": nominal_vram,
            "docker": False,
            "nvidia_runtime": False,
        }
        result[scenario["id"]] = []
        for hypothesis in hypotheses:
            blocks = hypothesis["settings"]["blocks_to_swap"]
            profile = copy.deepcopy(base)
            profile_id = "calibration-%s-%s" % (scenario["id"], hypothesis["id"])
            profile.update({
                "id": profile_id,
                "status": "experimental",
                "summary": (
                    "Run-scoped calibration hypothesis for %s at blocks_to_swap=%d."
                    % (scenario["id"], blocks)),
                # Discovery must not inherit the reference host's RAM or
                # swap minima.  The current machine's observed capacity is attached
                # only after a candidate passes.
                "requires": discovery_requirements,
                "settings": {**candidate_settings, **hypothesis["settings"]},
                "evidence": {
                    "duration_class": "short",
                    "frames": recipe["short_test_frames"],
                    "seconds": round(recipe["short_test_frames"] / recipe["fps"], 4),
                    "pipeline_stages": list(recipe.get("pipeline_stages") or []),
                    "gpu_evidence": ("capacity-simulated" if supplied_settings.get(
                        "vram_ballast_mib") else "physical"),
                    "gpu_model": gpu.get("name", "unmeasured"),
                    "visual_review": "pending",
                    "calibration_state": "planned",
                },
                "limitations": [
                    "Run-scoped hypothesis only; do not publish before stable short and full qualification.",
                    "Host RAM and swap requirements must be inferred from telemetry and validated separately.",
                ],
                "calibration_candidate": {
                    "candidate_set": candidate_set_id,
                    "hypothesis_id": hypothesis["id"],
                    "gpu_slug": slug,
                    "face_detailer_profile": (
                        face_profile["id"] if face_profile else None),
                },
            })
            # Calibration replaces the base profile's evidence with a short run
            # on the current GPU.  A published Full timing belongs to the base
            # profile's original duration and GPU; retaining it here would make
            # the runtime estimator scale that Full timing from the new short
            # duration and can inflate ETA by an order of magnitude.  The first
            # Full qualification deliberately starts without a reference ETA.
            profile.pop("timing", None)
            path = destination / scenario["id"] / (profile_id + ".yaml")
            write_json(path, profile)
            entry = {
                "id": profile_id,
                "path": path,
                "directory": path.parent,
                "blocks_to_swap": blocks,
                "sha256": sha256_file(path),
                "profile": profile,
            }
            result[scenario["id"]].append(entry)
            plan.append({key: str(value) if isinstance(value, Path) else value
                         for key, value in entry.items() if key != "profile" and key != "directory"})
    write_json(run_dir / "calibration-plan.json", {
        "schema_version": 2,
        "base_profile_snapshot": base_profile,
        "candidate_set": candidate_set_id,
        "search_start_table": search_table["id"],
        "selection_safety_margin_blocks": int(
            scenario_set["calibration"].get("selection_safety_margin_blocks", 0)),
        "search_starts": {
            scenario["id"]: select_search_start(
                search_table, scenario["id"], gpu, available_blocks)
            for scenario in scenarios
        },
        "profiles": plan,
    })
    return result


def load_zero_swap_block_margin_table(root, scenario_set):
    """Load Short-run VRAM released by one swapped block for bs=0 guarding."""
    from narration_video_gen.compat import load_yaml_file

    relative = scenario_set["calibration"].get("zero_swap_block_margin_table")
    if not relative:
        raise ValueError("zero-swap block margin table is required")
    payload = load_yaml_file(root / relative)
    expected = {item["id"] for item in scenario_set["scenarios"]}
    tiers = []
    for item in payload.get("tiers") or []:
        vram = item.get("vram_gib")
        per_block = item.get("vram_per_block_mib") or {}
        status = item.get("evidence_status") or {}
        bootstrap = item.get("bootstrap_source_vram_gib") or {}
        if (not isinstance(vram, int) or vram <= 0
                or set(per_block) != expected or set(status) != expected
                or set(bootstrap) != expected
                or any(_number(value) is None or _number(value) <= 0
                       for value in per_block.values())
                or any(value not in {"observed", "provisional"}
                       for value in status.values())):
            raise ValueError("invalid zero-swap block margin table: %s" % relative)
        tiers.append({"vram_gib": vram, "per_block": dict(per_block),
                      "status": dict(status), "bootstrap": dict(bootstrap)})
    if payload.get("schema_version") != 1 or not payload.get("id") or not tiers:
        raise ValueError("invalid zero-swap block margin table: %s" % relative)
    return {"id": payload["id"], "tiers": sorted(tiers, key=lambda item: item["vram_gib"])}


def zero_swap_block_margin_policy(table, scenario_id, gpu, margin_blocks):
    _slug, vram_gib = gpu_slug(gpu)
    tier = min(table["tiers"], key=lambda item: (
        abs(item["vram_gib"] - vram_gib), -item["vram_gib"]))
    return {
        "table_id": table["id"], "gpu_vram_gib": vram_gib,
        "source_vram_gib": tier["vram_gib"],
        "vram_per_block_mib": _number(tier["per_block"][scenario_id]),
        "margin_blocks": margin_blocks,
        "evidence_status": tier["status"][scenario_id],
        "bootstrap_source_vram_gib": tier["bootstrap"][scenario_id],
        "tier_match": "exact" if tier["vram_gib"] == vram_gib else "nearest",
    }


def classify_failure(returncode, stdout, stderr, timed_out=False, host_oom=False):
    if timed_out:
        return "timeout"
    if returncode == 0:
        return None
    if host_oom:
        return "host-oom"
    text = (stdout + "\n" + stderr).lower()
    if any(token in text for token in (
            "gpu-out-of-memory", "cuda out of memory", "gpu memory ran out",
            "gpuメモリ不足")):
        return "gpu-oom"
    if any(token in text for token in (
            "host-out-of-memory", "host memory ran out", "ホストのメモリ不足",
            "oom_kill")):
        return "host-oom"
    return "execution"


def classify_saturated_gpu_device_loss(failure, stdout, stderr, telemetry, gpu_before,
                                       wsl_allocator_shim_active=None):
    """Treat a WSL CUDA device loss at saturated VRAM as a capacity boundary."""
    if failure != "execution":
        return failure
    text = (stdout + "\n" + stderr).lower()
    if "cuda driver error: device not ready" not in text:
        return failure
    totals = []
    for gpu in (gpu_before or {}).get("gpus", []):
        try:
            totals.append(float(gpu.get("memory.total")))
        except (TypeError, ValueError):
            continue
    peaks = []
    for value in (telemetry or {}).get("gpu_max_memory_used_mib", {}).values():
        try:
            peaks.append(float(value))
        except (TypeError, ValueError):
            continue
    if wsl_allocator_shim_active is None:
        wsl_allocator_shim_active = (
            "vmm-rdma-interpose.so" in os.environ.get("LD_PRELOAD", ""))
    # "device not ready" alone can also mean an unrelated driver failure.
    # WSL can reject a dxg residency request with ENOMEM before the requested
    # pages become resident and visible to nvidia-smi.  Keep the stricter
    # threshold elsewhere, but accept the observed earlier boundary when the
    # WSL allocator shim proves this is the WSL VMM path.
    saturation_ratio = 0.80 if wsl_allocator_shim_active else 0.97
    if totals and peaks and max(peaks) >= max(totals) * saturation_ratio:
        return "gpu-oom"
    return failure


def telemetry_errors(record):
    """Return missing evidence that makes an otherwise-passed attempt unusable."""
    summary = record.get("telemetry_summary") or {}
    errors = []
    if (summary.get("sample_count") or 0) < 1:
        errors.append("no attempt-labelled resource sample")
    if not summary.get("gpu_max_memory_used_mib"):
        errors.append("no GPU memory sample")
    cgroup_versions = []
    for snapshot_name in ("host_before", "host_after"):
        host = record.get(snapshot_name) or {}
        for key in (
                "mem_total_bytes", "mem_available_bytes", "swap_total_bytes",
                "swap_free_bytes", "cgroup_memory_current_bytes",
                "cgroup_swap_current_bytes", "cgroup_oom", "cgroup_oom_kill"):
            if host.get(key) is None:
                errors.append("missing %s metric %s" % (snapshot_name, key))
        version = host.get(
            "cgroup_version", 2 if host.get("cgroup_v2_available") else None)
        cgroup_versions.append(version)
        if version not in (1, 2):
            errors.append("%s cgroup memory controller is unavailable" % snapshot_name)
        if version == 2:
            if not host.get("cgroup_memory_pressure_available"):
                errors.append("%s cgroup memory pressure is unavailable" % snapshot_name)
            elif (host.get("cgroup_pressure_some_avg10") is None
                  or host.get("cgroup_pressure_full_avg10") is None):
                errors.append("%s cgroup memory pressure could not be parsed" % snapshot_name)
    for key in (
            "host_memory_total_bytes", "max_host_memory_used_bytes",
            "host_swap_total_bytes", "max_host_swap_used_bytes",
            "max_cgroup_memory_current_bytes", "max_cgroup_swap_current_bytes",
            "max_cgroup_oom", "max_cgroup_oom_kill",
            "max_system_pressure_some_avg10", "max_system_pressure_full_avg10"):
        if summary.get(key) is None:
            errors.append("missing telemetry summary %s" % key)
    if 2 in cgroup_versions:
        for key in ("max_cgroup_pressure_some_avg10",
                    "max_cgroup_pressure_full_avg10"):
            if summary.get(key) is None:
                errors.append("missing telemetry summary %s" % key)
    return errors


def cli_prefix(root, profile_dirs):
    command = [str(root / "bin" / "narration-video-gen"), "--root", str(root)]
    for directory in sorted({str(Path(path)) for path in profile_dirs}):
        command.extend(["--profile-dir", directory])
    return command


def completed_run_output(root, child_id, expected_name=None, expected_stage=None):
    """Resolve the runner's terminal output without guessing ComfyUI suffixes."""
    output_dir = root / "outputs" / child_id
    contract_path = output_dir / "media-contract.json"
    if expected_stage is not None and contract_path.is_file():
        try:
            stages = json.loads(
                contract_path.read_text(encoding="utf-8")).get("stages") or []
        except (OSError, ValueError):
            stages = []
        matches = [item for item in stages if item.get("stage") == expected_stage]
        if matches:
            recorded = matches[-1].get("audio_companion") or matches[-1].get("video")
            container_prefix = "/opt/ComfyUI/output/"
            if recorded and recorded.startswith(container_prefix):
                candidate = root / "outputs" / recorded[len(container_prefix):]
            else:
                candidate = Path(recorded) if recorded else None
                if candidate is not None and not candidate.is_absolute():
                    candidate = root / candidate
            if candidate is not None and candidate.is_file():
                return candidate
    state_path = root / "outputs" / child_id / "run-state.json"
    if state_path.is_file():
        try:
            relative = json.loads(state_path.read_text(encoding="utf-8")).get("output")
        except (OSError, ValueError):
            relative = None
        if relative:
            candidate = Path(relative)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_file():
                return candidate
    if expected_name is not None:
        return root / "outputs" / child_id / expected_name
    return None


def main_verify_expectation(root, profile_entry, audio_path):
    catalog, _lock, _scenario_set = load_catalog(root)
    recipe = catalog.recipes[profile_entry["profile"]["recipe"]]
    sys.path.insert(0, str(root / "src"))
    from narration_video_gen.verify import frames_for_audio, wav_duration
    return {
        "width": recipe["resolution"][0],
        "height": recipe["resolution"][1],
        "fps": recipe["fps"],
        "frames": frames_for_audio(wav_duration(audio_path), recipe["fps"]),
        "audio_present": True,
    }


def execute_attempt(root, run_dir, scenario_set, scenario, profile_entry, attempt_id,
                    length, replicate, phase, reason, monitor, timeout, cache_state,
                    main_only=False):
    attempt_path = run_dir / "attempts" / (attempt_id + ".json")
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "scenario": scenario["id"],
        "phase": phase,
        "selection_reason": reason,
        "profile": profile_entry["id"],
        "profile_sha256": profile_entry["sha256"],
        "profile_snapshot": profile_entry["profile"],
        "blocks_to_swap": (profile_entry["profile"].get("settings") or {}).get("blocks_to_swap"),
        "length": length,
        "replicate": replicate,
        "cache_state": cache_state,
        "pipeline_scope": pipeline_scope(main_only),
        "status": "planned",
        "planned_at": utc_now(),
    }
    write_json(attempt_path, record)
    record.update({"status": "running", "started_at": utc_now()})
    write_json(attempt_path, record)
    monitor.set_attempt(attempt_id)
    host_before = host_snapshot(run_dir)
    gpu_before = gpu_snapshot()
    child_id = attempt_id
    command = cli_prefix(root, [profile_entry["directory"]]) + [
        "run", "--profile", profile_entry["id"],
        "--image", str(root / scenario_set["input"]["image"]),
        "--audio", str(root / scenario_set["input"]["audio"]),
        "--length", length, "--run-id", child_id,
        "--server", "127.0.0.1:8188", "--container", "self", "--force",
    ]
    selected_main_stage = None
    if main_only:
        catalog, _lock, _scenario_set = load_catalog(root)
        recipe = catalog.recipe_for(profile_entry["profile"])
        selected_main_stage = main_stage(recipe)
        command.extend(["--stages", selected_main_stage])
        record["stages"] = [selected_main_stage]
    started = time.monotonic()
    returncode, stdout, stderr, timed_out = captured_command(command, timeout)
    record.update({
        "finished_at": utc_now(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    })
    host_after = host_snapshot(run_dir)
    gpu_after = gpu_snapshot()
    event_delta = {}
    for key in ("cgroup_oom", "cgroup_oom_kill"):
        before, after = host_before.get(key), host_after.get(key)
        event_delta[key] = (after - before if before is not None and after is not None
                            else None)
    record.update({
        "host_before": host_before,
        "host_after": host_after,
        "host_event_delta": event_delta,
        "gpu_before": gpu_before,
        "gpu_after": gpu_after,
    })
    failure = classify_failure(
        returncode, stdout, stderr, timed_out=timed_out,
        host_oom=bool((event_delta.get("cgroup_oom_kill") or 0) > 0))
    output = completed_run_output(
        root, child_id, None if main_only else "04-retime.mp4",
        expected_stage=selected_main_stage)
    if failure is None and (output is None or not output.is_file()):
        failure = "output-missing"
    if failure is None:
        verify_audio = (root / "outputs" / child_id / "input-short-test.wav"
                        if length == "short" else root / scenario_set["input"]["audio"])
        verify_command = cli_prefix(root, [profile_entry["directory"]])
        verify_command.insert(1, "--json")
        verify_command.extend(["verify", str(output)])
        if main_only:
            expect_path = run_dir / "attempts" / (attempt_id + "-verify-expect.json")
            write_json(
                expect_path, main_verify_expectation(root, profile_entry, verify_audio))
            verify_command.extend(["--expect", str(expect_path)])
            record["verify_expectation"] = str(expect_path.relative_to(run_dir))
        else:
            verify_command.extend([
                "--profile", profile_entry["id"], "--audio", str(verify_audio)])
        verify_command.extend(["--container", "self"])
        verify_returncode, verify_stdout, verify_stderr, verify_timed_out = (
            captured_command(verify_command, VERIFY_TIMEOUT_SECONDS))
        try:
            verify_payload = json.loads(verify_stdout) if verify_stdout else None
        except json.JSONDecodeError:
            verify_payload = None
        record.update({
            "verify_command": verify_command,
            "verify_returncode": verify_returncode,
            "verify_timed_out": verify_timed_out,
            "verify": verify_payload,
            "verify_stdout": verify_stdout,
            "verify_stderr": verify_stderr,
        })
        if verify_returncode != 0 or verify_timed_out:
            failure = "verification"
        else:
            record["output"] = str(Path("outputs") / child_id / output.name)
            record["output_bytes"] = output.stat().st_size
            record["output_sha256"] = sha256_file(output)
    monitor.set_attempt(None)
    record["telemetry_summary"] = monitor.summary(attempt_id)
    failure = classify_saturated_gpu_device_loss(
        failure, stdout, stderr, record["telemetry_summary"], gpu_before)
    incomplete = telemetry_errors(record) if failure is None else []
    if incomplete:
        failure = "telemetry-incomplete"
        record["telemetry_errors"] = incomplete
    record["failure_category"] = failure
    record["status"] = "passed" if failure is None else "failed"
    write_json(attempt_path, record)
    return record


@contextlib.contextmanager
def attempt_ballast(root, run_dir, profile_entry, attempt_id):
    """Materialize the profile's allocation before Comfy starts; keep it alive."""
    settings = profile_entry["profile"].get("settings") or {}
    if not settings.get("vram_ballast_mib"):
        yield
        return
    log_path = run_dir / "attempts" / (attempt_id + "-ballast.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as handle:
        process = subprocess.Popen([
            sys.executable, "-u", str(root / "scripts" / "hold-vram-ballast.py"),
            "--profile-file", str(profile_entry["path"])],
            stdout=handle, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            ready = False
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("VRAM ballast exited before readiness: " + str(log_path))
                for line in log_path.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event") == "ready":
                        expected = settings["vram_ballast_mib"] * 1024**2
                        if event.get("allocated_bytes") != expected:
                            raise RuntimeError("VRAM ballast allocation does not match profile")
                        ready = True
                if ready:
                    break
                time.sleep(0.5)
            if not ready:
                raise RuntimeError("VRAM ballast readiness timed out")
            yield
            if process.poll() is not None:
                raise RuntimeError("VRAM ballast exited during measured attempt")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def fresh_attempt(root, run_dir, scenario_set, scenario, profile_entry, attempt_id,
                  length, replicate, phase, reason, monitor, args):
    try:
        with attempt_ballast(root, run_dir, profile_entry, attempt_id):
            return _fresh_attempt(root, run_dir, scenario_set, scenario, profile_entry,
                                  attempt_id, length, replicate, phase, reason, monitor, args)
    except Exception as exc:
        path = run_dir / "attempts" / (attempt_id + ".json")
        if path.is_file():
            record = json.loads(path.read_text())
            if record.get("status") == "passed":
                record.update(status="failed", failure_category="attempt-lifecycle",
                              controller_error=str(exc))
                write_json(path, record)
        raise


def _fresh_attempt(root, run_dir, scenario_set, scenario, profile_entry, attempt_id,
                  length, replicate, phase, reason, monitor, args):
    process, handle, log_path = start_comfy(run_dir, attempt_id, args.comfy_timeout_seconds)
    if hasattr(monitor, "set_comfy_process"):
        monitor.set_comfy_process(process)
    record = None
    attempt_path = run_dir / "attempts" / (attempt_id + ".json")
    error = None
    try:
        record = execute_attempt(
            root, run_dir, scenario_set, scenario, profile_entry, attempt_id,
            length, replicate, phase, reason, monitor, args.attempt_timeout_seconds,
            "cold-process", main_only=getattr(args, "main_only", False))
        return record
    except Exception as exc:
        error = exc
        raise
    finally:
        observation = comfy_process_observation(process)
        if hasattr(monitor, "set_comfy_process"):
            monitor.set_comfy_process(None)
        lifecycle = stop_comfy(process, handle)
        if record is None and attempt_path.is_file():
            try:
                record = json.loads(attempt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = None
        if record is not None:
            record["comfy_log"] = str(log_path.relative_to(run_dir))
            record["comfy_process_evidence"] = str(
                getattr(process, "_nvg_lifecycle_path").relative_to(run_dir))
            record["comfy_process_observation"] = observation
            record["comfy_exit_observed_before_cleanup"] = lifecycle[
                "exit_observed_before_cleanup"]
            if lifecycle["exit_observed_before_cleanup"]:
                record["comfy_returncode"] = observation["returncode"]
                record["comfy_signal"] = observation["signal_name"]
                if record.get("failure_category") == "execution":
                    record["failure_category"] = "comfy-process-exit"
            if error is not None and record.get("status") in ("planned", "running"):
                record.update({
                    "status": "failed",
                    "failure_category": "benchmark-controller-exception",
                    "finished_at": utc_now(),
                    "controller_error": str(error),
                })
            write_json(attempt_path, record)


def attempt_id(metadata, scenario, profile_entry, sequence, label):
    blocks = profile_entry["blocks_to_swap"]
    return bounded_run_id("%s-%s-bs%02d-%03d-%s" % (
        metadata["run_id"], scenario["id"], blocks, sequence, label))


def capacity_observation(records):
    """Describe the tested host capacity separately from inferred peak usage."""
    summaries = [record.get("telemetry_summary") or {} for record in records
                 if record.get("status") == "passed"]
    starts = [record.get("host_before") or {} for record in records
              if record.get("status") == "passed"]
    ram_total = max(
        [item.get("mem_total_bytes") or 0 for item in starts]
        + [item.get("host_memory_total_bytes") or 0 for item in summaries])
    swap_total = max(
        [item.get("swap_total_bytes") or 0 for item in starts]
        + [item.get("host_swap_total_bytes") or 0 for item in summaries])
    ram_limits = [item.get("cgroup_memory_max_bytes")
                  for item in [*starts, *summaries]
                  if item.get("cgroup_memory_max_bytes") is not None]
    swap_limits = [item.get("cgroup_swap_max_bytes")
                   for item in [*starts, *summaries]
                   if item.get("cgroup_swap_max_bytes") is not None]
    cgroup_ram_max = max(ram_limits) if ram_limits else None
    cgroup_swap_max = max(swap_limits) if swap_limits else None
    effective_ram = (
        min(ram_total, cgroup_ram_max) if cgroup_ram_max is not None else ram_total)
    effective_swap = (
        min(swap_total, cgroup_swap_max) if cgroup_swap_max is not None else swap_total)
    peak_ram = max([item.get("max_host_memory_used_bytes") or 0 for item in summaries],
                   default=0)
    peak_swap = max([item.get("max_host_swap_used_bytes") or 0 for item in summaries],
                    default=0)
    return {
        "basis": "observed-capacity-not-minimum",
        "configured_host_ram_gib": bytes_to_requirement_gib(effective_ram),
        "configured_host_swap_gib": bytes_to_requirement_gib(effective_swap),
        "observed_host_ram_used_peak_gib": bytes_to_gib(peak_ram),
        "observed_host_swap_used_peak_gib": bytes_to_gib(peak_swap),
        "kernel_host_ram_gib": bytes_to_gib(ram_total),
        "kernel_host_swap_gib": bytes_to_gib(swap_total),
        "cgroup_memory_max_gib": bytes_to_gib(cgroup_ram_max),
        "cgroup_swap_max_gib": bytes_to_gib(cgroup_swap_max),
    }


def gpu_capacity_observation(gpu, records):
    total = _number(gpu.get("memory.total"))
    used_values = []
    for record in records:
        by_gpu = (record.get("telemetry_summary") or {}).get(
            "gpu_max_memory_used_mib") or {}
        used_values.extend(value for value in by_gpu.values()
                           if _number(value) is not None)
    peak = max((_number(value) for value in used_values), default=None)
    return {
        "gpu_uuid": gpu.get("uuid"),
        "memory_total_mib": total,
        "memory_used_peak_mib": peak,
        "headroom_mib": round(total - peak, 3)
        if total is not None and peak is not None else None,
    }


def short_gpu_headroom_mib(gpu, records, profile_id):
    """Return the worst observed Short VRAM headroom for one candidate."""
    total = _number(gpu.get("memory.total"))
    if total is None:
        return None, None
    peaks = []
    for record in records:
        if (record.get("status") != "passed"
                or record.get("profile") != profile_id):
            continue
        by_gpu = (record.get("telemetry_summary") or {}).get(
            "gpu_max_memory_used_mib") or {}
        peaks.extend(_number(value) for value in by_gpu.values()
                     if _number(value) is not None)
    if not peaks:
        return None, None
    peak = max(peaks)
    return peak, total - peak


def promote_profile(run_dir, scenario, candidate, gpu, records,
                    raw_candidate, raw_lower_candidate, raw_failure_candidate,
                    lower_boundary_outcome, configured_margin,
                    safety_baseline_blocks=None, zero_swap_policy=None):
    slug, _nominal = gpu_slug(gpu)
    if safety_baseline_blocks is None:
        safety_baseline_blocks = raw_candidate["blocks_to_swap"]
    grid_minimum_margin_waived = bool(
        configured_margin > 0
        and raw_candidate is not None
        and raw_candidate["blocks_to_swap"] == 0
        and candidate["blocks_to_swap"] == 0
        and lower_boundary_outcome == "grid-minimum")
    profile = copy.deepcopy(candidate["profile"])
    profile["id"] = "calibration-%s-%s-bs%02d" % (
        scenario["id"], slug, candidate["blocks_to_swap"])
    passed = [record for record in records
              if record["profile"] == candidate["id"] and record["status"] == "passed"]
    # A bs=0 guard may deliberately select +1..+3 directly for Full after the
    # three bs=0 Short confirmations.  Its host-capacity evidence is therefore
    # the confirmed zero-swap run, not an unexecuted selected profile.
    if not passed and raw_candidate is not None:
        passed = [record for record in records
                  if (record["profile"] == raw_candidate["id"]
                      and record["status"] == "passed")]
    capacity = capacity_observation(passed)
    profile["requires"].update({
        "host_ram_gib_min": capacity["configured_host_ram_gib"],
        "swap_gib_min": capacity["configured_host_swap_gib"],
    })
    profile["summary"] = (
        "Calibration candidate for %s on %s with blocks_to_swap=%d."
        % (scenario["id"], gpu.get("name", "unknown GPU"), candidate["blocks_to_swap"]))
    profile["evidence"].update({
        "calibration_state": (
            "short-stable-grid-minimum" if grid_minimum_margin_waived
            else "short-stable-safety-margin"),
        "raw_short_pass_boundary_blocks_to_swap": (
            raw_candidate["blocks_to_swap"]
            if raw_candidate is not None else None),
        "raw_short_failure_boundary_blocks_to_swap": (
            raw_failure_candidate["blocks_to_swap"]
            if raw_failure_candidate is not None else None),
        "raw_short_lower_boundary_blocks_to_swap": (
            raw_lower_candidate["blocks_to_swap"]
            if raw_lower_candidate is not None else None),
        "raw_short_lower_boundary_outcome": lower_boundary_outcome,
        "selection_safety_margin_blocks": configured_margin,
        "selection_safety_margin_waived_at_grid_minimum": (
            grid_minimum_margin_waived),
        "effective_safety_margin_blocks": (
            candidate["blocks_to_swap"] - safety_baseline_blocks),
        "raw_short_pass_boundary_attempts": [
            record["attempt_id"] for record in records
            if (raw_candidate is not None
                and record["scenario"] == scenario["id"]
                and record["blocks_to_swap"]
                == raw_candidate["blocks_to_swap"])],
        "raw_short_failure_boundary_attempts": [
            record["attempt_id"] for record in records
            if (record["scenario"] == scenario["id"]
                and raw_failure_candidate is not None
                and record["blocks_to_swap"]
                == raw_failure_candidate["blocks_to_swap"])],
        "raw_short_lower_boundary_attempts": [
            record["attempt_id"] for record in records
            if (record["scenario"] == scenario["id"]
                and raw_lower_candidate is not None
                and record["blocks_to_swap"]
                == raw_lower_candidate["blocks_to_swap"])],
        "calibration_attempts": [record["attempt_id"] for record in records
                                 if record["profile"] == candidate["id"]],
        "short_passes": len(passed),
        "telemetry_summaries": [record.get("telemetry_summary", {}) for record in passed],
        "capacity_observation": capacity,
        "gpu_capacity_observation": gpu_capacity_observation(gpu, passed),
        "zero_swap_full_headroom_policy": zero_swap_policy,
    })
    selection_limit = (
        "Selected at the zero-swap grid minimum after three fresh-process short passes. The Short VRAM peak retained the configured Full-length headroom reserve, so the configured block-swap margin was not applied. Full-length qualification is still required."
        if grid_minimum_margin_waived else
        "Selected with a safety margin above the short fresh-process capacity boundary; full-length qualification is still required.")
    profile["limitations"] = [
        selection_limit,
        "Run calibration on a machine without unrelated GPU processes or prior workload state.",
        "RAM and swap requirements reproduce the tested machine capacity; they are not proven minima.",
        "Visual review remains pending.",
    ]
    if lower_boundary_outcome == "mixed-pass-gpu-oom":
        profile["limitations"].insert(
            1,
            "The candidate directly below the raw pass boundary produced both a pass and a GPU OOM; selection relies on the safety margin and three fresh-process passes, not a deterministic failure boundary.")
    elif lower_boundary_outcome == "confirmed-oom-safety-jump":
        profile["limitations"].insert(
            1,
            "Exact raw-boundary probing was intentionally skipped after a confirmed GPU OOM; selection is based on the earliest possible raw pass, the configured safety margin, and three fresh-process passes.")
    destination = run_dir / "profiles" / "recommended" / (profile["id"] + ".yaml")
    write_json(destination, profile)
    return {
        "id": profile["id"],
        "path": destination,
        "directory": destination.parent,
        "sha256": sha256_file(destination),
        "profile": profile,
        "blocks_to_swap": candidate["blocks_to_swap"],
    }


def cmd_calibrate(args):
    # A named local profile may select a different quantization recipe. Keep
    # that identity through every catalog reload and model preflight, scoped
    # to this command only (never alter published scenario files).
    base = getattr(args, "base_profile_json", None)
    token = None
    if base is not None:
        catalog, _lock, scenario_set = load_catalog(args.root)
        catalog._validate_profile(base, "calibration base profile")
        scenarios = selected_scenarios(scenario_set, args.scenario)
        if len(scenarios) != 1:
            raise ValueError("a local base profile requires exactly one scenario")
        scenario = scenarios[0]
        expected = catalog.recipe_for(catalog.profiles[scenario["profile"]])
        selected = catalog.recipe_for(base)
        if any(selected.get(key) != expected.get(key)
               for key in ("model_family", "resolution")):
            raise ValueError("calibration base recipe must match scenario family/resolution")
        args._custom_calibration_recipe = selected["id"] != expected["id"]
        if set(selected.get("pipeline_stages") or []) <= set(MAIN_STAGES):
            args.main_only = True
        token = _CALIBRATION_PROFILE.set((scenario["profile"], base))
    try:
        return _cmd_calibrate(args)
    finally:
        if token is not None:
            _CALIBRATION_PROFILE.reset(token)


def _cmd_calibrate(args):
    root, workspace = args.root.resolve(), args.workspace.resolve()
    run_dir, metadata = begin_run(root, workspace, "calibration", args.run_id)
    _catalog, _lock, scenario_set = load_catalog(root)
    scenarios = selected_scenarios(scenario_set, args.scenario)
    evidence = input_evidence(root, scenario_set)
    if args.dry_run:
        gpu = {"name": "unmeasured-gpu", "memory.total": "1024", "uuid": "unknown"}
    else:
        try:
            gpus = require_gpu_context(metadata, args.allow_unknown_image)
            gpu = gpus[0]
        except Exception as exc:
            update_metadata(run_dir, status="failed", finished_at=utc_now(), error=str(exc))
            raise
    search_gpu = dict(gpu)
    ballast_mib = ((getattr(args, "base_profile_json", None) or {}).get(
        "settings") or {}).get("vram_ballast_mib", 0)
    if ballast_mib:
        if ballast_mib not in (4096, 8192):
            raise ValueError("unsupported VRAM ballast")
        search_gpu["memory.total"] = str(float(gpu["memory.total"]) - ballast_mib)
        if float(search_gpu["memory.total"]) <= 0 and not args.dry_run:
            raise ValueError("ballast exceeds physical VRAM")
        if args.dry_run:
            search_gpu["memory.total"] = "12288" if ballast_mib == 4096 else "8192"
    candidates = materialize_candidate_profiles(
        root, run_dir, scenarios, search_gpu, scenario_set,
        base_profile=getattr(args, "base_profile_json", None))
    search_table = load_search_start_table(root, scenario_set)
    search_starts = {
        scenario["id"]: select_search_start(
            search_table, scenario["id"], search_gpu,
            [item["blocks_to_swap"] for item in candidates[scenario["id"]]])
        for scenario in scenarios
    }
    main_only = getattr(args, "main_only", False)
    search_mode = getattr(args, "search_mode", "boundary")
    if search_mode not in {"boundary", "fast"}:
        raise ValueError("unknown calibration search mode: %s" % search_mode)
    requested_scope = pipeline_scope(main_only)
    metadata["pipeline_scope"] = requested_scope
    update_metadata(run_dir, scenario_set=scenario_set["id"], inputs=evidence,
                    scenarios=[item["id"] for item in scenarios], gpu=gpu,
                    pipeline_scope=requested_scope,
                    calibration_search={
                        "mode": search_mode,
                        "start_table": search_table["id"],
                        "starts": search_starts,
                        "selection_safety_margin_blocks": int(
                            scenario_set["calibration"].get(
                                "selection_safety_margin_blocks", 0)),
                    })
    if args.dry_run:
        update_metadata(run_dir, status="dry-run", finished_at=utc_now())
        print(run_dir)
        return 0

    models, _scenario_set = selected_models(
        root, False, scenarios=scenarios, scope=requested_scope)
    try:
        verify_selected_models(
            models, models_dir_for_args(args, workspace),
            run_dir / "model-preflight.json")
        verify_runtime_disk(
            workspace, run_dir / "runtime-disk-preflight.json")
    except Exception as exc:
        update_metadata(run_dir, status="failed", finished_at=utc_now(), error=str(exc))
        raise
    activate_output(root, run_dir)
    write_json(run_dir / "gpu-start.json", gpu_snapshot())
    stable_passes = int(scenario_set["calibration"].get("stable_passes", 3))
    boundary_failures = int(
        scenario_set["calibration"].get("boundary_failure_passes", 2))
    safety_margin = int(
        scenario_set["calibration"].get("selection_safety_margin_blocks", 0))
    zero_swap_block_table = (None if getattr(args, "_custom_calibration_recipe", False)
                            else load_zero_swap_block_margin_table(root, scenario_set))
    downward_probe_step = int(
        scenario_set["calibration"].get("downward_probe_step", 4))
    late_oom_seconds = _number(
        scenario_set["calibration"].get("late_oom_seconds", 300))
    if (stable_passes < 1 or boundary_failures < 1 or safety_margin < 0
            or downward_probe_step < 1
            or late_oom_seconds is None or late_oom_seconds <= 0):
        raise ValueError("invalid calibration search settings")
    monitor = ResourceMonitor(
        run_dir / "resource-telemetry.csv", args.telemetry_seconds, workspace)
    monitor.start()
    records = []
    promoted = {}
    sequence = 0
    terminal_status = "inconclusive"
    try:
        for scenario in scenarios:
            grid = candidates[scenario["id"]]
            by_blocks = {item["blocks_to_swap"]: index
                         for index, item in enumerate(grid)}
            scenario_records = []

            def run_candidate(index, label, reason, replicate=1):
                nonlocal sequence
                candidate = grid[index]
                sequence += 1
                record = fresh_attempt(
                    root, run_dir, scenario_set, scenario, candidate,
                    attempt_id(metadata, scenario, candidate, sequence, label),
                    "short", replicate, "calibration", reason, monitor, args)
                records.append(record)
                scenario_records.append(record)
                write_json(run_dir / "runs.json", records)
                if (record["status"] != "passed"
                        and record["failure_category"] != "gpu-oom"):
                    raise RuntimeError(
                        "%s calibration stopped on %s, not a GPU capacity result"
                        % (scenario["id"], record["failure_category"]))
                return record

            def is_late_oom(record):
                return bool(
                    record["failure_category"] == "gpu-oom"
                    and (_number(record.get("elapsed_seconds")) or 0)
                    >= late_oom_seconds)

            def candidate_at_or_above(blocks):
                return next(
                    (index for index, item in enumerate(grid)
                     if item["blocks_to_swap"] >= blocks), None)

            start_blocks = search_starts[scenario["id"]]["blocks_to_swap"]
            start = by_blocks[start_blocks]
            seed = run_candidate(
                start, "seed", "evidence-seeded-oom-first-search")
            best = None
            late_oom_index = None

            if search_mode == "boundary":
                if seed["status"] == "passed":
                    # Find a failing lower bracket quickly, beginning with the
                    # adjacent value so a nearby boundary costs one attempt.
                    # Then fill only the bracket from its failing side.  This
                    # preserves every value near the boundary without walking
                    # the entire candidate grid one block at a time.
                    best = upper_pass = start
                    step = 1
                    while upper_pass > 0:
                        probe = max(0, upper_pass - step)
                        record = run_candidate(
                            probe, "down", "boundary-downward-bracket-search")
                        if record["status"] == "passed":
                            best = upper_pass = probe
                            step *= 2
                            continue
                        for index in range(probe + 1, upper_pass):
                            record = run_candidate(
                                index, "up", "boundary-ascending-fill-search")
                            if record["status"] == "passed":
                                best = index
                                break
                        break
                else:
                    # A failed starting hint is normally already close to the
                    # useful boundary. Ascending one block at a time records
                    # the first passing value and avoids skipping late OOMs.
                    for index in range(start + 1, len(grid)):
                        record = run_candidate(
                            index, "up", "boundary-one-block-ascending-search")
                        if record["status"] == "passed":
                            best = index
                            break
            elif seed["status"] == "passed":
                # Fast mode retains the original widening/jump search. It is
                # useful when runtime matters more than an exact boundary.
                best = upper_pass = start
                step = downward_probe_step
                while upper_pass > 0:
                    probe = max(0, upper_pass - step)
                    record = run_candidate(
                        probe, "down", "downward-pass-bracket-search")
                    if record["status"] == "passed":
                        best = upper_pass = probe
                        step *= 2
                        continue

                    best = upper_pass
                    if is_late_oom(record):
                        late_oom_index = probe
                        break
                    index = probe + 1
                    while index < upper_pass:
                        record = run_candidate(
                            index, "up", "oom-first-ascending-search")
                        if record["status"] == "passed":
                            best = index
                            break
                        if is_late_oom(record):
                            late_oom_index = index
                            break
                        index += 1
                    break
            else:
                # Fast mode jumps directly above an observed OOM plus the
                # safety margin, then spends time on stable-pass evidence.
                late_oom_index = start

            if best is None and late_oom_index is None:
                terminal_status = "no-fit"
                raise RuntimeError("%s calibration no-fit across the candidate grid"
                                   % scenario["id"])

            zero_swap_policy = None
            if late_oom_index is not None:
                # Exact-boundary probes are expensive after a late OOM.  The
                # earliest possible raw pass is one block above the observed
                # OOM, so apply the configured margin above that baseline and
                # spend the remaining time on stable-pass evidence instead.
                raw_candidate = None
                raw_lower_candidate = grid[late_oom_index]
                raw_failure_candidate = None
                lower_boundary_outcome = "confirmed-oom-safety-jump"
                safety_baseline_blocks = (
                    raw_lower_candidate["blocks_to_swap"] + 1)
                target_blocks = safety_baseline_blocks + safety_margin
            else:
                # First prove the raw short-process capacity boundary.  This
                # is evidence, not the value promoted for later short/full runs.
                raw = best
                raw_lower_candidate = None
                raw_failure_candidate = None
                lower_boundary_outcome = "grid-minimum"
                while True:
                    lower = raw - 1
                    if lower < 0:
                        break
                    lower_blocks = grid[lower]["blocks_to_swap"]
                    boundary = [
                        record for record in scenario_records
                        if record["blocks_to_swap"] == lower_blocks
                        and (record["status"] == "passed"
                             or record["failure_category"] == "gpu-oom")]
                    while len(boundary) < boundary_failures:
                        replicate = len(boundary) + 1
                        record = run_candidate(
                            lower, "boundary%02d" % replicate,
                            "lower-boundary-confirmation", replicate)
                        boundary.append(record)
                    passed_lower = [
                        r for r in boundary if r["status"] == "passed"]
                    oom_lower = [r for r in boundary
                                 if r["failure_category"] == "gpu-oom"]
                    if len(oom_lower) == len(boundary):
                        raw_lower_candidate = grid[lower]
                        raw_failure_candidate = grid[lower]
                        lower_boundary_outcome = "gpu-oom-reproduced"
                        break
                    if len(passed_lower) == len(boundary):
                        raw = lower
                        continue
                    # Preserve a stochastic edge and rely on the margin plus
                    # stable passes instead of calling it a failure boundary.
                    raw_lower_candidate = grid[lower]
                    lower_boundary_outcome = "mixed-pass-gpu-oom"
                    break

                raw_candidate = grid[raw]
                safety_baseline_blocks = raw_candidate["blocks_to_swap"]
                # Zero is a physical lower bound, not a razor-thin boundary
                # above a failing candidate.  Confirm it three times directly;
                # adding swap here would only slow a GPU already shown to need
                # no model offload.  Any OOM during confirmation cancels this
                # exception and restores the normal OOM+margin jump.
                # Always complete the three bs=0 confirmations first.  Their
                # worst peak, rather than a Full-uplift estimate, decides the
                # eventual +0..+3 setting below.
                waive_margin_at_grid_minimum = bool(
                    safety_baseline_blocks == 0 and zero_swap_block_table is not None
                    and lower_boundary_outcome == "grid-minimum")
                if safety_baseline_blocks == 0 and zero_swap_block_table is not None:
                    zero_peak, zero_headroom = short_gpu_headroom_mib(
                        gpu, scenario_records, raw_candidate["id"])
                    zero_swap_policy = zero_swap_block_margin_policy(
                        zero_swap_block_table, scenario["id"], gpu, safety_margin)
                    zero_swap_policy.update({
                        "short_peak_mib": zero_peak,
                        "short_headroom_mib": zero_headroom,
                        "waived_margin": None,
                        "reason": "awaiting-three-zero-short-passes",
                    })
                target_blocks = (
                    safety_baseline_blocks if waive_margin_at_grid_minimum
                    else safety_baseline_blocks + safety_margin)

            if late_oom_index is not None:
                waive_margin_at_grid_minimum = False

            selected = candidate_at_or_above(target_blocks)
            if selected is None:
                terminal_status = "no-fit"
                raise RuntimeError(
                    "%s calibration cannot apply safety margin=%d above baseline=%d"
                    % (scenario["id"], safety_margin, safety_baseline_blocks))

            # Confirm the safety-adjusted value, not the razor-thin raw
            # boundary.  A stochastic GPU OOM moves selection farther toward
            # safety without changing the recorded raw boundary.
            while True:
                candidate = grid[selected]
                confirmations = [
                    record for record in scenario_records
                    if record["blocks_to_swap"] == candidate["blocks_to_swap"]
                    and record["status"] == "passed"
                    and (lower_boundary_outcome != "confirmed-oom-safety-jump"
                         or record["selection_reason"]
                         == "confirmed-oom-safety-jump-confirmation")]
                moved_up = False
                while len(confirmations) < stable_passes:
                    replicate = len(confirmations) + 1
                    confirmation_reason = (
                        "confirmed-oom-safety-jump-confirmation"
                        if lower_boundary_outcome == "confirmed-oom-safety-jump"
                        else "grid-minimum-zero-stable-pass-confirmation"
                        if waive_margin_at_grid_minimum
                        else "safety-margin-stable-pass-confirmation")
                    record = run_candidate(
                        selected, "stable%02d" % replicate,
                        confirmation_reason, replicate)
                    if record["status"] == "passed":
                        confirmations.append(record)
                        continue
                    failed_blocks = candidate["blocks_to_swap"]
                    if waive_margin_at_grid_minimum:
                        raw_candidate = None
                        raw_lower_candidate = candidate
                        raw_failure_candidate = None
                        lower_boundary_outcome = "confirmed-oom-safety-jump"
                        safety_baseline_blocks = failed_blocks + 1
                        next_blocks = safety_baseline_blocks + safety_margin
                        waive_margin_at_grid_minimum = False
                    elif lower_boundary_outcome == "confirmed-oom-safety-jump":
                        raw_lower_candidate = candidate
                        safety_baseline_blocks = failed_blocks + 1
                        next_blocks = safety_baseline_blocks + safety_margin
                    else:
                        next_blocks = failed_blocks + 1
                    next_selected = candidate_at_or_above(next_blocks)
                    if next_selected is None:
                        terminal_status = "no-fit"
                        raise RuntimeError(
                            "%s calibration no-fit after safety-margin confirmation gpu-oom"
                            % scenario["id"])
                    selected = next_selected
                    moved_up = True
                    break
                if not moved_up and waive_margin_at_grid_minimum:
                    zero_peak, zero_headroom = short_gpu_headroom_mib(
                        gpu, scenario_records, candidate["id"])
                    per_block = zero_swap_policy["vram_per_block_mib"]
                    available_blocks = (int(zero_headroom // per_block)
                                        if zero_headroom is not None else 0)
                    added_blocks = max(
                        0, safety_margin - min(safety_margin, available_blocks))
                    zero_swap_policy.update({
                        "short_peak_mib": zero_peak,
                        "short_headroom_mib": zero_headroom,
                        "available_headroom_blocks": available_blocks,
                        "selected_blocks_to_swap": added_blocks,
                        "waived_margin": added_blocks == 0,
                        "reason": "three-zero-short-passes-block-headroom",
                    })
                    if added_blocks:
                        selected = candidate_at_or_above(added_blocks)
                        if selected is None:
                            terminal_status = "no-fit"
                            raise RuntimeError(
                                "%s calibration cannot apply zero-swap block margin"
                                % scenario["id"])
                        waive_margin_at_grid_minimum = False
                    # The selected +1..+3 value is intentionally sent straight
                    # to Full qualification.  The three Short confirmations
                    # belong to bs=0, whose measured headroom chose it.
                    break
                if not moved_up:
                    break

            promoted[scenario["id"]] = promote_profile(
                run_dir, scenario, grid[selected], gpu, records,
                raw_candidate, raw_lower_candidate, raw_failure_candidate,
                lower_boundary_outcome, safety_margin,
                safety_baseline_blocks, zero_swap_policy)

        profile_set = {
            "schema_version": 1,
            "scenario_set": scenario_set["id"],
            "pipeline_scope": requested_scope,
            "search_mode": search_mode,
            "calibration_run_id": metadata["run_id"],
            "source_revision": metadata["source_revision"],
            "image_reference": metadata["image_reference"],
            "gpu": gpu,
            "profiles": {},
        }
        for scenario_id, entry in promoted.items():
            profile_set["profiles"][scenario_id] = {
                "id": entry["id"],
                "path": str(entry["path"].relative_to(run_dir)),
                "sha256": entry["sha256"],
                "blocks_to_swap": entry["blocks_to_swap"],
                "raw_short_pass_boundary_blocks_to_swap": (
                    entry["profile"]["evidence"][
                        "raw_short_pass_boundary_blocks_to_swap"]),
                "raw_short_failure_boundary_blocks_to_swap": (
                    entry["profile"]["evidence"][
                        "raw_short_failure_boundary_blocks_to_swap"]),
                "raw_short_lower_boundary_blocks_to_swap": (
                    entry["profile"]["evidence"][
                        "raw_short_lower_boundary_blocks_to_swap"]),
                "raw_short_lower_boundary_outcome": (
                    entry["profile"]["evidence"][
                        "raw_short_lower_boundary_outcome"]),
                "selection_safety_margin_blocks": (
                    entry["profile"]["evidence"][
                        "selection_safety_margin_blocks"]),
                "effective_safety_margin_blocks": (
                    entry["profile"]["evidence"][
                        "effective_safety_margin_blocks"]),
                "selection_safety_margin_waived_at_grid_minimum": (
                    entry["profile"]["evidence"][
                        "selection_safety_margin_waived_at_grid_minimum"]),
                "zero_swap_full_headroom_policy": entry["profile"]["evidence"].get(
                    "zero_swap_full_headroom_policy"),
                "qualification": entry["profile"]["evidence"][
                    "calibration_state"],
            }
        write_json(run_dir / "profile-set.json", profile_set)
        write_json(run_dir / "gpu-finish.json", gpu_snapshot())
        update_metadata(run_dir, status="completed", finished_at=utc_now(),
                        attempts=len(records), profile_set="profile-set.json",
                        profile_set_sha256=sha256_file(run_dir / "profile-set.json"))
    except Exception as exc:
        write_json(run_dir / "gpu-finish.json", gpu_snapshot())
        update_metadata(run_dir, status=terminal_status, finished_at=utc_now(),
                        error=str(exc), attempts=len(records))
        raise
    finally:
        monitor.close()
    print(run_dir)
    return 0


def add_gpu_options(parser, scenario_required=False):
    parser.add_argument(
        "--scenario", action="append", required=scenario_required,
        help="limit to one scenario id")
    parser.add_argument("--telemetry-seconds", type=positive_int, default=1)
    parser.add_argument("--comfy-timeout-seconds", type=positive_int, default=300)
    parser.add_argument("--attempt-timeout-seconds", type=positive_int, default=28800)
    parser.add_argument("--allow-unknown-image", action="store_true",
                        help="local test only: allow missing immutable image digest")
    parser.add_argument(
        "--short-repeats", type=positive_int,
        help="override the scenario's Short measurement repeat count")
    parser.add_argument(
        "--main-only", action="store_true",
        help="run only the recipe's main generation stage; default is full E2E")
    parser.add_argument("--dry-run", action="store_true")


def add_search_mode_option(parser):
    parser.add_argument(
        "--search-mode", choices=("boundary", "fast"), default="boundary",
        help=("boundary finds and confirms the adjacent fail/pass edge; "
              "fast retains the safety-jump search (default: %(default)s)"))


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    common.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    common.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    calibrate = sub.add_parser(
        "calibrate", parents=[common], help="find a stable blocks_to_swap profile")
    calibrate.add_argument("--run-id")
    add_search_mode_option(calibrate)
    calibrate.add_argument(
        "--base-profile-json", type=json.loads,
        help="named base profile snapshot supplied by the local calibration CLI")
    add_gpu_options(calibrate)
    calibrate.set_defaults(func=cmd_calibrate)
    return parser


def main():
    args = parser().parse_args()
    try:
        return args.func(args)
    except (RuntimeError, OSError, ValueError) as exc:
        print("benchmark error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
