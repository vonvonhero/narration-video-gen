"""Match a detected environment against the profile catalog.

The repository publishes every verified cell, including short-length and
capacity-simulated ones. Rather than asking the reader to find their row in a
table, ``select`` filters the catalog by what the machine actually has and then
ranks what survives. A profile is only ever *offered*; anything short of
"physical GPU, full length, visually reviewed" is reported as such and requires
an explicit acknowledgement before ``run`` will use it.
"""

from __future__ import annotations

from . import detect

# Higher is better. These orderings encode the judgement in docs/evidence.md.
STATUS_RANK = {"recommended": 3, "acceptable": 2, "experimental": 1, "not-recommended": 0}
DURATION_RANK = {"full": 3, "short": 2, "gate": 1}
GPU_EVIDENCE_RANK = {"physical": 2, "capacity-simulated": 1}
VISUAL_RANK = {"passed": 2, "pending": 1, "failed": 0}
PRODUCT_PREFERENCE = {
    "wan22-s2v-720p": 2,
    "wan22-s2v-480p": 1,
    "wan22-s2v-720p-q4ks": 2,
    "wan22-s2v-480p-q4ks": 1,
}

# A card sold as "16 GB" reports 16376 MiB, not 16384: the board reserves a
# slice for ECC and display scanout. Comparing a nominal tier against the
# reported figure with a strict >= would reject the exact RTX A4000 that
# produced the reference runs. Profiles state nominal tiers, so the comparison
# allows this much shortfall -- and no more, so a 12 GiB card still cannot
# satisfy a 16 GiB profile.
VRAM_TIER_TOLERANCE = 0.97

# RAM requirements also name nominal hardware/configuration tiers. Windows
# reports a 32 GB machine as about 31.9 GiB, and a WSL `memory=20GB` ceiling is
# slightly smaller inside the guest. Keep exact checks for swap and disk, but
# allow the same small accounting shortfall for host and physical RAM tiers.
RAM_TIER_TOLERANCE = 0.97

# Requirement key -> (human label, extractor, unit)
_CHECKS = (
    ("host_ram_gib_min", "kernel-visible RAM", lambda env: env["memory"]["ram_gib"], "GiB"),
    ("swap_gib_min", "swap", lambda env: env["memory"]["swap_gib"], "GiB"),
    ("physical_ram_gib_min", "physical Windows RAM",
     lambda env: (env.get("windows_host") or {}).get("physical_ram_gib"), "GiB"),
    ("free_disk_gib_min", "free disk for models",
     lambda env: (env.get("disk", {}).get("models") or {}).get("free_gib"), "GiB"),
)


class Blocker(str):
    """An English blocker line that also carries the cause behind it.

    It stays a plain string for JSON output and for every existing caller; the
    CLI reads the structured fields to render the same reason in Japanese.
    """

    def __new__(cls, text, code, **fields):
        blocker = super().__new__(cls, text)
        blocker.code = code
        blocker.fields = fields
        return blocker


def _gpu_field(env, field):
    gpu = detect.primary_gpu(env)
    return gpu.get(field) if gpu else None


