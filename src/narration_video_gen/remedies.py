"""Turn a failed run into the next thing to try.

Knowing that a run failed is not useful on its own. What a reader needs is
which resource ran out and what to change, with the numbers filled in from
their machine rather than from the machine that produced the catalog.

The catalog records what worked here. That is evidence a setting *can* work, not
that it will work on another machine: a driver reserving a few hundred MiB more,
a desktop session on the same card, or a longer clip all move the boundary. So
the failure path has to stand on its own, and these functions are the part that
does.

Each remedy is one concrete action with the current and suggested value already
resolved. They are ordered: the first one is what to try next.
"""

from __future__ import annotations

# Block swapping trades speed for VRAM headroom. Steps of two are what the
# recorded Wan runs moved in (25 -> 28 -> 30 -> 32), and each step costs time
# because more weights move across PCIe every sampler step.
BLOCK_SWAP_STEP = 2
BLOCK_SWAP_MAX = 40
FACE_DETAILER_SIZE_STEPS = (512, 384, 320, 256)

# Below this much free host memory a run is at the mercy of the OOM killer even
# when swap is large, because the working set has to be resident to make progress.
HOST_RAM_FLOOR_GIB = 4.0


class Remedy:
    """One action, with the numbers already resolved for this machine."""

    def __init__(self, action, detail, *, setting=None, current=None, suggested=None):
        self.action = action          # short imperative, shown as the heading
        self.detail = detail          # why, and what it costs
        self.setting = setting        # profile key to change, when there is one
        self.current = current
        self.suggested = suggested

    def as_dict(self):
        out = {"action": self.action, "detail": self.detail}
        for key in ("setting", "current", "suggested"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def _gib(value):
    return None if value is None else round(float(value), 1)


def _next_face_detailer_size(current):
    """Return the next conservative detail plane below ``current``."""
    if not isinstance(current, int):
        return None
    return next((size for size in FACE_DETAILER_SIZE_STEPS if size < current), None)


def for_gpu_oom(profile, env, context=None, recipe=None, stage=None):
    """VRAM ran out. Move weights off the card, or use a smaller canvas."""
    context = context or {}
    settings = (profile or {}).get("settings") or {}
    remedies = []

    face_detailer = stage == "face-detailer"
    setting_name = ("face_detailer_blocks_to_swap" if face_detailer
                    else "blocks_to_swap")
    current = (settings.get("face_detailer_blocks_to_swap",
                            settings.get("blocks_to_swap"))
               if face_detailer else settings.get("blocks_to_swap"))
    if isinstance(current, int) and current < BLOCK_SWAP_MAX:
        suggested = min(BLOCK_SWAP_MAX, current + BLOCK_SWAP_STEP)
        margin = ""
        requested, free = context.get("allocation_requested"), context.get("memory_free")
        if requested and free:
            margin = " The run asked for %s with %s left, so it was close." % (requested, free)
        remedies.append(Remedy(
            "Swap more blocks to system memory",
            "Raising blocks_to_swap keeps fewer weights on the card at once. "
            "It is the setting these profiles use to fit a larger canvas on a "
            "smaller card, and it costs time: more weights cross PCIe on every "
            "sampler step.%s" % margin,
            setting=setting_name, current=current, suggested=suggested))
    elif isinstance(current, int) and not face_detailer:
        remedies.append(Remedy(
            "Use a smaller canvas",
            "blocks_to_swap is already at %d, near the point where nearly every "
            "block is swapped and there is little left to move off the card. A "
            "lower resolution profile is the remaining option." % current))

    if face_detailer:
        # Keep failure advice aligned with runner.py's conservative default.
        current_size = settings.get("face_detailer_size", 320)
        suggested_size = _next_face_detailer_size(current_size)
        if suggested_size is not None:
            remedies.append(Remedy(
                "Use a smaller Face Detailer plane",
                "The tracked face crop is resized to this square plane before VACE. "
                "Reducing it lowers temporal activation memory for every frame, but "
                "also trades away some facial detail. It does not change the source "
                "video resolution or the tracked mask.",
                setting="face_detailer_size", current=current_size,
                suggested=suggested_size))

    remedies.append(Remedy(
        "Free the card of everything else",
        "A desktop session, a browser with hardware acceleration, or a second "
        "CUDA process all take VRAM that this run then cannot use. Note that the "
        "driver itself reserves a few hundred MiB, so a 16 GiB card offers less "
        "than 16 GiB to CUDA; that reservation is normal and is already counted "
        "in the failure message."))

    resolution = ((recipe or {}).get("resolution")
                  or (profile or {}).get("resolution")
                  or (profile or {}).get("recipe_resolution"))
    if resolution:
        remedies.append(Remedy(
            "Drop to a lower resolution profile",
            "Resolution drives VRAM harder than clip length does. If the same "
            "model has a 480p profile, it will fit where 720p does not."))
    return remedies


def for_host_oom(profile, env, context=None):
    """Host RAM ran out. Swap sometimes covers it and sometimes cannot."""
    env = env or {}
    memory = env.get("memory") or {}
    ram = _gib(memory.get("ram_gib"))
    swap = _gib(memory.get("swap_gib"))
    requires = (profile or {}).get("requires") or {}
    ram_needed = requires.get("host_ram_gib_min")
    swap_needed = requires.get("swap_gib_min")
    remedies = []

    if swap is not None and swap_needed is not None and swap + 1e-9 < swap_needed:
        remedies.append(Remedy(
            "Add swap",
            "Block swapping moves weights to host memory, and this profile "
            "expects swap to absorb the peak. The machine is below what the "
            "profile asks for. scripts/setup-linux.sh adds swap and makes it "
            "persistent, confirming the size first.",
            setting="swap_gib", current=swap, suggested=swap_needed))
    elif swap is not None and swap >= (swap_needed or 0):
        remedies.append(Remedy(
            "More swap will not help here",
            "Swap already meets what the profile asks for (%.1f GiB). Swap "
            "covers a peak; it cannot stand in for memory the run has to touch "
            "continuously, and a run that thrashes will crawl rather than "
            "finish. The next lever is physical RAM." % swap))

    if ram is not None and ram_needed is not None and ram + 1e-9 < ram_needed:
        remedies.append(Remedy(
            "Add physical RAM",
            "The profile needs %.1f GiB of kernel-visible RAM and this machine "
            "has %.1f GiB. This is the one shortage swap cannot paper over."
            % (float(ram_needed), ram)))
    elif ram is not None and ram < HOST_RAM_FLOOR_GIB:
        remedies.append(Remedy(
            "Add physical RAM",
            "Only %.1f GiB of RAM is visible. Even with swap configured, a run "
            "this size has no resident working set to make progress in." % ram))

    remedies.append(Remedy(
        "Close what else is using memory",
        "Browsers and editors hold gigabytes. A run that fits on an idle "
        "machine can fail on a busy one, and nothing in the profile can "
        "predict that."))
    return remedies


def for_failure(error_code, profile, env, context=None, recipe=None, stage=None):
    """Ranked remedies for one failure, or an empty list when there is no advice."""
    if error_code == "gpu-out-of-memory":
        return for_gpu_oom(profile, env, context, recipe, stage=stage)
    if error_code == "host-out-of-memory":
        return for_host_oom(profile, env, context)
    return []
