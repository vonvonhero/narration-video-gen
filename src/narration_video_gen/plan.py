"""Turn a chosen profile into a concrete, reviewable plan.

Nothing here executes anything. The point is that the reader -- or an agent --
sees the disk, time and host-level cost of a run *before* committing to it,
because the full-length runs in this repository take between 20 minutes and
3.5 hours.
"""

from __future__ import annotations

import hashlib
import json
import subprocess

from .compat import load_yaml_file

GIB = 1024 ** 3


def load_models_lock(root):
    return load_yaml_file(root / "manifests" / "models.lock.yaml")


def required_models(root, recipe):
    """Resolve the model ids a recipe needs into full lock entries."""
    # A plan can disable an optional stage. Do not make that choice download
    # weights which no remaining stage can address.
    from .runner import MODEL_ROLES, STAGE_ROLES

    lock = load_models_lock(root)
    by_id = {entry["id"]: entry for entry in lock.get("models", [])}
    needed_roles = set()
    for stage in recipe.get("pipeline_stages") or []:
        needed_roles.update(STAGE_ROLES.get(stage, ()))
    resolved = []
    missing = []
    for model_id in recipe.get("models", []):
        roles = set(MODEL_ROLES.get(model_id, ()))
        if roles and not roles.intersection(needed_roles):
            continue
        if model_id in by_id:
            resolved.append(by_id[model_id])
        else:
            missing.append(model_id)
    if missing:
        raise ValueError("recipe %s references unknown model id(s): %s"
                         % (recipe["id"], ", ".join(missing)))
    return resolved


def download_plan(root, recipe, models_dir):
    """Report which model files are already present and what is still needed."""
    entries = []
    total_bytes = 0
    missing_bytes = 0
    for model in required_models(root, recipe):
        dest = models_dir / model["path"]
        present = dest.is_file() and dest.stat().st_size == model["bytes"]
        entries.append({
            "id": model["id"],
            "path": model["path"],
            "bytes": model["bytes"],
            "sha256": model["sha256"],
            "license": model.get("license"),
            "present": present,
        })
        total_bytes += model["bytes"]
        if not present:
            missing_bytes += model["bytes"]
    return {
        "models": entries,
        "total_bytes": total_bytes,
        "missing_bytes": missing_bytes,
        "total_gib": round(total_bytes / GIB, 2),
        "missing_gib": round(missing_bytes / GIB, 2),
    }


def runtime_build_identity(root):
    """Fingerprint only inputs that can change the locally built image."""
    def source_hashes(source_root):
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source_root.rglob("*"))
            if (path.is_file() and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"})
        }

    lock = load_yaml_file(root / "manifests" / "containers.lock.yaml")
    node_root = root / "docker" / "nvg_nodes"
    patch_root = root / "docker" / "patches"
    calibration_roots = [
        root / "bin", root / "src", root / "recipes", root / "profiles",
        root / "calibration",
    ]
    inputs = {
        "schema_version": lock.get("schema_version"),
        "base_image": lock.get("base_image"),
        "python_runtime": lock.get("python_runtime"),
        "wsl2_allocator_workaround": lock.get("wsl2_allocator_workaround"),
        "components": lock.get("components"),
        "python_extras": lock.get("python_extras"),
        "dockerfile_sha256": hashlib.sha256(
            (root / "docker" / "comfy.Dockerfile").read_bytes()).hexdigest(),
        "dockerignore_sha256": hashlib.sha256(
            (root / ".dockerignore").read_bytes()).hexdigest(),
        "repository_node_sources": source_hashes(node_root),
        "repository_patch_sources": source_hashes(patch_root),
        "calibration_cli_sources": {
            key: value
            for directory in calibration_roots
            for key, value in source_hashes(directory).items()
        },
        "calibration_assets": {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                root / "assets/characters/aoi/manifest.yaml",
                root / "assets/characters/aoi/images/aoi-portrait-angled-01.png",
                root / "assets/characters/aoi/audio/aoi-narration-take3.wav",
            )
            if path.is_file()
        },
        "calibration_models_lock_sha256": (
            hashlib.sha256(
                (root / "manifests/models.lock.yaml").read_bytes()).hexdigest()
            if (root / "manifests/models.lock.yaml").is_file() else None),
        "calibration_ballast_source_sha256": (
            hashlib.sha256((root / "scripts/hold-vram-ballast.py").read_bytes()).hexdigest()
            if (root / "scripts/hold-vram-ballast.py").is_file() else None),
    }
    canonical = json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def musetalk_build_identity(root):
    """Fingerprint the optional MuseTalk sidecar independently of ComfyUI."""
    lock = load_yaml_file(root / "manifests" / "containers.lock.yaml")
    inputs = {
        "schema_version": lock.get("schema_version"),
        "musetalk": lock.get("musetalk"),
        "dockerfile_sha256": hashlib.sha256(
            (root / "docker" / "musetalk.Dockerfile").read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(
            (root / "docker" / "musetalk-run.sh").read_bytes()).hexdigest(),
    }
    canonical = json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def _inspect_image(image, label, expected):
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format",
             '{{ index .Config.Labels "%s" }}' % label, image],
            capture_output=True, text=True, check=False, timeout=20,
        )
        return inspected.returncode == 0 and inspected.stdout.strip() == expected
    except (OSError, subprocess.TimeoutExpired):
        return False