def evaluate(profile, env):
    """Return ``(eligible, blockers, notes)`` for one profile against ``env``."""
    blockers = []
    notes = []

    if profile["platform"] != env.get("platform"):
        # Nothing else about this profile is worth reporting once the platform
        # is wrong -- the remaining numbers would just be noise.
        return False, [Blocker(
            "platform is %s, profile targets %s"
            % (env.get("platform"), profile["platform"]),
            "platform", actual=env.get("platform"), needed=profile["platform"])], []

    requires = profile.get("requires") or {}

    vram_needed = requires.get("gpu_vram_gib_min")
    if vram_needed is not None:
        actual_vram = _gpu_field(env, "vram_gib")
        if actual_vram is None:
            blockers.append(Blocker(
                "no GPU visible (profile needs a %s GiB card)" % vram_needed,
                "gpu-missing", needed=vram_needed))
        elif actual_vram < vram_needed * VRAM_TIER_TOLERANCE:
            blockers.append(Blocker(
                "GPU VRAM is %s GiB, profile needs a %s GiB card"
                % (actual_vram, vram_needed),
                "gpu-vram", actual=actual_vram, needed=vram_needed))

    for key, label, extractor, unit in _CHECKS:
        needed = requires.get(key)
        if needed is None:
            continue
        try:
            actual = extractor(env)
        except (KeyError, TypeError):
            actual = None
        if actual is None:
            # Unknown is not the same as insufficient: withhold rather than guess.
            blockers.append(Blocker(
                "%s could not be measured (profile needs >= %s %s)"
                % (label, needed, unit),
                "unmeasured", key=key, label=label, needed=needed, unit=unit))
            continue
        threshold = (needed * RAM_TIER_TOLERANCE
                     if key in ("host_ram_gib_min", "physical_ram_gib_min")
                     else needed)
        if actual + 1e-9 < threshold:
            blockers.append(Blocker(
                "%s is %s %s, profile needs >= %s %s"
                % (label, actual, unit, needed, unit),
                "insufficient", key=key, label=label, actual=actual,
                needed=needed, unit=unit))

    container = env.get("container", {})
    if requires.get("docker", True) and not container.get("docker_available"):
        blockers.append(Blocker("docker is not available", "docker-missing"))
    if requires.get("nvidia_runtime", True) and not container.get("nvidia_runtime"):
        blockers.append(Blocker("docker has no nvidia runtime", "nvidia-runtime"))

    evidence = profile.get("evidence") or {}
    if evidence.get("gpu_evidence") == "capacity-simulated":
        notes.append(
            "verified on a larger GPU limited to %s GiB, not on a physical %s GiB GPU"
            % (requires.get("gpu_vram_gib_min", "?"), requires.get("gpu_vram_gib_min", "?"))
        )
    if evidence.get("duration_class") == "short":
        notes.append("verified only at %s frames (%s s), not for a full narration"
                     % (evidence.get("frames"), evidence.get("seconds")))
    if evidence.get("duration_class") == "gate":
        notes.append("stopped part-way during verification; no finished video exists")
    if evidence.get("visual_review") == "pending":
        notes.append("the output has not been reviewed by a person")
    if profile["status"] == "not-recommended":
        notes.append("not recommended; read the limitations before using it")
    for limitation in profile.get("limitations") or []:
        notes.append(limitation)

    return (not blockers), blockers, notes


def _recovery_distance(profile, env):
    """Approximate how far the machine is from one ineligible profile.

    This is only for choosing which actionable explanation to show. It never
    makes a profile eligible and never participates in normal profile ranking.
    """
    requires = profile.get("requires") or {}
    distance = 0.0
    vram_needed = requires.get("gpu_vram_gib_min")
    if vram_needed:
        actual = _gpu_field(env, "vram_gib") or 0
        threshold = vram_needed * VRAM_TIER_TOLERANCE
        distance += max(threshold - actual, 0) / threshold
    for key, _, extractor, _ in _CHECKS:
        needed = requires.get(key)
        if not needed:
            continue
        try:
            actual = extractor(env)
        except (KeyError, TypeError):
            actual = None
        distance += 1.0 if actual is None else max(needed - actual, 0) / needed
    container = env.get("container", {})
    if requires.get("docker", True) and not container.get("docker_available"):
        distance += 0.25
    if requires.get("nvidia_runtime", True) and not container.get("nvidia_runtime"):
        distance += 0.25
    return distance


def confidence_key(profile, recipe=None):
    """Sort key used to rank eligible profiles (descending).

    The order of the components is the whole design decision:

    1. ``duration_class`` comes first, so a capacity probe that never produced
       a video can never outrank a profile that did.
    2. Rejected or visually failed results are excluded from preference.
    3. ``gpu_evidence`` next: a physical-GPU result beats a simulated one.
    4. The VRAM tier comes before product preference, so a larger card does not
       inherit an unnecessarily aggressive low-VRAM block-swap profile.
    5. Within the same evidence and hardware tier, prefer the primary Wan 2.2
       path over Wan 2.1.  This is a product decision shared by Linux and
       Windows; evidence limitations are still displayed and acknowledged.
    6. ``status`` and ``visual_review`` then rank the evidence quality within
       that product choice.

    The VRAM tier is about fitting the hardware rather than about trust.
    Running a profile tuned for 16 GiB on a 24 GiB card is not the safer choice:
    it swaps blocks that would have fit, turning a 21 minute run into a 66
    minute one. Whatever review status the chosen profile has is reported.
    """
    evidence = profile.get("evidence") or {}
    requires = profile.get("requires") or {}
    pixels = 0
    if recipe and isinstance(recipe.get("resolution"), (list, tuple)):
        pixels = recipe["resolution"][0] * recipe["resolution"][1]
    return (
        DURATION_RANK.get(evidence.get("duration_class"), 0),
        int(profile["status"] != "not-recommended"
            and evidence.get("visual_review") != "failed"),
        GPU_EVIDENCE_RANK.get(evidence.get("gpu_evidence"), 0),
        requires.get("gpu_vram_gib_min", 0),
        PRODUCT_PREFERENCE.get(profile.get("recipe"), 0),
        STATUS_RANK.get(profile["status"], 0),
        VISUAL_RANK.get(evidence.get("visual_review"), 0),
        -pixels,
    )


