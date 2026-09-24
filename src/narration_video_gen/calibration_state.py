"""Persistent, machine-bound profiles produced by local block-swap calibration.

Published profiles remain immutable defaults.  This module stores a calibrated
profile outside the checkout and only returns it when both the physical GPU and
the complete saved plan identity match.  The checkout can therefore be updated
without losing a user's local capacity result, while another GPU never inherits
that result accidentally.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path


SCHEMA_VERSION = 1
# A capacity-simulated profile reserves this much VRAM so a larger GPU behaves
# like a smaller one. Local calibration measures the real GPU, so these
# settings are never passed to the search or kept in its result.
BALLAST_SETTINGS = ("vram_ballast_mib", "vram_ballast_device_index")


def config_root():
    override = os.environ.get("NVG_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "narration-video-gen"
    return Path.home() / ".config" / "narration-video-gen"


def calibration_root():
    return config_root() / "calibrations"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _normalise_name(value):
    return "".join(character for character in str(value or "").lower()
                   if character.isalnum())


def machine_identity(env):
    gpus = env.get("gpus") or []
    if len(gpus) != 1:
        return None
    gpu = gpus[0]
    identity = {
        "platform": env.get("platform"),
        "gpu_uuid": gpu.get("uuid"),
        "gpu_name": gpu.get("name"),
        "gpu_vram_mib": gpu.get("vram_mib"),
    }
    # UUID is stable across OS installs.  Older nvidia-smi versions may not
    # expose it through detect, so keep a strict name+capacity fallback.
    key = ({"platform": identity["platform"], "gpu_uuid": identity["gpu_uuid"]}
           if identity["gpu_uuid"] else {
               "platform": identity["platform"],
               "gpu_name": _normalise_name(identity["gpu_name"]),
               "gpu_vram_mib": identity["gpu_vram_mib"],
           })
    identity["fingerprint"] = _digest(key)
    return identity


def plan_identity(profile, recipe, recipe_options):
    value = {
        "base_profile": profile.get("_base_profile_id", profile["id"]),
        "recipe": recipe["id"],
        "model_family": recipe.get("model_family"),
        "resolution": list(recipe.get("resolution") or []),
        "recipe_options": sorted(recipe_options or []),
    }
    value["fingerprint"] = _digest(value)
    return value


def scenario_for(recipe):
    family = recipe.get("model_family")
    resolution = recipe.get("resolution") or []
    if len(resolution) != 2:
        return None
    prefix = {"wan21-infinitetalk": "wan21", "wan22-s2v": "wan22"}.get(family)
    return "%s-%dp" % (prefix, resolution[1]) if prefix else None


def _record_path(machine, plan):
    return (calibration_root() / machine["fingerprint"] /
            (plan["fingerprint"] + ".json"))


def _profile_path(record_path):
    return record_path.with_suffix(".profile.yaml")


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise OSError("calibration directory must be private and owned by this user")


def _atomic_json(path, payload):
    _private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".calibration-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def _safe_load(path):
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def resolve(profile, recipe, recipe_options, env):
    """Return ``(effective_profile, metadata)`` for an exact local match."""
    machine = machine_identity(env)
    if machine is None:
        return profile, None
    plan = plan_identity(profile, recipe, recipe_options)
    path = _record_path(machine, plan)
    record = _safe_load(path)
    if (not record or record.get("schema_version") != SCHEMA_VERSION
            or not record.get("enabled", True)
            or (record.get("machine") or {}).get("fingerprint")
            != machine["fingerprint"] or record.get("plan") != plan):
        return profile, None
    calibrated = record.get("profile")
    if not isinstance(calibrated, dict) or calibrated.get("recipe") != profile.get("recipe"):
        return profile, None
    expected = record.get("profile_sha256")
    if expected != _digest(calibrated):
        return profile, None
    materialized = _safe_load(_profile_path(path))
    if materialized is None or _digest(materialized) != expected:
        return profile, None
    effective = json.loads(json.dumps(calibrated))
    effective["_path"] = str(_profile_path(path))
    effective["_base_profile_id"] = plan["base_profile"]
    effective["_local_calibration"] = {
        "path": str(path),
        "saved_at": record.get("saved_at"),
        "source_run": record.get("source_run"),
        "scenario": record.get("scenario"),
    }
    return effective, effective["_local_calibration"]


def import_profile_set(profile_set_path, base_profile, recipe, recipe_options, env):
    """Validate and persist one scenario from a benchmark calibration result."""
    profile_set_path = Path(profile_set_path).expanduser().resolve()
    payload = json.loads(profile_set_path.read_text(encoding="utf-8"))
    scenario = scenario_for(recipe)
    entry = (payload.get("profiles") or {}).get(scenario)
    if not entry:
        raise ValueError("profile set has no result for %s" % scenario)
    source_profile = (profile_set_path.parent / entry["path"]).resolve()
    if profile_set_path.parent not in source_profile.parents:
        raise ValueError("calibrated profile path escapes its run directory")
    raw = source_profile.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
        raise ValueError("calibrated profile SHA-256 does not match profile-set.json")
    measured_profile = json.loads(raw.decode("utf-8"))
    if measured_profile.get("recipe") != base_profile.get("recipe"):
        raise ValueError("calibrated profile recipe does not match the selected plan")
    blocks = (measured_profile.get("settings") or {}).get("blocks_to_swap")
    if not isinstance(blocks, int) or isinstance(blocks, bool) or not 0 <= blocks <= 40:
        raise ValueError("calibrated blocks_to_swap must be between 0 and 40")

    machine = machine_identity(env)
    if machine is None:
        raise ValueError("calibration requires exactly one visible GPU")
    measured = payload.get("gpu") or {}
    measured_uuid = measured.get("uuid")
    if measured_uuid and machine.get("gpu_uuid") and measured_uuid != machine["gpu_uuid"]:
        raise ValueError("calibration was produced by a different GPU UUID")
    measured_name = measured.get("name")
    measured_mib = measured.get("memory.total")
    try:
        measured_mib = int(float(measured_mib))
    except (TypeError, ValueError):
        measured_mib = None
    if (not measured_uuid or not machine.get("gpu_uuid")) and (
            _normalise_name(measured_name) != _normalise_name(machine.get("gpu_name"))
            or measured_mib != machine.get("gpu_vram_mib")):
        raise ValueError("calibration GPU name/capacity does not match this machine")

    plan = plan_identity(base_profile, recipe, recipe_options)
    local_id = "local-%s-%s-%s" % (
        re.sub(r"[^a-z0-9-]+", "-", plan["base_profile"].lower()),
        machine["fingerprint"][:8], plan["fingerprint"][:8])
    # Only the main block swap is searched here. Keep every explicit setting
    # from the selected public/custom profile. When the selected plan includes
    # Face Detailer, also retain independently-qualified face settings that the
    # base profile left unspecified; otherwise the persisted plan would not
    # reproduce the pipeline that calibration actually exercised.
    calibrated = json.loads(json.dumps(base_profile))
    calibrated.pop("_path", None)
    calibrated.pop("_local_calibration", None)
    calibrated["id"] = local_id
    calibrated_settings = calibrated.setdefault("settings", {})
    for key in BALLAST_SETTINGS:
        calibrated_settings.pop(key, None)
    measured_settings = measured_profile.get("settings") or {}
    calibrated_settings["blocks_to_swap"] = blocks
    if "face-detailer-on" in (recipe_options or []):
        for key in ("face_detailer_size", "face_detailer_blocks_to_swap"):
            if key not in calibrated_settings and measured_settings.get(key) is not None:
                calibrated_settings[key] = measured_settings[key]
    measured_requires = measured_profile.get("requires") or {}
    for key in ("host_ram_gib_min", "swap_gib_min"):
        if measured_requires.get(key) is not None:
            calibrated.setdefault("requires", {})[key] = measured_requires[key]
    if calibrated.get("platform") == "windows-wsl2":
        for requirement, setting in (("host_ram_gib_min", "wsl_ram_gib"),
                                     ("swap_gib_min", "wsl_swap_gib")):
            value = (calibrated.get("requires") or {}).get(requirement)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                calibrated_settings[setting] = max(
                    calibrated_settings.get(setting, 0), math.ceil(value))
    calibrated["status"] = "experimental"
    calibrated["evidence"] = json.loads(json.dumps(
        measured_profile.get("evidence") or calibrated.get("evidence") or {}))
    calibrated["evidence"]["base_profile"] = plan["base_profile"]
    calibrated["evidence"]["plan_fingerprint"] = plan["fingerprint"]
    calibrated["summary"] = (
        "Machine-local calibration for %s with blocks_to_swap=%d."
        % (plan["base_profile"], blocks))
    calibrated["limitations"] = list(measured_profile.get("limitations") or [])
    if calibrated["evidence"].get("duration_class") != "full":
        calibrated["limitations"] = list(dict.fromkeys([
            *calibrated["limitations"],
            "This machine-local block-swap value is short-calibrated; run and review a full-length output before treating it as full evidence.",
        ]))
    record = {
        "schema_version": SCHEMA_VERSION,
        "enabled": True,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine,
        "plan": plan,
        "scenario": scenario,
        "source_run": {
            "profile_set": str(profile_set_path),
            "calibration_run_id": payload.get("calibration_run_id"),
            "source_revision": payload.get("source_revision"),
            "image_reference": payload.get("image_reference"),
        },
        "profile": calibrated,
        "profile_sha256": _digest(calibrated),
    }
    path = _record_path(machine, plan)
    _atomic_json(path, record)
    _atomic_json(_profile_path(path), calibrated)
    return path, record


def records(env=None):
    root = calibration_root()
    if not root.is_dir():
        return []
    current_machine = machine_identity(env) if env else None
    wanted = current_machine["fingerprint"] if current_machine else None
    found = []
    for path in sorted(root.glob("*/*.json")):
        payload = _safe_load(path)
        if not payload or payload.get("schema_version") != SCHEMA_VERSION:
            continue
        if wanted and (payload.get("machine") or {}).get("fingerprint") != wanted:
            continue
        found.append({**payload, "path": str(path)})
    return found


def set_enabled(path, enabled):
    path = Path(path).expanduser().resolve()
    root = calibration_root().resolve()
    if root not in path.parents:
        raise ValueError("calibration record is outside the configured calibration directory")
    payload = _safe_load(path)
    if not payload:
        raise ValueError("calibration record is missing or unsafe")
    payload["enabled"] = bool(enabled)
    _atomic_json(path, payload)
    return payload
