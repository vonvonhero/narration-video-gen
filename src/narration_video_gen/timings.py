"""Wall-clock times measured on this machine.

A published profile carries one timing measured on the hardware that qualified
it. That number is real, but it was not measured on the reader's machine, so
the first run on a new machine can only be estimated from someone else's.

Once this machine has finished a run, there is something better: its own time.
This module keeps those observations and turns them into an estimate for a
different narration length.

Records live outside the repository, under the same private configuration root
as the calibration state, and never leave the machine.
"""

from __future__ import annotations

import json
import os
import time

from . import detect
from .calibration_state import _digest, _normalise_name, config_root

SCHEMA_VERSION = 1
# Keep enough history to notice a machine getting slower, and no more.
MAX_OBSERVATIONS = 8


def timings_root():
    return config_root() / "timings"


def machine_identity(env):
    """Identify the machine, and the GPUs it had, when a timing was measured.

    The calibration state deliberately refuses machines with more than one GPU,
    because a block-swap setting only means something for the card it was found
    on. A wall-clock time is different: it is simply how long this box took, so
    a second GPU should not disable the feature.

    The generation service is given every GPU, and CUDA decides which one it
    uses. Rather than guess that, the whole visible set identifies the record:
    a timing measured before a card was added or removed is not reused
    afterwards.
    """
    gpus = env.get("gpus") or []
    if not gpus:
        return None
    cards = sorted(
        gpu.get("uuid") or "%s:%s" % (_normalise_name(gpu.get("name")),
                                      gpu.get("vram_mib"))
        for gpu in gpus)
    return {"gpus": cards,
            "fingerprint": _digest({"platform": env.get("platform"), "gpus": cards})}


def _record_path(machine):
    return timings_root() / (machine["fingerprint"] + ".json")


def workload_key(profile, recipe, recipe_options=None, pipeline=None, runtime=None):
    """Identify the exact work a timing describes.

    A recipe id alone is not enough. The same recipe runs a different amount of
    work depending on the options chosen (a disabled face detailer removes a
    whole stage) and on the profile settings a local calibration selected. The
    recipe's own contents and the image it runs in can change while the id stays
    the same, and a timing measured before such a change no longer describes the
    same work, so both are folded in as well.
    """
    return _digest({
        "profile": profile.get("_base_profile_id", profile["id"]),
        "settings": dict(profile.get("settings") or {}),
        "recipe": recipe["id"],
        "recipe_contents": recipe,
        "recipe_options": sorted(recipe_options or []),
        "pipeline": list(pipeline or []),
        "runtime": runtime,
    })


def _load(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def record(env, profile, recipe, basis_seconds, wall_clock_seconds,
           recipe_options=None, pipeline=None, runtime=None):
    """Store one finished run. Returns the stored observation, or None."""
    machine = machine_identity(env)
    if not machine or not basis_seconds or not wall_clock_seconds:
        return None
    key = workload_key(profile, recipe, recipe_options, pipeline, runtime)
    observation = {
        "basis_seconds": round(float(basis_seconds), 2),
        "wall_clock_seconds": round(float(wall_clock_seconds), 1),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = _record_path(machine)
    payload = _load(path)
    payload["schema_version"] = SCHEMA_VERSION
    entries = payload.setdefault("profiles", {}).setdefault(key, [])
    entries.append(observation)
    del entries[:-MAX_OBSERVATIONS]
    try:
        _write(path, payload)
    except OSError:
        # A finished video is the result that matters. Losing the local ETA
        # history because the config directory is unwritable is not a failure
        # worth reporting as one.
        return None
    return observation


def observations(env, profile, recipe, recipe_options=None, pipeline=None,
                 runtime=None):
    """Return this machine's finished runs for exactly this workload."""
    machine = machine_identity(env)
    if not machine:
        return []
    key = workload_key(profile, recipe, recipe_options, pipeline, runtime)
    return list((_load(_record_path(machine)).get("profiles") or {}).get(key) or [])


def _shape(reference_estimate, target_seconds, basis_seconds):
    """How much longer a ``target_seconds`` run takes than a ``basis_seconds`` one.

    Wall-clock time does not grow with narration length alone: loading the
    model, decoding and interpolating cost the same either way. The published
    profile already encodes how the total grows, so its shape is reused here and
    only the absolute level comes from this machine.
    """
    if not callable(reference_estimate):
        return None
    target = reference_estimate(target_seconds)
    basis = reference_estimate(basis_seconds)
    if not target or not basis:
        return None
    if not target.get("known") or not basis.get("known"):
        return None
    if not basis.get("minutes"):
        return None
    return target["minutes"] / basis["minutes"]


def estimate(env, profile, recipe, seconds, reference_estimate=None,
             recipe_options=None, pipeline=None, runtime=None):
    """Estimate this machine's wall-clock time for a run of ``seconds``.

    Returns None when this machine has not finished a comparable run yet, so the
    caller can fall back to the published reference.
    """
    history = observations(env, profile, recipe, recipe_options, pipeline, runtime)
    if not history or not seconds:
        return None

    scaled = []
    for entry in history:
        basis = entry.get("basis_seconds")
        measured = entry.get("wall_clock_seconds")
        if not basis or not measured:
            continue
        ratio = _shape(reference_estimate, seconds, basis)
        if ratio is None:
            # Without a published shape to borrow, only a run of the same length
            # can be trusted; extrapolating from a five second test would read
            # as precise while being invented.
            if abs(basis - seconds) > 0.5:
                continue
            ratio = 1.0
        scaled.append(measured * ratio)

    if not scaled:
        return None

    low, high = min(scaled), max(scaled)
    exact = any(abs((entry.get("basis_seconds") or 0) - seconds) <= 0.5
                for entry in history)
    return {
        "known": True,
        "source": "local",
        "minutes": round(sum(scaled) / len(scaled) / 60.0, 2),
        "low_minutes": round(low / 60.0, 2),
        "high_minutes": round(high / 60.0, 2),
        "extrapolated": not exact,
        "samples": len(scaled),
    }