def _normalise_gpu_name(value):
    return "".join(character for character in str(value or "").lower()
                   if character.isalnum())


def evidence_matches_gpu(profile, env):
    """Return whether provenance names the current GPU model.

    This is informational only.  Compatibility is based on platform and VRAM
    capacity, not the product name.
    """
    evidence = profile.get("evidence") or {}
    references = [evidence.get("gpu_model"),
                  *(evidence.get("qualified_gpu_models") or [])]
    actual_gpu = detect.primary_gpu(env) or {}
    actual = _normalise_gpu_name(actual_gpu.get("name"))
    return bool(actual and any(
        reference and (actual in reference or reference in actual)
        for reference in map(_normalise_gpu_name, references)
    ))


def _matches_hardware_class(profile, env):
    """Return whether ``env`` shares the profile's platform and VRAM tier."""
    if profile.get("platform") != env.get("platform"):
        return False
    needed = (profile.get("requires") or {}).get("gpu_vram_gib_min")
    if needed is None:
        return True
    actual = _gpu_field(env, "vram_gib")
    return actual is not None and actual >= needed * VRAM_TIER_TOLERANCE


def is_fully_verified(profile, env=None):
    """True when the profile's published evidence needs no acknowledgement.

    Hardware eligibility is decided separately by :func:`evaluate`, using the
    platform and resource requirements (including the nominal VRAM tier).  A
    GPU product name records where evidence came from; it does not define the
    only product allowed to reuse settings for the same capacity tier.

    When ``env`` is provided, its platform and VRAM tier must also fit. Exact
    GPU model names intentionally do not affect the result.
    """
    evidence = profile.get("evidence") or {}
    intrinsic = (
        profile["status"] in ("recommended", "acceptable")
        and evidence.get("duration_class") == "full"
        and evidence.get("gpu_evidence") == "physical"
        and evidence.get("visual_review") == "passed"
    )
    return intrinsic and (env is None or _matches_hardware_class(profile, env))


def select(catalog, env, recipe=None, model_family=None, resolution=None,
           profile_id=None, include_ineligible=False):
    """Rank profiles for ``env``, optionally narrowed to a recipe or resolution.

    Returns a dict with ``eligible`` (best first), ``ineligible`` and ``best``.
    """
    eligible = []
    ineligible = []

    for profile in catalog.sorted_profiles():
        rec = catalog.recipe_for(profile)
        if profile_id and profile["id"] != profile_id:
            continue
        if recipe and profile["recipe"] != recipe:
            continue
        if model_family and rec.get("model_family") != model_family:
            continue
        if resolution and _resolution_label(rec) != resolution:
            continue

        ok, blockers, notes = evaluate(profile, env)
        entry = {
            "id": profile["id"],
            "recipe": profile["recipe"],
            "model_family": rec.get("model_family"),
            "resolution": _resolution_label(rec),
            "status": profile["status"],
            "evidence": profile.get("evidence"),
            "settings": profile.get("settings"),
            "requires": profile.get("requires"),
            "notes": notes,
            "blockers": blockers,
            "fully_verified": is_fully_verified(profile, env),
            "path": profile.get("_path"),
        }
        (eligible if ok else ineligible).append((profile, entry, rec))

    eligible.sort(key=lambda row: confidence_key(row[0], row[2]), reverse=True)
    # For an unavailable user-selected model/resolution, report one useful
    # recovery target rather than dumping every internal tuning profile. A
    # profile for the current platform always beats a superficially closer
    # profile for another OS; within that group prefer the smallest aggregate
    # upgrade before counting blockers. A high-VRAM no-swap profile must not
    # become the recovery advice merely because it has one fewer requirement.
    ineligible.sort(key=lambda row: (
        row[0].get("platform") != env.get("platform"),
        _recovery_distance(row[0], env),
        len(row[1]["blockers"]),
        tuple(-value for value in confidence_key(row[0], row[2])),
        row[1]["id"],
    ))

    result = {
        "eligible": [row[1] for row in eligible],
        "best": eligible[0][1] if eligible else None,
        "closest": ineligible[0][1] if ineligible else None,
    }
    if include_ineligible:
        result["ineligible"] = [row[1] for row in ineligible]
    return result


def _resolution_label(recipe):
    res = recipe.get("resolution")
    if isinstance(res, (list, tuple)) and len(res) == 2:
        return "%dp" % res[1]
    return str(res)
