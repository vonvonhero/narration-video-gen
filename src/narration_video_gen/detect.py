"""Host environment probe.

Everything here is read-only and uses the standard library plus a small set of
tools that are already present on any machine capable of running the pipeline
(``nvidia-smi``, ``docker``). Nothing is installed and nothing is written
outside the path the caller asks for.

The result is a plain dict so it can be serialised into ``env.json`` and fed to
the profile matcher, a human, or an agent without further interpretation.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

GIB = 1024 ** 3
SCHEMA_VERSION = 1


def _run(args, timeout=20):
    """Run a command and return stdout, or ``None`` if it is unavailable."""
    if shutil.which(args[0]) is None:
        return None
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def detect_platform():
    """Return ``linux``, ``windows-wsl2`` or ``unsupported``.

    WSL2 is detected from the kernel release string, which contains
    ``microsoft-standard-WSL2`` on every currently shipping WSL2 kernel, and
    from ``/proc/sys/kernel/osrelease`` as a fallback for custom kernels.
    """
    if platform.system() != "Linux":
        return "unsupported"
    markers = [platform.release().lower()]
    try:
        markers.append(Path("/proc/sys/kernel/osrelease").read_text().lower())
    except OSError:
        pass
    if any("microsoft" in m for m in markers):
        return "windows-wsl2"
    return "linux"


def detect_gpus():
    """Return a list of GPUs with VRAM in GiB, or an empty list if none is visible."""
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version,compute_cap,power.limit,uuid",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return []
    gpus = []
    for line in out.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 3:
            continue
        try:
            vram_mib = int(float(fields[1]))
        except ValueError:
            continue
        gpus.append({
            "name": fields[0],
            # nvidia-smi reports MiB; profiles are written in GiB.
            "vram_gib": round(vram_mib / 1024, 2),
            "vram_mib": vram_mib,
            "driver_version": fields[2],
            "compute_capability": fields[3] if len(fields) > 3 else None,
            "power_limit_w": _number_or_none(fields[4]) if len(fields) > 4 else None,
            "uuid": fields[5] if len(fields) > 5 and fields[5] else None,
        })
    return gpus


def _number_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _meminfo():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            number = rest.strip().split(" ")[0]
            if number.isdigit():
                values[key] = int(number) * 1024  # /proc/meminfo is in kB
    except OSError:
        pass
    return values


def detect_memory():
    """Return total RAM and swap as seen by this kernel.

    Under WSL2 these are the values from ``.wslconfig``, not the Windows host's
    physical numbers. ``windows_host`` below fills in the physical side.
    """
    info = _meminfo()
    return {
        "ram_gib": round(info.get("MemTotal", 0) / GIB, 2),
        "ram_available_gib": round(info.get("MemAvailable", 0) / GIB, 2),
        "swap_gib": round(info.get("SwapTotal", 0) / GIB, 2),
    }


def detect_windows_host():
    """Read physical Windows RAM from inside WSL2 via ``powershell.exe``.

    Windows Wan2.1 720p needs 48 GiB of *physical* RAM; a 32 GiB machine with a
    large ``.wslconfig`` allocation is not equivalent, so the matcher has to see
    the real number. Returns ``None`` when it cannot be determined.
    """
    out = _run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
    ], timeout=60)
    if not out:
        return None
    digits = re.search(r"\d+", out)
    if not digits:
        return None
    return {"physical_ram_gib": round(int(digits.group()) / GIB, 2)}


def detect_disk(path):
    """Return total/free space for the filesystem holding ``path``."""
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(str(target))
    return {
        "path": str(Path(path)),
        "total_gib": round(usage.total / GIB, 2),
        "free_gib": round(usage.free / GIB, 2),
    }


def detect_container_runtime():
    """Report whether Docker is usable and whether it can see the GPU."""
    version = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    info = {
        "docker_available": version is not None,
        "docker_server_version": version,
        "nvidia_runtime": False,
        "compose_v2": False,
    }
    if version is None:
        return info
    runtimes = _run(["docker", "info", "--format", "{{json .Runtimes}}"])
    if runtimes:
        try:
            info["nvidia_runtime"] = "nvidia" in json.loads(runtimes)
        except (ValueError, TypeError):
            info["nvidia_runtime"] = "nvidia" in runtimes
    info["compose_v2"] = _run(["docker", "compose", "version"]) is not None
    return info


def detect(models_dir=None, outputs_dir=None):
    """Collect the full environment record used by ``select`` and ``preflight``."""
    plat = detect_platform()
    gpus = detect_gpus()
    env = {
        "schema_version": SCHEMA_VERSION,
        "platform": plat,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "gpus": gpus,
        "memory": detect_memory(),
        "container": detect_container_runtime(),
        "disk": {},
        "warnings": [],
    }

    if models_dir:
        env["disk"]["models"] = detect_disk(models_dir)
    if outputs_dir:
        env["disk"]["outputs"] = detect_disk(outputs_dir)

    if plat == "windows-wsl2":
        host = detect_windows_host()
        if host:
            env["windows_host"] = host
        else:
            env["warnings"].append(
                "physical Windows RAM could not be read (powershell.exe unavailable); "
                "profiles that require it will be withheld"
            )

    if not gpus:
        env["warnings"].append("no NVIDIA GPU visible to this shell")
    if not env["container"]["docker_available"]:
        env["warnings"].append("docker is not available")
    elif not env["container"]["nvidia_runtime"]:
        env["warnings"].append("docker has no nvidia runtime registered")

    return env


def primary_gpu(env):
    """Return the GPU the matcher should size against (the largest VRAM)."""
    gpus = env.get("gpus") or []
    if not gpus:
        return None
    return max(gpus, key=lambda g: g.get("vram_gib", 0))