def runtime_image_plan(root, recipe=None):
    """Report whether the exact pinned runtime image has already been built."""
    build_sha = runtime_build_identity(root)
    image = "narration-video-gen-comfy:%s" % build_sha[:12]
    images = [{
        "name": "comfy",
        "image": image,
        "build_sha256": build_sha,
        "present": _inspect_image(
            image, "io.narration-video-gen.runtime-build-sha", build_sha),
    }]
    if recipe and "musetalk" in (recipe.get("pipeline_stages") or []):
        muse_sha = musetalk_build_identity(root)
        muse_image = "narration-video-gen-musetalk:%s" % muse_sha[:12]
        images.append({
            "name": "musetalk",
            "image": muse_image,
            "build_sha256": muse_sha,
            "present": _inspect_image(
                muse_image, "io.narration-video-gen.musetalk-build-sha", muse_sha),
        })
    return {
        "image": image,
        "build_sha256": build_sha,
        "present": all(item["present"] for item in images),
        "images": images,
    }


def _same_reference_gpu(left, right):
    """Whether two evidence records name the same measured GPU."""
    def normalise(value):
        return "".join(character for character in (value or "").lower()
                       if character.isalnum())
    return bool(normalise(left) and normalise(left) == normalise(right))


def _composed_stage_estimate(profile, recipe, seconds, catalog):
    """Compose a full-pipeline reference from compatible recorded stages.

    A new configuration is often measured stage-by-stage before a full run is
    available.  Do not discard that useful same-GPU evidence at run start: use
    its stage first, then fill only missing post-stages from the nearest recipe
    of the same model family and measured GPU.
    """
    if catalog is None or seconds is None:
        return None
    evidence = profile.get("evidence") or {}
    reference_gpu = evidence.get("gpu_model")
    reference_seconds = evidence.get("seconds")
    if not reference_gpu or not reference_seconds:
        return None
    target_stages = evidence.get("stage_minutes") or {}
    target_resolution = tuple(recipe.get("resolution") or ())
    components = []
    for stage in recipe.get("pipeline_stages") or ():
        minutes = target_stages.get(stage)
        source_profile = profile["id"] if minutes is not None else None
        source_seconds = reference_seconds if minutes is not None else None
        if minutes is None:
            candidates = []
            for candidate in catalog.profiles.values():
                candidate_evidence = candidate.get("evidence") or {}
                candidate_minutes = (candidate_evidence.get("stage_minutes") or {}).get(stage)
                if (candidate_minutes is None
                        or not _same_reference_gpu(reference_gpu,
                                                   candidate_evidence.get("gpu_model"))):
                    continue
                candidate_recipe = catalog.recipe_for(candidate)
                if candidate_recipe.get("model_family") != recipe.get("model_family"):
                    continue
                candidate_seconds = candidate_evidence.get("seconds")
                if not candidate_seconds:
                    continue
                resolution = tuple(candidate_recipe.get("resolution") or ())
                distance = sum(abs(a - b) for a, b in zip(target_resolution, resolution))
                candidates.append((distance, candidate["id"], candidate_minutes,
                                   candidate_seconds))
            if not candidates:
                return None
            _distance, source_profile, minutes, source_seconds = min(candidates)
        scaled_minutes = float(minutes) * (float(seconds) / float(source_seconds))
        components.append({"stage": stage, "minutes": round(scaled_minutes, 3),
                           "source_profile": source_profile})
    if not components:
        return None
    return {"known": True, "extrapolated": True, "composed": True,
            "minutes": round(sum(item["minutes"] for item in components)),
            "stage_estimates": components}


def estimate_runtime(profile, recipe, seconds=None, catalog=None):
    """Scale the profile's measured wall-clock time to a different audio length.

    The measurement in each profile is real; the extrapolation is not, so the
    caller is told which is which.
    """
    evidence = profile.get("evidence") or {}
    reference = (profile.get("timing") or {}).get("reference") or {}
    # New profiles keep timing separate from qualification evidence so several
    # cooling/power observations can coexist. Keep the legacy evidence field as
    # a compatibility fallback for published profiles not yet migrated.
    measured_minutes = (float(reference["wall_clock_seconds"]) / 60.0
                        if reference.get("wall_clock_seconds") is not None
                        else evidence.get("wall_clock_minutes"))
    measured_seconds = evidence.get("seconds")
    if measured_minutes is None or not measured_seconds:
        return _composed_stage_estimate(profile, recipe, seconds, catalog) or {"known": False}
    requested_seconds = seconds if seconds is not None else measured_seconds
    comparable = []
    for observation in (profile.get("timing") or {}).get("observations") or []:
        if observation.get("scope") not in ("e2e", "resumed-e2e"):
            continue
        basis_seconds = observation.get("basis_seconds") or measured_seconds
        observation_minutes = float(observation["wall_clock_seconds"]) / 60.0
        comparable.append(observation_minutes * requested_seconds / basis_seconds)
    observation_range = None
    if comparable:
        observation_range = {
            "min_minutes": round(min(comparable), 2),
            "max_minutes": round(max(comparable), 2),
            "count": len(comparable),
        }
    if seconds is None or abs(seconds - measured_seconds) < 0.5:
        return {
            "known": True,
            "extrapolated": False,
            "minutes": round(measured_minutes, 2),
            "basis_seconds": measured_seconds,
            "timing_scope": reference.get("scope") if reference else None,
            "timing_environment": reference.get("environment") if reference else None,
            "observation_range": observation_range,
        }
    # Sampling dominates and scales close to linearly with frame count; the
    # fixed model-load cost is small next to runs of this length.
    scaled = measured_minutes * (seconds / measured_seconds)
    return {
        "known": True,
        "extrapolated": True,
        "minutes": round(scaled),
        "basis_minutes": round(measured_minutes, 2),
        "basis_seconds": measured_seconds,
        "timing_scope": reference.get("scope") if reference else None,
        "timing_environment": reference.get("environment") if reference else None,
        "observation_range": observation_range,
    }


def host_changes(profile):
    """List host-level changes a profile needs that a human must approve."""
    changes = list(profile.get("host_changes") or [])
    requires = profile.get("requires") or {}
    if profile["platform"] == "windows-wsl2":
        wsl_ram = (profile.get("settings") or {}).get("wsl_ram_gib")
        wsl_swap = (profile.get("settings") or {}).get("wsl_swap_gib")
        if wsl_ram or wsl_swap:
            changes.append(
                "edit %%UserProfile%%\\.wslconfig (memory=%sGB, swap=%sGB) and run "
                "`wsl --shutdown` from Windows -- this stops every WSL distribution"
                % (wsl_ram or "?", wsl_swap or "?")
            )
    if requires.get("swap_gib_min"):
        changes.append(
            "ensure at least %s GiB of swap is active; block swapping pages weights "
            "through host memory" % requires["swap_gib_min"]
        )
    return changes


def build_plan(root, catalog, profile, env, models_dir, seconds=None, recipe=None):
    recipe = recipe or catalog.recipe_for(profile)
    downloads = download_plan(root, recipe, models_dir)
    runtime = estimate_runtime(profile, recipe, seconds, catalog=catalog)

    free_gib = ((env.get("disk", {}).get("models") or {}).get("free_gib"))
    disk_ok = None
    required_free_gib = downloads["missing_bytes"] / GIB + 20
    if free_gib is not None:
        # Leave headroom for outputs and the container image itself.
        disk_ok = free_gib >= required_free_gib

    return {
        "profile": profile["id"],
        "recipe": recipe["id"],
        "platform": profile["platform"],
        "resolution": recipe.get("resolution"),
        "fps": recipe.get("fps"),
        "pipeline_stages": list(recipe.get("pipeline_stages") or []),
        "settings": profile.get("settings"),
        "downloads": downloads,
        "runtime_image": runtime_image_plan(root, recipe),
        "recipe_options": list(recipe.get("active_pipeline_options") or []),
        "runtime_estimate": runtime,
        "host_changes": host_changes(profile),
        "disk": {
            "free_gib": free_gib,
            "needed_gib": downloads["missing_gib"],
            "required_free_gib": round(required_free_gib, 2),
            "sufficient": disk_ok,
        },
        "approvals_required": _approvals(profile, downloads),
    }


def _approvals(profile, downloads):
    """The decisions this repository deliberately refuses to make on its own."""
    items = []
    if downloads["missing_gib"] > 0:
        items.append(
            "download %.1f GiB of model weights and accept each model's license"
            % downloads["missing_gib"]
        )
    if host_changes(profile):
        items.append("apply the host changes listed above")
    from .select import is_fully_verified  # local import keeps the module import-light
    if not is_fully_verified(profile):
        items.append(
            "acknowledge that this profile is not a physical-GPU, full-length, "
            "visually reviewed result"
        )
    items.append("review the finished video and audio yourself before publishing it")
    return items
