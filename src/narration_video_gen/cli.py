"""``narration-video-gen`` command line interface.

Every subcommand accepts ``--json`` so that the same entry point serves a human
reading a terminal and automation parsing structured output.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import ipaddress
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
import wave
import webbrowser
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import detect as detect_mod
from . import calibration_state
from . import measure as measure_mod
from . import remedies
from . import plan as plan_mod
from . import verify as verify_mod
from .catalog import Catalog, CatalogError, repo_root
from .select import confidence_key, evaluate, is_fully_verified, select
from . import selection_state
from . import run_state
from . import timings
from . import narration
from . import tts_service
from . import tts_web
from . import ui_locale

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_MATCH = 2
EXIT_CANCELLED = 130

MODEL_ALIASES = {
    "wan21": "wan21-infinitetalk",
    "wan2.1": "wan21-infinitetalk",
    "2.1": "wan21-infinitetalk",
    "wan21-infinitetalk": "wan21-infinitetalk",
    "wan22": "wan22-s2v",
    "wan2.2": "wan22-s2v",
    "2.2": "wan22-s2v",
    "wan22-s2v": "wan22-s2v",
}

MODEL_LABELS = {
    "wan21-infinitetalk": "Wan 2.1 InfiniteTalk",
    "wan22-s2v": "Wan 2.2 S2V",
}

STAGE_LABELS = {
    "infinitetalk": ("動画生成", "Generate video"),
    "s2v": ("動画生成", "Generate video"),
    "musetalk": ("リップシンク強化", "Enhance lip sync"),
    "face-detailer": ("顔補正", "Refine face"),
    "rife": ("フレーム補間", "Interpolate frames"),
    "retime": ("音声・長さ調整", "Align audio and duration"),
}

EXACT_FRAME_STAGES = frozenset(("infinitetalk", "s2v", "face-detailer"))
WINDOWS_EXPORT_DIRECTORY = "Narration Video Gen"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _emit(args, payload, render):
    if getattr(args, "json", False):
        output = getattr(args, "_json_output", sys.stdout)
        json.dump(payload, output, indent=2, ensure_ascii=False, sort_keys=False)
        output.write("\n")
        args._json_emitted = True
    else:
        render(payload)


def _cli_error(args, message, code=EXIT_ERROR):
    _emit(args, {"error": message}, lambda _data: print(message, file=sys.stderr))
    return code


def _models_missing(downloads):
    """Use bytes: a missing small model rounds down to zero GiB."""
    return downloads.get("missing_bytes", downloads["missing_gib"]) > 0


def _bullet(lines, prefix="  - ", output=None):
    for line in lines:
        print("%s%s" % (prefix, line), file=output or sys.stdout)


def _canonical_audio_source(_stage_id, current, produced):
    """Keep the earliest narration track instead of accumulating AAC padding.

    ComfyUI's video-combine nodes encode the same audio again at each visual
    post-stage. AAC priming/padding can move its reported end by several
    packets, which makes the final 60 fps retime add or remove frames that were
    not implied by the source narration. The original WAV or the first audio
    bearing input establishes the track; no video stage replaces it.
    """
    return produced if current is None else current


def _expected_media_frames(stage_id, frames):
    """Return a strict media-frame contract only for non-interpolating stages."""
    return frames if stage_id in EXACT_FRAME_STAGES else None


def _record_media_contract(root, run_id, stage_id, stage_index, evidence):
    """Append an atomic, machine-readable raw/audio validation record."""
    from . import runner as runner_mod

    path = Path(root) / "outputs" / run_id / "media-contract.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {"schema_version": 1, "run_id": run_id, "stages": []}
    except (OSError, ValueError) as exc:
        raise runner_mod.RunnerError(
            "could not read media contract evidence %s: %s" % (path, exc)) from exc
    record = dict(evidence)
    record.update({
        "stage": stage_id,
        "stage_index": stage_index,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    payload["stages"].append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _env_for(args):
    if getattr(args, "env_file", None):
        return json.loads(Path(args.env_file).read_text(encoding="utf-8"))
    root = args.root
    return detect_mod.detect(models_dir=root / "models", outputs_dir=root / "outputs")


def _normalise_model_family(value):
    if value is None:
        return None
    normalised = MODEL_ALIASES.get(value.lower())
    if not normalised:
        raise ValueError("unknown model %r; choose wan21 or wan22" % value)
    return normalised


def _model_label(model_family):
    return MODEL_LABELS.get(model_family, model_family)


def _ui_is_japanese():
    """Use the app preference, falling back to the process message locale."""
    return ui_locale.is_japanese()


def _tr(japanese, english):
    return japanese if _ui_is_japanese() else english


def _open_browser(url):
    """Open a local page in the desktop browser, including from inside WSL2."""
    if detect_mod.detect_platform() == "windows-wsl2":
        environment = os.environ.copy()
        environment["NVG_BROWSER_URL"] = url
        # WSL does not pass arbitrary Linux environment variables to Windows
        # processes. Publish this one explicitly without a path-translation
        # modifier: applying /u to an http:// URL drops the value.
        shared = [entry for entry in environment.get("WSLENV", "").split(":")
                  if entry and entry.split("/", 1)[0] != "NVG_BROWSER_URL"]
        environment["WSLENV"] = ":".join([*shared, "NVG_BROWSER_URL"])
        try:
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "Start-Process -FilePath $env:NVG_BROWSER_URL",
            ], env=environment, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL, timeout=15, check=False)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return webbrowser.open(url)


def _warn_windows_mounted_checkout(args):
    """Warn before large model I/O is performed through a Windows mount."""
    if args.json or detect_mod.detect_platform() != "windows-wsl2":
        return
    root = str(args.root.resolve())
    if root == "/mnt" or root.startswith("/mnt/"):
        print(_tr(
            "警告: このリポジトリはWindows側のドライブにあります。モデルI/Oが遅くなるため、"
            "WSL側の ~/narration-video-gen を使用してください。",
            "warning: this checkout is on a Windows-mounted drive; use "
            "~/narration-video-gen inside WSL to avoid slow model I/O."),
            file=sys.stderr)


_BLOCKER_LABELS = {
    "host_ram_gib_min": "カーネルから見えるRAM",
    "swap_gib_min": "swap",
    "physical_ram_gib_min": "Windowsの物理RAM",
    "free_disk_gib_min": "モデル用のディスク空き容量",
}


def _blocker_text(blocker):
    """Render one blocker in the UI language, English text being the default."""
    code = getattr(blocker, "code", None)
    if code is None or not _ui_is_japanese():
        return str(blocker)
    fields = blocker.fields
    label = _BLOCKER_LABELS.get(fields.get("key"), fields.get("label"))
    if code == "platform":
        return "このマシンは%s、プロファイルの対象は%s" % (fields["actual"], fields["needed"])
    if code == "gpu-missing":
        return "GPUが見つかりません（%s GiBのカードが必要）" % fields["needed"]
    if code == "gpu-vram":
        return "GPUのVRAMは%s GiB、必要なのは%s GiBのカード" % (fields["actual"], fields["needed"])
    if code == "unmeasured":
        return "%sを測定できません（%s %s以上が必要）" % (label, fields["needed"], fields["unit"])
    if code == "insufficient":
        return "%sは%s %s、%s %s以上が必要" % (
            label, fields["actual"], fields["unit"], fields["needed"], fields["unit"])
    if code == "docker-missing":
        return "Dockerを利用できません"
    if code == "nvidia-runtime":
        return "DockerにNVIDIAランタイムがありません"
    return str(blocker)


def _blocker_remedy(blockers):
    """Return the one command that addresses these blockers, when there is one."""
    codes = {getattr(blocker, "code", None) for blocker in blockers}
    keys = {getattr(blocker, "fields", {}).get("key") for blocker in blockers}
    if "free_disk_gib_min" in keys:
        return _tr("models/ を置くファイルシステムの空き容量を増やしてください。",
                   "Free up space on the filesystem holding models/.")
    if "swap_gib_min" in keys or codes & {"docker-missing", "nvidia-runtime"}:
        if detect_mod.detect_platform() == "windows-wsl2":
            return _tr("Windowsで setup.cmd を実行して環境を確認してください。",
                       "Run setup.cmd in Windows to check the environment.")
        return _tr("ホストの準備には scripts/setup-linux.sh を実行してください。",
                   "Run scripts/setup-linux.sh to prepare the host.")
    return None


def _without_stale_download_budget(blockers, root, catalog, profile, env,
                                   recipe, preparation=None):
    """Drop the free-disk blocker once the weights it budgets for are on disk.

    ``free_disk_gib_min`` sizes a from-scratch download, so it answers "is there
    room to install this profile". Running asks a different question: "is there
    room to generate with what is already here". ``plan`` measures that -- the
    bytes still missing plus working headroom -- so when it reports the disk as
    sufficient, the profile's download budget says nothing about this machine.
    Leaving the blocker in place would also contradict the ``[OK] 空き容量``
    line printed from the same payload.

    Pass ``preparation`` when the caller already built the plan; otherwise this
    builds one, which only inspects files on disk.
    """
    if not any(getattr(b, "fields", {}).get("key") == "free_disk_gib_min"
               for b in blockers):
        return blockers
    if preparation is None:
        try:
            preparation = plan_mod.build_plan(
                root, catalog, profile, env, root / "models", recipe=recipe)
        except Exception:
            # Measuring failed, so the static budget is the only figure left.
            return blockers
    if not preparation["disk"]["sufficient"]:
        return blockers
    return [b for b in blockers
            if getattr(b, "fields", {}).get("key") != "free_disk_gib_min"]


def _print_blockers(blockers, output=None, prefix="  - "):
    output = output or sys.stdout
    _bullet([_blocker_text(blocker) for blocker in blockers], prefix, output)
    remedy = _blocker_remedy(blockers)
    if remedy:
        print(remedy, file=output)


def _verification_limits(profile):
    evidence = profile.get("evidence") or {}
    limits = []
    if evidence.get("duration_class") != "full":
        limits.append(_tr("短尺のみ", "short only"))
    if evidence.get("gpu_evidence") != "physical":
        limits.append(_tr("VRAM容量シミュレーション", "simulated VRAM capacity"))
    if evidence.get("visual_review") != "passed":
        limits.append(_tr("目視未確認", "not visually reviewed"))
    return limits


def _video_model_families(catalog):
    available = {
        recipe.get("model_family")
        for recipe in catalog.recipes.values()
        if isinstance(recipe.get("resolution"), (list, tuple))
    }
    preferred = ["wan21-infinitetalk", "wan22-s2v"]
    return [family for family in preferred if family in available] + sorted(
        family for family in available if family not in preferred
    )


def _video_resolutions(catalog, model_family):
    values = set()
    for recipe in catalog.recipes.values():
        resolution = recipe.get("resolution")
        # An experimental recipe can be present in the repository before its
        # experimental hardware profile is deliberately supplied. Do not let
        # that orphaned recipe change the ordinary guided menu or leave a
        # selected resolution with no profile to run.
        has_profile = any(profile.get("recipe") == recipe.get("id")
                          for profile in catalog.profiles.values())
        if (has_profile and recipe.get("model_family") == model_family
                and isinstance(resolution, (list, tuple))):
            values.add("%dp" % resolution[1])
    return sorted(values, key=lambda value: int(value[:-1]))


def _choice_status(catalog, env, model_family, resolution, recommended_id=None):
    result = select(catalog, env, model_family=model_family, resolution=resolution,
                    include_ineligible=True)
    best = result["best"]
    if best:
        if best["id"] == recommended_id:
            if best["fully_verified"]:
                return _tr("推奨", "recommended"), result
            return _tr("推奨・未検証", "recommended; unverified"), result
        if best["fully_verified"]:
            return _tr("生成可能・確認済み", "runnable; verified"), result
        return _tr("生成可能・未確認", "runnable; unverified"), result
    unavailable = _tr("利用不可", "unavailable")
    closest = result.get("closest")
    if closest and closest.get("blockers"):
        reasons = " / ".join(_blocker_text(item) for item in closest["blockers"])
        unavailable = "%s: %s" % (unavailable, reasons)
    return unavailable, result


def _prompt_choice(title, options, default_index, input_fn=input, output=None):
    output = output or sys.stdout
    print(title, file=output)
    for index, (label, detail) in enumerate(options, 1):
        suffix = "  [%s]" % detail if detail else ""
        print("  %d. %s%s" % (index, label, suffix), file=output)
    while True:
        try:
            answer = input_fn(_tr("選択 [%d]: ", "Choice [%d]: ")
                              % (default_index + 1)).strip()
        except (EOFError, KeyboardInterrupt):
            print(_tr("\n中止しました。", "\nCancelled."), file=output)
            return None
        if not answer:
            return default_index
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(_tr("1〜%dの番号を入力してください。",
                  "Enter a number from 1 to %d.") % len(options), file=output)


def _confirm(prompt, input_fn=input):
    try:
        return input_fn("%s [y/N]: " % prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("")
        return False


def _new_run_id(root):
    base = time.strftime("%Y%m%d-%H%M%S")
    candidate = base
    suffix = 2
    while (root / "outputs" / candidate).exists():
        candidate = "%s-%d" % (base, suffix)
        suffix += 1
    return candidate


def _background_command(args, profile_id, run_id):
    command = [
        sys.executable, str(args.root / "bin" / "narration-video-gen"),
        "--root", str(args.root),
    ]
    for directory in getattr(args, "profile_dir", []):
        command.extend(("--profile-dir", str(directory)))
    command.extend([
        "run",
        "--profile", profile_id, "--run-id", run_id,
        "--server", args.server, "--container", args.container,
        "--background-worker",
    ])
    for option, value in (("--image", args.image), ("--audio", args.audio),
                          ("--source-video", args.source_video),
                          ("--stages", args.stages)):
        if value:
            command.extend((option, str(value)))
    if args.length:
        command.extend(("--length", args.length))
    if args.frames is not None:
        command.extend(("--frames", str(args.frames)))
    for enabled, option in ((args.mask_only, "--mask-only"),
                            (args.accept_unverified, "--accept-unverified"),
                            (args.force, "--force")):
        if enabled:
            command.append(option)
    return command


def _length_cli_value(length_plan):
    """Translate the human-facing plan mode to the public CLI vocabulary."""
    return "short" if length_plan["mode"] == "test" else "full"


def _normalise_gpu_name(value):
    return "".join(character for character in (value or "").lower()
                   if character.isalnum())


_GENERATION_BY_COMPUTE_CAPABILITY = {
    "8.0": "ampere", "8.6": "ampere", "8.7": "ampere",
    "8.9": "ada", "9.0": "hopper", "10.0": "blackwell", "12.0": "blackwell",
}


def _reference_eta_state(profile, env, estimate):
    """Use a reference ETA only when its measured GPU is this GPU exactly.

    Current profiles carry one evidence record, rather than a GPU benchmark
    matrix. Treating an A4000 measurement as a 4090 or 5090 estimate would look
    precise while being misleading. A later matrix can deliberately define
    similarity fallbacks; until then, absence is more honest.
    """
    if not estimate.get("known"):
        return {}
    evidence = profile.get("evidence") or {}
    reference = evidence.get("gpu_model")
    actual_gpu = detect_mod.primary_gpu(env) or {}
    actual = actual_gpu.get("name")
    if not reference or not actual:
        return {}
    exact = _normalise_gpu_name(actual) in _normalise_gpu_name(reference)
    reference_generation = evidence.get("gpu_generation")
    if reference_generation is None and "rtxa4000" in _normalise_gpu_name(reference):
        # The legacy evidence was measured on the 140 W Ampere RTX A4000.
        reference_generation = "ampere"
    actual_generation = _GENERATION_BY_COMPUTE_CAPABILITY.get(
        str(actual_gpu.get("compute_capability")))
    if not exact and (not reference_generation or actual_generation != reference_generation):
        return {}
    reference_tdp = evidence.get("gpu_tdp_w", 140 if reference_generation == "ampere"
                                 and "rtxa4000" in _normalise_gpu_name(reference) else None)
    actual_tdp = actual_gpu.get("power_limit_w")
    return {
        "estimated_total_seconds": estimate["minutes"] * 60,
        "estimate_extrapolated": bool(estimate.get("extrapolated")),
        "eta_source": "reference",
        "eta_reference_gpu": reference,
        "eta_reference_match": "exact" if exact else "same-generation",
        "eta_reference_tdp_delta_w": (round(abs(actual_tdp - reference_tdp), 1)
                                      if isinstance(actual_tdp, (int, float))
                                      and isinstance(reference_tdp, (int, float)) else None),
    }


def _runtime_revision(root):
    """Fingerprint the built image, so a rebuild does not inherit old timings."""
    try:
        return plan_mod.runtime_build_identity(root)
    except (OSError, ValueError, KeyError):
        return None


def _print_full_length_estimate(profile, recipe, env, catalog, seconds, full_seconds,
                                recipe_options=None, pipeline=None, runtime=None):
    """After a shorter test run, say how long the full narration would take."""
    if not full_seconds or not seconds or seconds >= full_seconds - 0.5:
        return

    def reference(at_seconds):
        return plan_mod.estimate_runtime(profile, recipe, at_seconds, catalog=catalog)

    local = timings.estimate(env, profile, recipe, full_seconds,
                             reference_estimate=reference,
                             recipe_options=recipe_options, pipeline=pipeline,
                             runtime=runtime)
    if not local:
        return
    low, high = local["low_minutes"], local["high_minutes"]
    span = ("%d" % round(low) if round(low) == round(high)
            else "%d〜%d" % (round(low), round(high)))
    print(_tr("本編尺（%.1f秒）の推定: 約%s分",
              "Estimated for the full %.1f s narration: about %s min")
          % (full_seconds, span))


def _local_eta_state(args, profile, recipe, env, catalog, seconds,
                     recipe_options=None, pipeline=None, runtime=None):
    """Prefer this machine's own finished runs over the published reference."""
    def reference(at_seconds):
        return plan_mod.estimate_runtime(profile, recipe, at_seconds, catalog=catalog)

    local = timings.estimate(env, profile, recipe, seconds, reference_estimate=reference,
                             recipe_options=recipe_options, pipeline=pipeline,
                             runtime=runtime)
    if not local:
        return {}
    return {
        "estimated_total_seconds": local["minutes"] * 60,
        "estimated_total_seconds_low": local["low_minutes"] * 60,
        "estimated_total_seconds_high": local["high_minutes"] * 60,
        "estimate_extrapolated": local["extrapolated"],
        "eta_source": "local",
        "eta_local_samples": local["samples"],
    }


def _launch_background(args, profile, env, run_id, estimate):
    active = run_state.latest(args.root, running_only=True)
    if active and _pid_is_run(active):
        print(_tr("別の生成が実行中です。先にstatusで確認してください。",
                  "Another generation is running; check status first."),
              file=sys.stderr)
        return EXIT_ERROR
    if not args.dry_run and not _release_tts_backend_for_video(args.root):
        return EXIT_ERROR
    log = run_state.log_path(args.root, run_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    command = _background_command(args, profile["id"], run_id)
    state_values = {
        "status": "starting",
        "profile": profile["id"],
        "profile_settings": dict(profile.get("settings") or {}),
        "profile_requires": dict(profile.get("requires") or {}),
        "log": str(log.relative_to(args.root)),
    }
    state_values.update(_reference_eta_state(profile, env, estimate))
    run_state.write(args.root, run_id, **state_values)
    try:
        with log.open("ab") as stream:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=stream,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
    except OSError as exc:
        run_state.write(args.root, run_id, status="failed",
                        error_code="worker-start-failed", error=str(exc))
        raise
    run_state.write(args.root, run_id, pid=process.pid)
    print(_tr("\n生成をバックグラウンドで開始しました: %s",
              "\nGeneration started in the background: %s") % run_id)
    print(_tr("状態確認: ./bin/narration-video-gen status",
              "Status: ./bin/narration-video-gen status"))
    print(_tr("中止: ./bin/narration-video-gen cancel",
              "Cancel: ./bin/narration-video-gen cancel"))
    return EXIT_OK


def _remedies_for_state(args, state):
    """Resolve the next things to try for a failed run state."""
    code = state.get("error_code")
    if not code:
        return []
    context = dict(state.get("error_context") or {})
    # Prefer what the machine looked like when it failed over what it looks
    # like now: an idle machine after the fact explains nothing.
    env = {"memory": context.get("host_at_failure") or {}}
    if not env["memory"]:
        try:
            env = detect_mod.detect()
        except Exception:
            env = {}
    profile = None
    recipe = None
    if state.get("profile"):
        try:
            catalog = Catalog(args.root, profile_dirs=args.profile_dir)
            profile = catalog.profiles.get(state["profile"])
            if profile:
                recipe = catalog.recipe_for(profile)
        except Exception:
            profile = None
            recipe = None
    # The profile may have been edited after the run. Advice must use the
    # values that actually failed, not today's catalog entry.
    if state.get("profile_settings") is not None:
        profile = {
            "id": state.get("profile"),
            "settings": dict(state.get("profile_settings") or {}),
            "requires": dict(state.get("profile_requires") or {}),
        }
    if recipe is None and state.get("recipe_resolution"):
        recipe = {"resolution": list(state["recipe_resolution"])}
    return remedies.for_failure(
        code, profile, env, context, recipe, stage=state.get("stage"))


def _print_remedies(args, state):
    items = _remedies_for_state(args, state)
    if not items:
        return
    print(_tr("\n次に試すこと:", "\nWhat to try next:"))
    for item in items:
        if item.setting and item.suggested is not None:
            print(_tr("  - %s（%s: %s → %s）", "  - %s (%s: %s -> %s)")
                  % (item.action, item.setting, item.current, item.suggested))
        else:
            print("  - %s" % item.action)
        for line in textwrap.wrap(item.detail, width=76):
            print("      %s" % line)


def _host_state_now():
    """Memory and GPU as they are at this instant, for the failure record.

    Free memory at the moment of the failure is the number that explains it, and
    it cannot be recovered afterwards -- by the time anyone reads the run state
    the process has exited and its memory is back.
    """
    try:
        env = detect_mod.detect()
    except Exception:
        return {}
    memory = env.get("memory") or {}
    gpu = detect_mod.primary_gpu(env) or {}
    snapshot = {}
    for key in ("ram_gib", "ram_available_gib", "swap_gib"):
        if memory.get(key) is not None:
            snapshot[key] = memory[key]
    if gpu.get("vram_gib") is not None:
        snapshot["vram_gib"] = gpu["vram_gib"]
    return snapshot


def _worker_error(args, message, *, code="generation-failed", detail=None, context=None):
    if _tracks_run(args):
        if detail and detail != message:
            print(_tr("\n技術的な詳細:\n%s", "\nTechnical detail:\n%s") % detail,
                  file=sys.stderr)
        current = run_state.load(args.root, args.run_id) or {}
        if current.get("status") != "cancelled":
            context = dict(context or {})
            host = _host_state_now()
            if host:
                context["host_at_failure"] = host
            run_state.write(args.root, args.run_id, status="failed", error_code=code,
                            error=message, error_context=context)


def _state_for_args(args, *, running_only=False):
    return (run_state.load(args.root, args.run_id) if args.run_id
            else run_state.latest(args.root, running_only=running_only))


def _tracks_run(args):
    """Return whether this invocation owns persistent status/cancel state."""
    return bool(hasattr(args, "background_worker")
                and getattr(args, "run_id", None)
                and not getattr(args, "dry_run", False))


def _run_timing(state, now=None):
    """Return elapsed and estimated remaining seconds for a running state."""
    started = state.get("started_epoch")
    total = state.get("estimated_total_seconds")
    if not isinstance(started, (int, float)):
        return None, None
    elapsed = max(0, (time.time() if now is None else now) - started)
    remaining = max(0, total - elapsed) if isinstance(total, (int, float)) else None
    return elapsed, remaining


def _live_stage_remaining(state, now=None):
    """Return a remaining estimate made from this run's sampler progress."""
    progress = state.get("live_progress") or {}
    remaining = progress.get("remaining_seconds")
    observed = progress.get("observed_at_epoch")
    if not isinstance(remaining, (int, float)) or not isinstance(observed, (int, float)):
        return None
    return max(0, remaining - ((time.time() if now is None else now) - observed))


def _sampler_node_steps(workflow):
    """Return WanVideoSampler node ids and their configured denoising steps."""
    sampler_nodes = {}
    for node_id, node in workflow.items():
        if node.get("class_type") != "WanVideoSampler":
            continue
        try:
            steps = int(node.get("inputs", {}).get("steps"))
        except (TypeError, ValueError):
            continue
        if steps > 0:
            sampler_nodes[str(node_id)] = steps
    return sampler_nodes


def _workflow_input_node(workflow, node, input_name):
    """Resolve a workflow node referenced by one of another node's inputs."""
    if not isinstance(node, dict):
        return None
    reference = node.get("inputs", {}).get(input_name)
    if not isinstance(reference, (list, tuple)) or not reference:
        return None
    return workflow.get(str(reference[0]))


def _sampler_progress_maxima(workflow):
    """Return the websocket progress maximum expected for each sampler node.

    A regular WanVideoSampler reports one progress unit per denoising step.
    Wan 2.2 S2V FramePack invokes the same callback across every frame window,
    so its maximum is ``steps * ceil(num_frames / frame_window_size)``.
    """
    maxima = _sampler_node_steps(workflow)
    for node_id, steps in list(maxima.items()):
        sampler = workflow.get(node_id, {})
        embeds = _workflow_input_node(workflow, sampler, "image_embeds")
        inputs = embeds.get("inputs", {}) if embeds else {}
        if not (inputs.get("enable_framepack") and inputs.get("start_from_ref")):
            continue
        empty_embeds = _workflow_input_node(workflow, embeds, "embeds")
        try:
            frames = int(empty_embeds.get("inputs", {}).get("num_frames"))
            window = int(inputs.get("frame_window_size"))
        except (AttributeError, TypeError, ValueError):
            continue
        if frames > 0 and window > 0:
            maxima[node_id] = steps * ((frames + window - 1) // window)
    return maxima


def _record_live_progress(args, stage_id, stage_started, sampler_maxima, data):
    """Persist a WanVideoSampler progress event and its stage-local ETA."""
    if not _tracks_run(args):
        return
    try:
        value, maximum = int(data["value"]), int(data["max"])
    except (KeyError, TypeError, ValueError):
        return
    expected_maximum = sampler_maxima.get(str(data.get("node")))
    # WanVideoSampler itself reports loader/conditioning work before denoising
    # (for example 1260/1260).  Accept only the exact sampling counter: one
    # window for regular sampling, or all windows combined for S2V FramePack.
    if expected_maximum is None or maximum != expected_maximum:
        return
    if value < 1 or maximum < value:
        return
    observed = time.time()
    seconds_per_step = max(0, observed - stage_started) / value
    run_state.write(
        args.root, args.run_id,
        eta_source="live-stage",
        live_progress={
            "stage": stage_id,
            "value": value,
            "max": maximum,
            "seconds_per_step": round(seconds_per_step, 3),
            "remaining_seconds": round((maximum - value) * seconds_per_step, 1),
            "observed_at_epoch": observed,
        },
    )


def _short_minutes(seconds):
    if seconds < 60:
        return _tr("1分未満", "less than 1 min")
    return _tr("約%d分", "about %d min") % max(1, round(seconds / 60))


def _display_path(root, path):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _input_set_label(root, directory, source):
    if source == "bundled":
        character = narration.CHARACTERS.get(directory.name, {})
        return character.get("label_ja" if _ui_is_japanese() else "label_en",
                             directory.name)
    manifest_path = directory / "tts-manifest.json"
    try:
        character_id = json.loads(manifest_path.read_text(
            encoding="utf-8")).get("character")
        character = tts_service.list_characters(root).get(character_id, {})
        character_label = character.get(
            "label_ja" if _ui_is_japanese() else "label_en")
    except (OSError, ValueError, AttributeError):
        character_label = None
    name = _display_path(root, directory)
    return "%s — %s" % (character_label, name) if character_label else name


def _inspect_input_set(root, directory, source="user"):
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    images = sorted(path for path in directory.rglob("*")
                    if path.is_file() and path.suffix.lower() in image_suffixes)
    audio = sorted(path for path in directory.rglob("*")
                   if path.is_file() and path.suffix.lower() == ".wav")
    scripts = sorted(path for path in directory.rglob("*")
                     if path.is_file() and path.suffix.lower() in {".txt", ".md"})
    issues = []
    if not images:
        issues.append("no image")
    elif len(images) > 1:
        issues.append("multiple images")
    if not audio:
        issues.append("no WAV audio")
    elif len(audio) > 1:
        issues.append("multiple WAV files")
    duration = None
    if len(audio) == 1:
        try:
            duration = verify_mod.wav_duration(audio[0])
            if duration <= 0:
                issues.append("empty WAV audio")
        except (OSError, ValueError, EOFError, wave.Error):
            issues.append("invalid WAV audio")
    files = images + audio + scripts
    return {
        "name": _display_path(root, directory),
        "label": _input_set_label(root, directory, source),
        "source": source,
        "directory": directory,
        "image": images[0] if len(images) == 1 else None,
        "audio": audio[0] if len(audio) == 1 else None,
        "duration_seconds": duration,
        "modified_at": max((path.stat().st_mtime for path in files), default=0),
        "scripts": scripts,
        "issues": issues,
    }


def _input_set_candidates(root):
    """Return user input sets followed by every usable bundled character."""
    inputs_root = root / "inputs"
    user_sets = []
    if inputs_root.is_dir():
        for directory in sorted(path for path in inputs_root.iterdir() if path.is_dir()):
            candidate = _inspect_input_set(root, directory, source="user")
            if candidate["image"] or candidate["audio"] or candidate["scripts"] \
                    or candidate["issues"] != ["no image", "no WAV audio"]:
                user_sets.append(candidate)
    user_sets = sorted(user_sets, key=lambda item: (
        bool(item["issues"]), -item["modified_at"], item["name"]))
    for candidate in user_sets:
        candidate["latest"] = False
    complete = next((candidate for candidate in user_sets
                     if not candidate["issues"]), None)
    if complete:
        complete["latest"] = True
    bundled_root = root / "assets" / "characters"
    complete_user = [candidate for candidate in user_sets
                     if not candidate["issues"]]
    incomplete_user = [candidate for candidate in user_sets
                       if candidate["issues"]]
    if not bundled_root.is_dir():
        return complete_user + incomplete_user
    bundled = [_inspect_input_set(root, directory, source="bundled")
               for directory in sorted(path for path in bundled_root.iterdir()
                                       if path.is_dir())]
    return complete_user + bundled + incomplete_user


def _input_candidate_detail(candidate):
    if candidate["issues"]:
        return _input_issue_text(candidate["issues"])
    details = []
    if candidate.get("latest"):
        details.append(_tr("最新", "latest"))
    if candidate.get("source") == "bundled":
        details.append(_tr("同梱", "bundled"))
    if candidate.get("duration_seconds") is not None:
        details.append("%.1f%s" % (
            candidate["duration_seconds"], _tr("秒", " sec")))
    return "・".join(details) if _ui_is_japanese() else ", ".join(details)


def _input_issue_text(issues):
    labels = {
        "no image": "画像がありません",
        "multiple images": "画像は1件にしてください",
        "no WAV audio": "WAV音声がありません",
        "multiple WAV files": "WAV音声は1件にしてください",
        "invalid WAV audio": "WAV音声を読み込めません",
        "empty WAV audio": "WAV音声が空です",
    }
    return ", ".join(labels.get(issue, issue) if _ui_is_japanese() else issue
                     for issue in issues)


def _choose_input_set(root, input_fn=input, output=None):
    output = output or sys.stdout
    candidates = _input_set_candidates(root)
    if not candidates:
        print(_tr("入力素材がありません。", "No input set found."), file=output)
        print(_tr("inputs/<名前>/ に画像1件とWAV音声1件を置いてください。",
                  "Add one image and one WAV file under inputs/<name>/."), file=output)
        return None
    selected = 0
    if len(candidates) > 1:
        options = [(candidate["label"], _input_candidate_detail(candidate))
                   for candidate in candidates]
        selected = _prompt_choice(
            _tr("入力素材を選択:", "Choose input:"), options, 0,
            input_fn=input_fn, output=output)
        if selected is None:
            return None
    candidate = candidates[selected]
    if candidate["issues"]:
        print(_tr("入力素材 %s は使用できません: %s",
                  "Input %s is not ready: %s") % (
                      candidate["name"], _input_issue_text(candidate["issues"])), file=output)
        if "no WAV audio" in candidate["issues"] and candidate["scripts"]:
            print(_tr(
                "先に `narration-video-gen tts` で音声を作り、この入力セットへ採用してください。",
                "Run `narration-video-gen tts` first, then adopt the narration into this input set."),
                file=output)
        return None
    return str(candidate["image"]), str(candidate["audio"])


def _short_test_frames(recipe):
    """Return the recipe-owned 4n+1 frame count for a guided short test."""
    frames = recipe.get("short_test_frames")
    if not isinstance(frames, int) or frames < 5 or (frames - 1) % 4:
        raise ValueError(
            "recipe %s has an invalid short_test_frames" % recipe["id"])
    return frames


def _choose_run_length(recipe, full_seconds, requested=None, input_fn=input, output=None):
    output = output or sys.stdout
    if not math.isfinite(full_seconds) or full_seconds <= 0:
        raise ValueError(_tr("音声が空です。音声の入ったWAVファイルを選んでください。",
                             "The audio is empty. Choose a WAV file containing audio."))
    fps = recipe["fps"]
    full_frames = verify_mod.frames_for_audio(full_seconds, fps)
    test_frames = _short_test_frames(recipe)
    test_seconds = test_frames / fps
    japanese = _ui_is_japanese()
    full_label = "フル尺" if japanese else "Full length"
    test_label = "短尺(テスト)" if japanese else "Short test"
    if full_seconds <= test_seconds:
        return {"mode": "full", "frames": full_frames, "seconds": full_seconds,
                "label": full_label}
    options = [
        (test_label, (("推奨・約%.1f秒" if japanese else "recommended; %.1f seconds")
                      % test_seconds)),
        (full_label, (("%.1f秒" if japanese else "%.1f seconds") % full_seconds)),
    ]
    if requested == "short":
        selected = 0
    elif requested == "full":
        selected = 1
    else:
        selected = _prompt_choice(
            "生成する長さ:" if japanese else "Generation length:",
            options, 0, input_fn=input_fn, output=output)
        if selected is None:
            return None
    if selected == 0:
        return {"mode": "test", "frames": test_frames, "seconds": test_seconds,
                "label": test_label}
    return {"mode": "full", "frames": full_frames, "seconds": full_seconds,
            "label": full_label}


def _render_generation_summary(root, recipe, image, audio, seconds, estimate,
                               length_label="Full length", output=None):
    output = output or sys.stdout
    print(_tr("\n生成", "\nGeneration"), file=output)
    print(_tr("モデル   : %s", "Model      : %s")
          % _model_label(recipe["model_family"]), file=output)
    print(_tr("解像度   : %dp", "Resolution : %dp")
          % recipe["resolution"][1], file=output)
    print(_tr("画像     : %s", "Image      : %s")
          % _display_path(root, Path(image)), file=output)
    print(_tr("音声     : %s", "Audio      : %s")
          % _display_path(root, Path(audio)), file=output)
    print(_tr("長さ     : %s（%.1f秒）", "Length     : %s (%.1f seconds)")
          % (length_label, seconds), file=output)
    if estimate.get("known"):
        print(_tr("推定時間 : 約%d分", "Estimate   : about %d minutes")
              % estimate["minutes"], file=output)


def _interactive_select_request(catalog, env, input_fn=input, output=None):
    output = output or sys.stdout
    recommended = select(catalog, env)["best"]
    recommended_id = recommended["id"] if recommended else None
    recommended_model = recommended["model_family"] if recommended else None
    recommended_resolution = recommended["resolution"] if recommended else None
    families = _video_model_families(catalog)
    default_model = families.index(recommended_model) if recommended_model in families else 0

    print(_tr("作成内容を選択\n", "Choose what to create\n"), file=output)
    model_options = []
    for family in families:
        if family == recommended_model:
            detail = (_tr("推奨", "recommended") if recommended["fully_verified"]
                      else _tr("推奨・未検証", "recommended; unverified"))
        else:
            detail = ""
        model_options.append((_model_label(family), detail))
    model_index = _prompt_choice(_tr("モデル:", "Model:"), model_options, default_model,
                                 input_fn=input_fn, output=output)
    if model_index is None:
        return None
    model_family = families[model_index]

    resolutions = _video_resolutions(catalog, model_family)
    resolution_options = []
    first_available = None
    for index, resolution in enumerate(resolutions):
        status, choice_result = _choice_status(
            catalog, env, model_family, resolution, recommended_id)
        if choice_result["best"] and first_available is None:
            first_available = index
        resolution_options.append((resolution, status))
    if model_family == recommended_model and recommended_resolution in resolutions:
        default_resolution = resolutions.index(recommended_resolution)
    else:
        default_resolution = first_available if first_available is not None else 0
    print("", file=output)
    resolution_index = _prompt_choice(_tr("解像度:", "Resolution:"), resolution_options,
                                      default_resolution, input_fn=input_fn, output=output)
    if resolution_index is None:
        return None
    return model_family, resolutions[resolution_index]


def _interactive_plan_profile(catalog, env, root, input_fn=input, output=None):
    """Choose and save a profile when a human starts directly from plan."""
    output = output or sys.stdout
    request = _interactive_select_request(
        catalog, env, input_fn=input_fn, output=output)
    if request is None:
        return None, True
    model_family, resolution = request
    result = select(
        catalog, env, model_family=model_family, resolution=resolution,
        include_ineligible=True)
    selected = result["best"]
    if selected is None:
        print(_tr("\nこの組み合わせは利用できません。",
                  "\nThis combination is unavailable."), file=output)
        closest = result.get("closest")
        if closest:
            _print_blockers(closest["blockers"], output=output)
        return None, False
    # ``select`` returns a display/ranking entry, not the complete catalog
    # profile.  ``cmd_plan`` must evaluate the latter (it includes platform,
    # requirements, and evidence) after the guided choice.
    profile = catalog.profiles[selected["id"]]
    selection_state.save(
        root, profile["id"], selected["model_family"], selected["resolution"])
    return profile, False


def _interactive_plan_target(catalog, env, root, input_fn=input, output=None):
    """Reuse the saved plan target or let an interactive user replace it."""
    output = output or sys.stdout
    saved = selection_state.load(root)
    saved_profile = catalog.profiles.get(saved["profile"]) if saved else None
    if saved_profile is None:
        return _interactive_plan_profile(catalog, env, root, input_fn=input_fn, output=output)

    recipe = catalog.recipe_for(saved_profile, saved.get("recipe_options") or [])
    face_enabled = "face-detailer" in (recipe.get("pipeline_stages") or [])
    current = "%s / %dp" % (_model_label(recipe["model_family"]), recipe["resolution"][1])
    if "musetalk" in (saved.get("recipe_options") or []):
        current += " / " + ("MuseTalk + VACE" if face_enabled else "MuseTalk")
    else:
        current += " / " + _tr("標準", "Standard")
    current += " / Face Detailer %s" % ("ON" if face_enabled else "OFF")
    current += " / %s fps" % (60 if "rife" in (recipe.get("pipeline_stages") or [])
                               else recipe["fps"])
    print(_tr("現在の構成: %s\n", "Current selection: %s\n") % current, file=output)
    selected = _prompt_choice(
        _tr("plan の操作:", "Plan action:"),
        [
            (_tr("この構成を確認・準備する", "Review and prepare this selection"),
             _tr("推奨", "recommended")),
            (_tr("作成内容を選び直す", "Choose a different configuration"), ""),
        ],
        0, input_fn=input_fn, output=output)
    if selected is None:
        return None, True
    if selected == 0:
        return saved_profile, False
    return _interactive_plan_profile(catalog, env, root, input_fn=input_fn, output=output)


def _available_alternatives(catalog, env, requested_model, requested_resolution):
    alternatives = []
    for family in _video_model_families(catalog):
        for resolution in _video_resolutions(catalog, family):
            if (family, resolution) == (requested_model, requested_resolution):
                continue
            result = select(catalog, env, model_family=family, resolution=resolution)
            if result["best"]:
                alternatives.append({
                    "model_family": family,
                    "model": _model_label(family),
                    "resolution": resolution,
                    "profile": result["best"]["id"],
                    "fully_verified": result["best"]["fully_verified"],
                })
    return alternatives


def _report_no_profile(catalog, env, args, output=None):
    """Explain why nothing can run here, instead of naming another command."""
    output = output or sys.stderr
    if args.json:
        return _cli_error(args, _tr("指定した条件で実行できる構成がありません。",
                                   "No configuration can run with the selected requirements."), EXIT_NO_MATCH)
    print(_tr("このマシンで実行できる構成がありません。",
              "No configuration can run on this machine."), file=output)
    result = select(catalog, env,
                    recipe=getattr(args, "recipe", None),
                    model_family=_normalise_model_family(getattr(args, "model", None)),
                    resolution=getattr(args, "resolution", None),
                    include_ineligible=True)
    closest = result.get("closest")
    if closest:
        print(_tr("最も近い構成 %s に不足しているもの:",
                  "Closest configuration %s is missing:") % closest["id"], file=output)
        _print_blockers(closest["blockers"], output=output)
    return EXIT_NO_MATCH


def _resolve_profile(catalog, env, args):
    """Return the profile to act on, either named explicitly or auto-selected."""
    if getattr(args, "profile", None):
        if args.profile not in catalog.profiles:
            raise CatalogError(_tr("プロファイル %r は存在しません。`narration-video-gen list` を確認してください。",
                                   "unknown profile %r; try `narration-video-gen list`")
                               % args.profile)
        return catalog.profiles[args.profile]
    has_filters = any((
        getattr(args, "recipe", None),
        getattr(args, "model", None),
        getattr(args, "resolution", None),
    ))
    if not has_filters:
        saved = selection_state.load(args.root)
        if saved:
            profile = catalog.profiles.get(saved["profile"])
            if profile is None:
                raise CatalogError(
                    _tr("保存された選択 %r は存在しません。`narration-video-gen plan` で選び直してください。",
                        "saved selection %r no longer exists; run "
                        "`narration-video-gen plan` again")
                    % saved["profile"])
            _eligible, blockers, _ = evaluate(profile, env)
            recipe = catalog.recipe_for(profile, _saved_recipe_options(args.root, profile))
            blockers = _without_stale_download_budget(
                blockers, args.root, catalog, profile, env, recipe)
            if blockers:
                raise CatalogError(
                    _tr("保存された選択 %r は現在のマシンに適合しません: %s。"
                        "`narration-video-gen plan` で選び直してください。",
                        "saved selection %r no longer matches this machine: %s; run "
                        "`narration-video-gen plan` again")
                    % (saved["profile"],
                       "; ".join(_blocker_text(blocker) for blocker in blockers)))
            return profile
    result = select(catalog, env, recipe=getattr(args, "recipe", None),
                    model_family=_normalise_model_family(getattr(args, "model", None)),
                    resolution=getattr(args, "resolution", None),
                    include_ineligible=True)
    candidates = []
    if result["best"]:
        profile = catalog.profiles[result["best"]["id"]]
        candidates.append((profile, catalog.recipe_for(profile)))
    # A profile's free_disk_gib_min is the capacity required to download its
    # weights from scratch. When every required weight is already present, use
    # the plan's live disk calculation instead. This must happen while finding
    # a profile as well as immediately before plan/run, otherwise saved and
    # filtered selections still fail before the later check can run.
    for entry in result["ineligible"]:
        profile = catalog.profiles[entry["id"]]
        recipe = catalog.recipe_for(profile)
        blockers = _without_stale_download_budget(
            entry["blockers"], args.root, catalog, profile, env, recipe)
        if not blockers:
            candidates.append((profile, recipe))
    if not candidates:
        return None
    return max(candidates, key=lambda row: confidence_key(*row))[0]


def _saved_recipe_options(root, profile):
    saved = selection_state.load(root)
    profile_id = profile.get("_base_profile_id", profile["id"])
    if not saved or saved.get("profile") != profile_id:
        return []
    return list(saved.get("recipe_options") or [])


def _local_profile(profile, catalog, env, recipe_options, *, enabled=True):
    """Resolve the exact machine+plan calibration without changing the catalog."""
    recipe = catalog.recipe_for(profile, recipe_options)
    if not enabled:
        return profile, recipe, None
    effective, metadata = calibration_state.resolve(
        profile, recipe, recipe_options, env)
    return effective, catalog.recipe_for(effective, recipe_options), metadata


def _attach_local_profile_dir(args, profile):
    """Make a calibrated profile addressable by a background child process."""
    if not profile.get("_local_calibration"):
        return
    directory = Path(profile["_path"]).parent
    if directory not in args.profile_dir:
        args.profile_dir.append(directory)


def _choose_plan_recipe_options(catalog, profile, args, interactive):
    """Resolve and persist the pipeline choices made during plan."""
    interactive = interactive and getattr(args, "advanced", False)
    recipe = catalog.recipe_for(profile)
    available = recipe.get("pipeline_options") or {}
    saved = selection_state.load(args.root)
    saved_matches = bool(saved and saved.get("profile") == profile["id"])
    current = list(saved.get("recipe_options") or []) if saved_matches else []
    requested_lip_sync = getattr(args, "lip_sync_enhancement", None)
    requested_face = getattr(args, "face_detailer", None)
    requested_interpolation = getattr(args, "frame_interpolation", None)
    if (saved_matches and saved.get("plan_configured") and not interactive
            and requested_lip_sync is None and requested_face is None
            and requested_interpolation is None):
        return current

    if requested_lip_sync is not None:
        if requested_lip_sync == "musetalk" and "musetalk" not in available:
            raise ValueError("MuseTalk lip-sync enhancement is available only for Wan 2.1")
        chosen = ["musetalk"] if requested_lip_sync == "musetalk" else []
    elif interactive and "musetalk" in available:
        selected = _prompt_choice(
            _tr("リップシンク:", "Lip sync:"),
            [
                (_tr("標準", "Standard"), _tr("推奨", "recommended")),
                ("MuseTalk + VACE", _tr("実験的", "experimental")),
            ],
            1 if "musetalk" in current else 0,
        )
        if selected is None:
            return None
        chosen = ["musetalk"] if selected == 1 else []
    else:
        # A model with no optional pipeline (currently Wan 2.2) still needs
        # its profile persisted.  Otherwise a command such as
        # ``plan --model wan22 --resolution 480p`` prepares Wan 2.2 but a
        # following bare ``run`` silently reuses the previous model.
        chosen = [option for option in current if option == "musetalk"]

    profile_default = (profile.get("settings") or {}).get(
        "face_detailer_enabled", True)
    saved_face = next((option for option in current
                       if option in ("face-detailer-on", "face-detailer-off")), None)
    face_default = ({"face-detailer-on": True, "face-detailer-off": False}[saved_face]
                    if saved_face else profile_default)
    if requested_face is not None:
        face_enabled = requested_face == "on"
    elif interactive:
        selected = _prompt_choice(
            _tr("顔補正:", "Face Detailer:"),
            [
                (_tr("ON（VACEで顔を精細化）", "On (VACE face refinement)"),
                 _tr("高品質・高メモリ", "higher quality and memory")),
                (_tr("OFF（生成結果をそのまま使用）", "Off (keep generated face)"),
                 _tr("高速・省メモリ", "faster and lower memory")),
            ],
            0 if face_default else 1,
        )
        if selected is None:
            return None
        face_enabled = selected == 0
    else:
        face_enabled = face_default
    chosen.append("face-detailer-on" if face_enabled else "face-detailer-off")

    if requested_interpolation is not None:
        interpolation_enabled = requested_interpolation == "on"
    else:
        saved_interpolation = next((
            option for option in current
            if option in ("frame-interpolation-on", "frame-interpolation-off")
        ), None)
        interpolation_enabled = ({
            "frame-interpolation-on": True,
            "frame-interpolation-off": False,
        }[saved_interpolation] if saved_interpolation else True)
    # Keep the standard ON path implicit so existing plan/calibration identities
    # remain stable.  Only the non-default opt-out needs to be persisted.
    if not interpolation_enabled:
        chosen.append("frame-interpolation-off")
    if not args.check and not args.json:
        selection_state.save(
            args.root, profile["id"], recipe["model_family"],
            "%dp" % recipe["resolution"][1], recipe_options=chosen,
            plan_configured=True)
    return chosen


def _relative_to(path, parent):
    """Return ``path`` below ``parent``, or ``None`` when it is elsewhere."""
    try:
        return path.relative_to(parent)
    except ValueError:
        return None


def _stage_input(root, value, loader=False):
    """Make a host input addressable inside the ComfyUI container.

    ``assets`` and ``outputs`` are the only host directories mounted by the
    Compose service. Inputs elsewhere are copied into the ignored outputs
    staging directory. LoadImage/LoadAudio use ComfyUI's annotated ``[output]``
    name for files there; post-stages receive an absolute container path.
    """
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError("input not found: %s" % value)

    assets = (root / "assets").resolve()
    outputs = (root / "outputs").resolve()
    relative = _relative_to(path, assets)
    location = "input"
    if relative is None:
        relative = _relative_to(path, outputs)
        location = "output"
    if relative is None:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        relative = Path(".narration-video-gen-inputs") / (digest.hexdigest()[:16] + "-" + path.name)
        staged = outputs / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        if not staged.is_file():
            shutil.copy2(path, staged)
        location = "output"

    name = relative.as_posix()
    if loader:
        return name if location == "input" else "%s [output]" % name
    container_root = "/opt/ComfyUI/input" if location == "input" else "/opt/ComfyUI/output"
    return str(Path(container_root) / relative)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _base_image_reference(root):
    """Return the pinned base image, by digest, from the container lock file."""
    from .compat import load_yaml_file

    lock = load_yaml_file(root / "manifests" / "containers.lock.yaml")
    base = lock.get("base_image") or {}
    reference, digest = base.get("reference"), base.get("digest")
    if not reference or not digest:
        raise CatalogError(_tr("manifests/containers.lock.yaml にベースイメージの指定がありません。",
                               "manifests/containers.lock.yaml has no pinned base image."))
    return "%s@%s" % (reference, digest)


def _measurement_image(root):
    """Return an image already on this machine that the probe can run in.

    The runtime image ``plan`` builds is preferred: it is the one generation
    uses. The base it was built from carries the same torch and is used when the
    runtime image has not been built yet, or the other way round when a docker
    prune has removed the separately tagged base.
    """
    for image in (plan_mod.runtime_image_plan(root).get("images") or []):
        if image.get("name") == "comfy" and image.get("present"):
            return image["image"]
    base = _base_image_reference(root)
    return base if measure_mod.image_present(base) else None


def _measurement_unavailable(env, japanese, english):
    """Record why the measurement did not run, without disturbing --json stdout."""
    message = _tr(japanese, english)
    env.setdefault("warnings", []).append(message)
    print(message, file=sys.stderr)
    return None


def _measure_environment(args, env):
    """Run the measured probe and attach the result to ``env``.

    Returns the measurement record, or None when it could not be run.
    """
    if args.measure_seconds <= 0:
        raise ValueError("--measure-seconds must be greater than zero")

    # Hold the lock across the check and the run, so two measurements started at
    # the same time cannot both decide the GPU is free.
    with measure_mod.exclusive():
        return _run_measurement(args, env)


def _gpu_is_busy(args):
    """Whether anything else is using the GPU the probe would saturate."""
    active = run_state.latest(args.root, running_only=True)
    if active and _pid_is_run(active):
        return True
    # The generation service exposes every GPU, so any busy card disturbs it.
    return bool(measure_mod.compute_processes())


def _run_measurement(args, env):
    """Fetch the image if needed, then measure. The caller holds the lock."""

    def refuse_busy():
        return _measurement_unavailable(
            env, "GPUが使用中です。空いてから測定してください。",
            "The GPU is in use; measure once it is free.")

    if _gpu_is_busy(args):
        return refuse_busy()

    image = _measurement_image(args.root)
    if not image:
        # --json promises a parseable document on stdout, which a prompt and
        # docker's pull progress would break.
        if args.json or not (sys.stdin.isatty() and sys.stdout.isatty()):
            return _measurement_unavailable(
                env, "測定に使うイメージがありません。先に plan を実行してください。",
                "The image the measurement uses is not present; run plan first.")
        print(_tr("測定にはイメージの取得が必要です（約9.4GB）。",
                  "The measurement needs an image (about 9.4 GB)."))
        if not _confirm(_tr("取得しますか？", "Download it?")):
            return None
        image = _base_image_reference(args.root)
        measure_mod.pull(image)
        # The download takes minutes, which is long enough for a generation to
        # have started since the check above.
        if _gpu_is_busy(args):
            return refuse_busy()

    record = measure_mod.measure(image, seconds=args.measure_seconds)
    record["assessment"] = measure_mod.assess(env, record)
    return record


def _render_measurement(record):
    assessment = record.get("assessment") or {}
    print("")
    if record.get("tflops_fp16") is not None:
        print(_tr("持続 fp16        : %.1f TFLOPS", "sustained fp16      : %.1f TFLOPS")
              % record["tflops_fp16"])
    if record.get("temperature_max_c") is not None:
        print(_tr("温度             : %.0f℃（上昇 %.0f℃）",
                  "temperature         : %.0f C (rose %.0f C)")
              % (record["temperature_max_c"], record.get("temperature_rise_c") or 0))
    if record.get("clocks_sm_mhz") is not None:
        if record.get("clock_ratio_pct") is not None:
            print(_tr("クロック         : %.0f MHz（定格の %d%%）",
                      "clock               : %.0f MHz (%d%% of rated)")
                  % (record["clocks_sm_mhz"], record["clock_ratio_pct"]))
        else:
            print(_tr("クロック         : %.0f MHz", "clock               : %.0f MHz")
                  % record["clocks_sm_mhz"])
    warnings = assessment.get("warnings") or []
    if warnings:
        print("")
        _bullet([_tr(item["ja"], item["en"]) for item in warnings])


def cmd_detect(args):
    env = _env_for(args)
    if getattr(args, "measure", False):
        record = _measure_environment(args, env)
        if record is not None:
            env["measurement"] = record
            path = args.root / "results" / "measurements" / (
                time.strftime("%Y%m%d-%H%M%S") + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    if args.out:
        Path(args.out).write_text(json.dumps(env, indent=2) + "\n", encoding="utf-8")

    def render(env):
        gpu = detect_mod.primary_gpu(env)
        print("platform            : %s (kernel %s)" % (env["platform"], env["kernel"]))
        if gpu:
            print("gpu                 : %s, %.1f GiB VRAM, driver %s"
                  % (gpu["name"], gpu["vram_gib"], gpu["driver_version"]))
        else:
            print("gpu                 : none visible")
        mem = env["memory"]
        print("ram / swap          : %.1f GiB / %.1f GiB" % (mem["ram_gib"], mem["swap_gib"]))
        if "windows_host" in env:
            print("physical windows ram: %.1f GiB" % env["windows_host"]["physical_ram_gib"])
        for name, disk in env.get("disk", {}).items():
            print("%-20s: %.1f GiB free of %.1f GiB"
                  % ("disk (%s)" % name, disk["free_gib"], disk["total_gib"]))
        con = env["container"]
        print("docker              : %s%s"
              % (con["docker_server_version"] or "not available",
                 ", nvidia runtime" if con["nvidia_runtime"] else ""))
        if env["warnings"]:
            print("\nwarnings:")
            _bullet(env["warnings"])
        if env.get("measurement"):
            _render_measurement(env["measurement"])

    _emit(args, env, render)
    return EXIT_OK


def cmd_select(args):
    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    env = _env_for(args)
    if args.profile and any((args.recipe, args.model, args.resolution)):
        raise ValueError("--profile cannot be combined with --recipe, --model, or --resolution")
    if args.profile and args.profile not in catalog.profiles:
        raise CatalogError(_tr("プロファイル %r は存在しません。`narration-video-gen list` を確認してください。",
                               "unknown profile %r; try `narration-video-gen list`")
                           % args.profile)

    model_family = _normalise_model_family(args.model)
    resolution = args.resolution
    interactive = (
        not args.json and sys.stdin.isatty() and sys.stdout.isatty()
        and not any((args.profile, args.recipe, args.model, args.resolution, args.explain))
    )
    if interactive:
        request = _interactive_select_request(catalog, env)
        if request is None:
            return EXIT_CANCELLED
        model_family, resolution = request

    include_ineligible = args.explain or bool(args.profile or (model_family and resolution))
    result = select(
        catalog, env, recipe=args.recipe, model_family=model_family,
        resolution=resolution, profile_id=args.profile,
        include_ineligible=include_ineligible,
    )
    machine_best = select(catalog, env)["best"]
    result["machine_recommendation"] = machine_best
    best = result["best"]
    closest = result.get("closest")
    selected_entry = best or closest
    display_model = model_family or (selected_entry and selected_entry["model_family"])
    display_resolution = resolution or (selected_entry and selected_entry["resolution"])
    result["request"] = {
        "mode": ("interactive" if interactive else "profile" if args.profile
                 else "direct" if any((args.recipe, args.model, args.resolution))
                 else "automatic"),
        "model_family": display_model,
        "model": _model_label(display_model) if display_model else None,
        "resolution": display_resolution,
        "profile": args.profile,
    }
    result["alternatives"] = (
        _available_alternatives(catalog, env, display_model, display_resolution)
        if not best and display_model and display_resolution and not args.profile else []
    )
    save_requested = not args.json and (
        interactive or any((args.profile, args.recipe, args.model, args.resolution))
    )
    result["selection_state"] = {"saved": False, "cleared": False, "path": None}
    if save_requested:
        if best:
            state_path = selection_state.save(
                args.root, best["id"], best["model_family"], best["resolution"])
            result["selection_state"] = {
                "saved": True, "cleared": False, "path": str(state_path)}
        else:
            selection_state.clear(args.root)
            result["selection_state"]["cleared"] = True
    result["environment"] = {
        "platform": env["platform"],
        "gpu": detect_mod.primary_gpu(env),
        "memory": env["memory"],
        "windows_host": env.get("windows_host"),
    }

    def render(result):
        best = result["best"]
        request = result["request"]
        model_label = request["model"] or _tr("自動", "automatic")
        resolution_label = request["resolution"] or _tr("自動", "automatic")
        print(_tr("\n選択結果", "\nSelection"))
        print(_tr("モデル   : %s", "Model      : %s") % model_label)
        print(_tr("解像度   : %s", "Resolution : %s") % resolution_label)
        if best:
            if best["fully_verified"]:
                status = (_tr("推奨", "recommended")
                          if machine_best and best["id"] == machine_best["id"]
                          else _tr("生成可能（確認済み）", "runnable (verified)"))
            else:
                status = (_tr("推奨（未検証）", "recommended (unverified)")
                          if machine_best and best["id"] == machine_best["id"]
                          else _tr("生成可能（未確認）", "runnable (unverified)"))
            print(_tr("状態     : %s", "Status     : %s") % status)
            if machine_best and best["id"] != machine_best["id"]:
                print(_tr("推奨     : %s / %s", "Recommended: %s / %s") % (
                    _model_label(machine_best["model_family"]), machine_best["resolution"]))
            if args.explain and best["notes"]:
                print(_tr("\n検証上の制限:", "\nVerification limits:"))
                _bullet(best["notes"])
            print(_tr("\n次の手順:", "\nNext:"))
            if result["selection_state"]["saved"]:
                print("./bin/narration-video-gen plan")
            else:
                print("./bin/narration-video-gen plan --profile %s"
                      % best["id"])
        else:
            print(_tr("状態     : 利用不可", "Status     : unavailable"))
            if closest:
                print(_tr("\n理由:", "\nWhy:"))
                _print_blockers(closest["blockers"])
            else:
                print(_tr("\n対応する構成がありません。",
                          "\nNo matching configuration."))
            if result["alternatives"]:
                print(_tr("\n生成可能な選択肢:", "\nRunnable alternatives:"))
                for alternative in result["alternatives"]:
                    suffix = "" if alternative["fully_verified"] else _tr("（未検証）", " (unverified)")
                    print("  - %s / %s%s" % (
                        alternative["model"], alternative["resolution"], suffix))
        if args.explain and result.get("ineligible"):
            print(_tr("\n確認した内部構成:", "\nInternal configurations:"))
            for entry in result["ineligible"]:
                print("  %s" % entry["id"])
                _print_blockers(entry["blockers"], prefix="      - ")

    _emit(args, result, render)
    return EXIT_OK if result["best"] else EXIT_NO_MATCH


def cmd_list(args):
    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    rows = []
    for profile in catalog.sorted_profiles():
        recipe = catalog.recipe_for(profile)
        ev = profile["evidence"]
        rows.append({
            "id": profile["id"],
            "platform": profile["platform"],
            "model_family": recipe["model_family"],
            "resolution": "%dp" % recipe["resolution"][1],
            "vram_gib": profile["requires"].get("gpu_vram_gib_min"),
            "status": profile["status"],
            "duration_class": ev["duration_class"],
            "gpu_evidence": ev["gpu_evidence"],
            "visual_review": ev["visual_review"],
        })

    def render(rows):
        header = ("%-42s %-13s %-6s %-5s %-16s %-6s %-19s %s"
                  % ("PROFILE", "PLATFORM", "RES", "VRAM", "STATUS", "LEN",
                     "GPU EVIDENCE", "VISUAL"))
        print(header)
        print("-" * len(header))
        for row in rows:
            print("%-42s %-13s %-6s %-5s %-16s %-6s %-19s %s"
                  % (row["id"], row["platform"], row["resolution"],
                     row["vram_gib"], row["status"], row["duration_class"],
                     row["gpu_evidence"], row["visual_review"]))

    _emit(args, rows, render)
    return EXIT_OK


def cmd_show(args):
    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    profile_id = args.profile
    if profile_id is None:
        saved = selection_state.load(args.root)
        profile_id = saved["profile"] if saved else None
    if profile_id is None:
        print(_tr("まだ構成を選んでいません。先に `narration-video-gen plan` を実行してください。",
                  "No configuration chosen yet; run `narration-video-gen plan` first."),
              file=sys.stderr)
        return EXIT_NO_MATCH
    if profile_id not in catalog.profiles:
        print(_tr("プロファイル %r は存在しません。", "unknown profile %r") % profile_id,
              file=sys.stderr)
        return EXIT_ERROR
    profile = catalog.profiles[profile_id]
    payload = {"profile": profile, "recipe": catalog.recipe_for(profile)}

    def render(payload):
        profile, recipe = payload["profile"], payload["recipe"]
        print("# %s" % profile["id"])
        print("%s\n" % (profile.get("summary") or ""))
        print("platform : %s" % profile["platform"])
        print("recipe   : %s  (%dx%d, %s fps)"
              % (recipe["id"], recipe["resolution"][0], recipe["resolution"][1],
                 recipe["fps"]))
        print("status   : %s" % profile["status"])
        print("\nrequires:")
        _bullet(["%s: %s" % kv for kv in (profile["requires"] or {}).items()])
        print("\nsettings:")
        _bullet(["%s: %s" % kv for kv in (profile["settings"] or {}).items()])
        print("\nevidence:")
        _bullet(["%s: %s" % kv for kv in (profile["evidence"] or {}).items()])
        if profile.get("timing"):
            print("\ntiming:")
            _bullet(["%s: %s" % kv for kv in profile["timing"].items()])
        if profile.get("limitations"):
            print("\nlimitations:")
            _bullet(profile["limitations"])

    _emit(args, payload, render)
    return EXIT_OK


def cmd_plan(args):
    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    env = _env_for(args)
    interactive = not args.json and not args.check and sys.stdin.isatty() and sys.stdout.isatty()
    explicit_selection = any((args.profile, args.recipe, args.model, args.resolution))
    profile = None
    if interactive and not explicit_selection:
        profile, cancelled = _interactive_plan_target(catalog, env, args.root)
        if profile is None:
            return EXIT_CANCELLED if cancelled else EXIT_NO_MATCH
    if profile is None:
        profile = _resolve_profile(catalog, env, args)
    if profile is None:
        return _report_no_profile(catalog, env, args)
    recipe_options = _choose_plan_recipe_options(catalog, profile, args, interactive)
    if recipe_options is None:
        return EXIT_CANCELLED
    profile, recipe, local_calibration = _local_profile(
        profile, catalog, env, recipe_options, enabled=not bool(args.profile))
    _attach_local_profile_dir(args, profile)
    ok, blockers, notes = evaluate(profile, env)
    estimate_seconds = args.seconds if args.seconds is not None else 30
    payload = plan_mod.build_plan(args.root, catalog, profile, env,
                                  args.root / "models", estimate_seconds,
                                  recipe=recipe)
    blockers = _without_stale_download_budget(
        blockers, args.root, catalog, profile, env, recipe, preparation=payload)
    if payload["disk"]["sufficient"] is False and not any(
            getattr(item, "fields", {}).get("key") == "free_disk_gib_min" for item in blockers):
        blockers.append(_tr("ディスクの空き容量が不足しています。%.1f GiB以上の空きを確保してください。",
                            "Not enough disk space. Free at least %.1f GiB before preparing.")
                        % payload["disk"]["required_free_gib"])
    ok = not blockers
    payload["eligible"] = ok
    payload["blockers"] = blockers
    payload["notes"] = notes
    payload["local_calibration"] = local_calibration

    def render(payload):
        print(_tr("準備状況\n", "Preparation\n"))
        print(_tr("モデル   : %s / %dp", "Model    : %s / %dp")
              % (_model_label(recipe["model_family"]), recipe["resolution"][1]))
        face_detailer = ("ON" if "face-detailer" in payload["pipeline_stages"]
                         else "OFF")
        lip_sync = (("MuseTalk + VACE" if face_detailer == "ON" else "MuseTalk")
                    if "musetalk" in recipe_options else _tr("標準", "Standard"))
        print(_tr("リップシンク: %s", "Lip sync : %s") % lip_sync)
        print(_tr("顔補正   : %s", "Face refinement: %s") % face_detailer)
        if "rife" in payload["pipeline_stages"]:
            interpolation = _tr("ON（60fps）", "ON (60 fps)")
        else:
            interpolation = _tr("OFF（%sfps）", "OFF (%s fps)") % recipe["fps"]
        print(_tr("フレーム補間: %s", "Frame interpolation: %s") % interpolation)
        if local_calibration:
            print(_tr("ローカル調整: 適用（blocks_to_swap=%s）",
                      "Local tuning: applied (blocks_to_swap=%s)")
                  % (profile.get("settings") or {}).get("blocks_to_swap"))
        print(_tr("[%s] 動作環境", "[%s] Machine") % ("OK" if ok else "NG"))
        dl = payload["downloads"]
        if _models_missing(dl):
            if dl["missing_gib"] < 0.1:
                print(_tr("[--] モデル: 一部未取得（0.1 GiB未満）",
                          "[--] Models: incomplete (less than 0.1 GiB)"))
            else:
                print(_tr("[--] モデル: 未取得（%.1f GiB）",
                      "[--] Models: not downloaded (%.1f GiB)")
                  % dl["missing_gib"])
        else:
            print(_tr("[OK] モデル: 取得済み", "[OK] Models: downloaded"))
        image = payload["runtime_image"]
        print(_tr("[%s] 実行イメージ: %s", "[%s] Runtime image: %s") % (
            "OK" if image["present"] else "--",
            _tr("ビルド済み", "built") if image["present"]
            else _tr("未ビルド", "not built")))
        disk = payload["disk"]
        if disk["free_gib"] is not None:
            print(_tr("[%s] 空き容量: %.1f GiB",
                      "[%s] Disk: %.1f GiB free") % (
                          "OK" if disk["sufficient"] else "NG", disk["free_gib"]))
        rt = payload["runtime_estimate"]
        if rt.get("known"):
            if _ui_is_japanese():
                print("\n%d秒動画の生成: 推定%d分" % (
                    round(estimate_seconds), rt["minutes"]))
            else:
                print("\n%d-second video generation: estimated %d min" % (
                    round(estimate_seconds), rt["minutes"]))
            observed = rt.get("observation_range") or {}
            if observed.get("count", 0) > 1:
                print(_tr("同一構成の観測範囲: 約%d〜%d分（環境差を含む）",
                          "Observed range for this configuration: about %d-%d min "
                          "(includes environment differences)") % (
                              round(observed["min_minutes"]),
                              round(observed["max_minutes"])))
        if not is_fully_verified(profile):
            print(_tr("\n[--] 検証範囲: %s", "\n[--] Verification scope: %s")
                  % ", ".join(_verification_limits(profile)))
        if payload["blockers"]:
            print(_tr("\n準備できない理由:", "\nCannot prepare:"))
            _print_blockers(payload["blockers"])

    _emit(args, payload, render)
    if args.json or args.check or not interactive:
        return EXIT_OK if ok else EXIT_NO_MATCH
    if not ok or payload["disk"]["sufficient"] is False:
        return EXIT_NO_MATCH
    needs_models = _models_missing(payload["downloads"])
    needs_image = not payload["runtime_image"]["present"]
    if not needs_models and not needs_image:
        print(_tr("\n準備完了", "\nPreparation complete"))
        print(_tr("次の手順: ./bin/narration-video-gen run",
              "Next: ./bin/narration-video-gen run"))
        return EXIT_OK
    if needs_models and needs_image:
        action_prompt = _tr("モデル取得と実行イメージのビルドを開始しますか？",
                            "Download models and build the runtime image?")
    elif needs_models:
        action_prompt = _tr("モデルを取得しますか？", "Download models?")
    else:
        action_prompt = _tr("実行イメージをビルドしますか？", "Build the runtime image?")
    if not _confirm("\n%s" % action_prompt):
        print(_tr("中止しました。", "Cancelled."))
        return EXIT_OK
    try:
        if needs_models:
            download_command = [str(args.root / "scripts" / "download-models.sh"),
                                "--profile", profile["id"]]
            for directory in args.profile_dir:
                download_command.extend(("--profile-dir", str(directory)))
            subprocess.run(download_command, check=True)
        if needs_image:
            build_command = [str(args.root / "scripts" / "build-image.sh")]
            if "musetalk" in (recipe.get("pipeline_stages") or []):
                build_command.append("--with-musetalk")
            subprocess.run(build_command, check=True)
    except subprocess.CalledProcessError as exc:
        print(_tr("準備に失敗しました（終了コード %d）。plan を再実行すると続きから進めます。",
                  "Preparation failed (exit %d). Re-run plan to continue.") % exc.returncode,
              file=sys.stderr)
        return EXIT_ERROR
    refreshed = plan_mod.build_plan(
        args.root, catalog, profile, _env_for(args), args.root / "models", estimate_seconds,
        recipe=recipe)
    if _models_missing(refreshed["downloads"]) or not refreshed["runtime_image"]["present"]:
        print(_tr("準備の最終確認に通りませんでした。plan を再実行して詳細を確認してください。",
                  "Preparation did not pass its final checks. Re-run plan for details."),
              file=sys.stderr)
        return EXIT_ERROR
    print(_tr("\n準備完了", "\nPreparation complete"))
    print(_tr("次の手順: ./bin/narration-video-gen run",
              "Next: ./bin/narration-video-gen run"))
    return EXIT_OK


def cmd_verify(args):
    expected = {}
    if args.expect:
        expected = json.loads(Path(args.expect).read_text(encoding="utf-8"))
    else:
        catalog = Catalog(args.root, profile_dirs=args.profile_dir)
        env = _env_for(args)
        profile = _resolve_profile(catalog, env, args)
        if profile is None:
            return _report_no_profile(catalog, env, args)
        recipe = catalog.recipe_for(profile, _saved_recipe_options(args.root, profile))
        expected = {
            "width": recipe["resolution"][0],
            "height": recipe["resolution"][1],
            "audio_present": True,
        }
        if args.frames:
            expected["frames"] = args.frames
        elif args.audio:
            # Derive the expected frame count from the audio the run was given,
            # the same way the generator does. The number in a profile's
            # evidence belongs to *that* run's narration, not to yours.
            seconds = verify_mod.wav_duration(args.audio)
            if recipe.get("output_fps"):
                expected["frames"] = int(round(seconds * recipe["output_fps"]))
            else:
                expected["frames"] = verify_mod.frames_for_audio(seconds, recipe["fps"])

    try:
        probe_options = {}
        if shutil.which("ffprobe") is None and Path(args.path).is_file():
            probe_options = {
                "command_prefix": ["docker", "exec", args.container, "ffprobe"],
                "probe_path": _stage_input(args.root, args.path),
            }
        result = verify_mod.verify_output(args.path, expected, **probe_options)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    def render(result):
        print("%s" % result["path"])
        for check in result["checks"]:
            print("  [%s] %-24s %s"
                  % ("PASS" if check["ok"] else "FAIL", check["name"], check["detail"]))
        if result.get("sha256"):
            print("  sha256: %s" % result["sha256"])
        print("\n%s" % ("PASSED" if result["passed"] else "FAILED"))
        print(_tr("公開前に映像と音声を確認してください。",
                  "Review the video and audio before publishing."))

    _emit(args, result, render)
    return EXIT_OK if result["passed"] else EXIT_ERROR


def _release_comfy_between_stages(client, stage_index, is_comfy_stage):
    """Drop models and cached tensors before the next in-process GPU stage."""
    if stage_index <= 1 or not is_comfy_stage:
        return False
    client.call("/free", {"unload_models": True, "free_memory": True})
    return True


def _release_tts_backend_for_video(root):
    """Stop the managed TTS engine before a video run claims the GPU."""
    container = "narration-video-gen-tts"
    if not _container_running(container):
        return True
    try:
        result = subprocess.run(
            [str(root / "scripts" / "tts-backend.sh"), "stop"],
            cwd=str(root), capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(_tr("TTSエンジンを停止できませんでした: %s",
                  "Could not stop the TTS engine: %s") % exc, file=sys.stderr)
        return False
    if result.returncode != 0 or _container_running(container):
        detail = (result.stderr or result.stdout).strip()
        message = _tr("TTSエンジンを停止できませんでした。",
                      "Could not stop the TTS engine.")
        if detail:
            message += " " + detail
        print(message, file=sys.stderr)
        return False
    print(_tr(
        "TTSエンジンを停止してGPUメモリを解放しました。TTS Web UIは起動したままです。",
        "Stopped the TTS engine to release GPU memory. The TTS Web UI remains available."))
    return True


def cmd_run(args):
    result = {}
    if not args.json:
        return _run_pipeline(args, result)
    with contextlib.redirect_stdout(sys.stderr):
        code = _run_pipeline(args, result)
    result.setdefault("status", "planned" if args.dry_run and code == EXIT_OK
                      else "completed" if code == EXIT_OK else "failed")
    result["exit_code"] = code
    _emit(args, result, lambda _data: None)
    return code


def _run_pipeline(args, result):
    """Run a pipeline. Refuses to start until the run has been acknowledged."""
    from . import runner as runner_mod
    from . import stages as stages_mod

    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    env = _env_for(args)
    guided = (not args.json and sys.stdin.isatty() and sys.stdout.isatty()
              and not any((args.image, args.audio, args.source_video, args.stages)))
    explicit_selection = any((args.profile, args.recipe, args.model, args.resolution))
    if guided and not explicit_selection and selection_state.load(args.root) is None:
        print(_tr("まだ構成を選んでいません。先に `narration-video-gen plan` を実行してください。",
                  "No configuration chosen yet; run `narration-video-gen plan` first."),
              file=sys.stderr)
        return EXIT_NO_MATCH
    profile = _resolve_profile(catalog, env, args)
    if profile is None:
        return _report_no_profile(catalog, env, args)
    recipe_options = _saved_recipe_options(args.root, profile)
    profile, recipe, local_calibration = _local_profile(
        profile, catalog, env, recipe_options, enabled=not bool(args.profile))
    _attach_local_profile_dir(args, profile)

    if guided:
        preparation = plan_mod.build_plan(
            args.root, catalog, profile, env, args.root / "models", 30,
            recipe=recipe)
        if not args.dry_run and (_models_missing(preparation["downloads"])
                or not preparation["runtime_image"]["present"]):
            print(_tr("生成の準備ができていません。先に `narration-video-gen plan` を実行してください。",
                      "Generation is not prepared. Run `narration-video-gen plan` first."),
                  file=sys.stderr)
            return EXIT_NO_MATCH

    try:
        pipeline = runner_mod.pipeline_for(
            recipe, [s.strip() for s in args.stages.split(",")] if args.stages else None)
        if args.mask_only:
            if "face-detailer" not in pipeline:
                raise stages_mod.StageError("--mask-only requires the face-detailer stage")
            pipeline = pipeline[:pipeline.index("face-detailer") + 1]
    except stages_mod.StageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    ok, blockers, _notes = evaluate(profile, env)
    blockers = _without_stale_download_budget(
        blockers, args.root, catalog, profile, env, recipe)
    if blockers and not args.force:
        print(_tr("このマシンは %s の要件を満たしていません:",
                  "This machine does not satisfy %s:") % profile["id"], file=sys.stderr)
        _print_blockers(blockers, output=sys.stderr)
        return EXIT_NO_MATCH

    # Resolve inputs. A pipeline of post-stages only starts from a video.
    image_name = None
    audio_name = None
    generation_audio = None
    canonical_generation_audio = None
    initial_audio_source = None
    length_plan = None
    full_seconds = None
    from_video = stages_mod.needs_source_video(pipeline)
    if not guided and not from_video and args.length is None:
        print("--length short or --length full is required for a non-interactive run",
              file=sys.stderr)
        return EXIT_ERROR
    if from_video:
        if not args.source_video:
            print("stages %s start from an existing video; pass --source-video"
                  % ", ".join(pipeline), file=sys.stderr)
            return EXIT_ERROR
        try:
            source = _stage_input(args.root, args.source_video)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        source_path = Path(args.source_video).expanduser().resolve()
        companion = source_path.with_name(
            source_path.stem + "-audio" + source_path.suffix)
        canonical_audio = (Path(args.audio).expanduser()
                           if args.audio else
                           companion if companion.is_file() else source_path)
        try:
            initial_audio_source = _stage_input(args.root, canonical_audio)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        seconds = None
        try:
            result = verify_mod.verify_output(args.source_video)
            source_frames = result.get("frames")
        except (RuntimeError, OSError):
            source_frames = None
        if "face-detailer" in pipeline:
            try:
                frames = runner_mod.face_detail_frame_count(
                    source_frames, args.frames)
            except runner_mod.RunnerError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_ERROR
            if args.frames is None and frames != source_frames:
                print("face detail frame count: %d source frames -> %d (4n+1)"
                      % (source_frames, frames))
        else:
            frames = args.frames if args.frames is not None else source_frames
        if frames is None:
            print("could not determine the frame count of %s; pass --frames"
                  % args.source_video, file=sys.stderr)
            return EXIT_ERROR
        if "musetalk" in pipeline:
            if not args.audio:
                print("--audio is required when starting from the MuseTalk stage",
                      file=sys.stderr)
                return EXIT_ERROR
            generation_audio = Path(args.audio)
            if not generation_audio.is_file():
                print("input not found: %s" % generation_audio, file=sys.stderr)
                return EXIT_ERROR
    else:
        if guided:
            selected_inputs = _choose_input_set(args.root)
            if selected_inputs is None:
                print(_tr("中止しました。", "Cancelled."))
                return EXIT_CANCELLED
            args.image, args.audio = selected_inputs
        for label, value in (("--image", args.image), ("--audio", args.audio)):
            if not value:
                print("%s is required for stage %s" % (label, pipeline[0]),
                      file=sys.stderr)
                return EXIT_ERROR
        image, audio = Path(args.image).expanduser(), Path(args.audio).expanduser()
        try:
            if not image.is_file():
                raise ValueError("input not found: %s" % image)
            full_seconds = verify_mod.wav_duration(audio)
        except (ValueError, OSError, EOFError, wave.Error) as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        length_plan = _choose_run_length(
            recipe, full_seconds, requested=args.length)
        if length_plan is None:
            print(_tr("中止しました。", "Cancelled."))
            return EXIT_CANCELLED
        seconds, frames = length_plan["seconds"], length_plan["frames"]
        source = None

    try:
        models = runner_mod.resolve_models(args.root, recipe, pipeline)
    except runner_mod.RunnerError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    run_id = args.run_id or _new_run_id(args.root)
    result.update({"run_id": run_id, "profile": profile["id"],
                   "stages": pipeline, "frames": frames, "seconds": seconds})
    if args.dry_run:
        result["workflows"] = []
    estimate = plan_mod.estimate_runtime(profile, recipe, seconds, catalog=catalog)
    if guided:
        _render_generation_summary(
            args.root, recipe, args.image, args.audio, seconds, estimate,
            length_label=length_plan["label"])
        if not args.dry_run and not _confirm(_tr("\n生成を開始しますか？", "\nStart generation?")):
            print(_tr("中止しました。", "Cancelled."))
            return EXIT_OK
        args.length = _length_cli_value(length_plan)
        if not args.dry_run:
            return _launch_background(args, profile, env, run_id, estimate)
    else:
        print("profile  : %s" % profile["id"])
        print("recipe   : %s (%dx%d, %s fps)"
              % (recipe["id"], recipe["resolution"][0], recipe["resolution"][1],
                 recipe["fps"]))
        print("stages   : %s" % " -> ".join(pipeline))
        if seconds is not None:
            print("length   : %s" % length_plan["mode"])
            print("audio    : %s (%.3f s -> %d frames)" % (args.audio, seconds, frames))
        else:
            print("source   : %s (%d frames)" % (args.source_video, frames))
        if estimate.get("known"):
            print("estimate : ~%d min%s for the full pipeline%s"
                  % (estimate["minutes"],
                     " (extrapolated)" if estimate.get("extrapolated") else "",
                     "" if len(pipeline) == len(recipe.get("pipeline_stages") or pipeline)
                     else "; you are running a subset, so less"))

    if not from_video:
        generation_audio = audio
        if length_plan["mode"] == "test":
            generation_audio = args.root / "outputs" / run_id / "input-short-test.wav"
            try:
                actual_seconds = runner_mod.trim_wav(audio, generation_audio, seconds)
            except (OSError, EOFError, wave.Error) as exc:
                print(_tr("短尺テスト用の音声を作成できませんでした: %s",
                          "Could not create the short test audio: %s") % exc, file=sys.stderr)
                return EXIT_ERROR
            seconds = actual_seconds
            frames = verify_mod.frames_for_audio(seconds, recipe["fps"])
        try:
            image_name = _stage_input(args.root, image, loader=True)
            audio_name = _stage_input(args.root, generation_audio, loader=True)
            canonical_generation_audio = _stage_input(
                args.root, generation_audio, loader=False)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR

    if not args.dry_run and not _release_tts_backend_for_video(args.root):
        _worker_error(args, _tr("TTSエンジンを停止できませんでした。",
                                "Could not stop the TTS engine."),
                      code="tts-backend-stop-failed")
        return EXIT_ERROR

    client = runner_mod.ComfyClient(args.server)
    if (guided or args.background_worker) and not args.dry_run:
        try:
            subprocess.run([str(args.root / "scripts" / "up.sh"), "up", "-d"], check=True)
            client.wait_ready()
        except (subprocess.CalledProcessError, runner_mod.RunnerError) as exc:
            _worker_error(args, _tr("生成サービスを起動できませんでした。",
                                    "Could not start the generation service."),
                          code="service-start-failed", detail=str(exc))
            print(_tr("生成サービスを起動できませんでした: %s",
                      "Could not start the generation service: %s") % exc, file=sys.stderr)
            return EXIT_ERROR
        if guided:
            print(_tr("\n生成を開始します。", "\nStarting generation."))
    if _tracks_run(args):
        state_values = {
            "status": "running",
            "pid": os.getpid(),
            "profile": profile["id"],
            "profile_settings": dict(profile.get("settings") or {}),
            "profile_requires": dict(profile.get("requires") or {}),
            "recipe": recipe["id"],
            "recipe_resolution": list(recipe.get("resolution") or []),
            "stages": pipeline,
            "recipe_options": recipe_options,
            "local_calibration": local_calibration,
        }
        state_values.update(_reference_eta_state(profile, env, estimate))
        state_values.update(
            _local_eta_state(args, profile, recipe, env, catalog, seconds,
                             recipe_options=recipe_options, pipeline=pipeline,
                             runtime=_runtime_revision(args.root)))
        state_values["basis_seconds"] = seconds
        run_state.write(args.root, run_id, **state_values)
    produced = []
    pipeline_started = time.time()
    current_fps = float(recipe["fps"])
    # Visual post-stages re-encode AAC. Keep the earliest narration track so
    # encoder padding does not accumulate before the final retime.
    audio_source = (initial_audio_source if from_video
                    else canonical_generation_audio)

    for index, stage_id in enumerate(pipeline, start=1):
        out_prefix = "%s/%02d-%s" % (run_id, index, stage_id)
        rife_chunk_frames = (
            (profile.get("settings") or {}).get("rife_chunk_frames")
            if stage_id == "rife" else None)
        try:
            workflow, output_node = runner_mod.build_stage(
                stage_id, recipe=recipe, profile=profile, models=models,
                image_name=image_name,
                audio_name=audio_name,
                frames=frames, out_prefix=out_prefix, source_video=source,
                audio_source=audio_source,
                mask_only=args.mask_only and stage_id == "face-detailer",
                source_fps=current_fps,
            )
        except (runner_mod.RunnerError, stages_mod.StageError) as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR

        stage_label = (_tr(*STAGE_LABELS.get(stage_id, (stage_id, stage_id)))
                       if guided else stage_id)
        if _tracks_run(args):
            run_state.write(args.root, run_id, status="running", stage=stage_id,
                            stage_index=index, stage_total=len(pipeline),
                            live_progress=None)
        print("\n[%d/%d] %s" % (index, len(pipeline), stage_label))
        if args.dry_run:
            preview = workflow
            if rife_chunk_frames:
                multiplier = int(
                    ((recipe.get("postprocess") or {}).get("rife") or {})
                    .get("multiplier", 4))
                preview = {
                    "mode": "chunked",
                    "chunk_frames": rife_chunk_frames,
                    "multiplier": multiplier,
                    "chunks": [],
                    "join": "ffmpeg concat stream copy, then one canonical audio mux",
                }
                for chunk_number, chunk in enumerate(
                        runner_mod.rife_chunk_ranges(
                            frames, rife_chunk_frames, multiplier), start=1):
                    chunk_workflow, _ = runner_mod.build_rife_chunk_workflow(
                        ((recipe.get("postprocess") or {}).get("rife") or {}),
                        source, current_fps, out_prefix, chunk, chunk_number)
                    preview["chunks"].append({
                        **chunk, "workflow": chunk_workflow})
            if stage_id in runner_mod.COMMAND_STAGES:
                target = runner_mod.container_path("%s.mp4" % out_prefix)
                preview = [part.replace("{output}", target) for part in workflow]
            result["workflows"].append({"stage": stage_id, "workflow": preview})
            if stage_id in runner_mod.EXTERNAL_STAGES:
                if not args.json:
                    print(json.dumps(workflow, indent=2, ensure_ascii=False))
            elif stage_id in runner_mod.COMMAND_STAGES:
                if not args.json:
                    print(" ".join(preview))
            elif not args.json:
                json.dump(workflow, sys.stdout, indent=2, ensure_ascii=False)
                sys.stdout.write("\n")
            # Later stages consume the previous stage's file, which does not
            # exist during a dry run; keep the placeholder visible.
            audio_source = _canonical_audio_source(
                stage_id, audio_source,
                runner_mod.container_path("%s.mp4" % out_prefix))
            source = runner_mod.container_path("%s.mp4" % out_prefix)
            if stage_id == "musetalk":
                current_fps = float(workflow.get("output_fps", 25))
                if seconds is not None:
                    frames = int(round(seconds * current_fps))
            continue

        try:
            _release_comfy_between_stages(
                client, index,
                stage_id not in runner_mod.EXTERNAL_STAGES
                and stage_id not in runner_mod.COMMAND_STAGES)
        except (OSError, ValueError) as exc:
            message = _tr(
                "前のステージのGPUメモリを解放できませんでした。",
                "Could not release GPU memory from the previous stage.")
            _worker_error(args, message, code="service-free-failed",
                          detail=str(exc))
            print("%s: %s" % (message, exc), file=sys.stderr)
            return EXIT_ERROR

        if stage_id in runner_mod.EXTERNAL_STAGES:
            if generation_audio is None:
                print(_tr("MuseTalkにナレーション音声が渡されていません",
                          "MuseTalk has no narration audio input"), file=sys.stderr)
                return EXIT_ERROR
            try:
                client.call("/free", {"unload_models": True, "free_memory": True})
            except (OSError, ValueError):
                pass
            started = time.time()
            external_container = runner_mod.musetalk_container_name(
                "%s.mp4" % out_prefix)
            if _tracks_run(args):
                run_state.write(args.root, run_id,
                                external_container=external_container)
            try:
                final = runner_mod.run_musetalk_stage(
                    args.root, workflow, source, generation_audio,
                    "%s.mp4" % out_prefix)
            except runner_mod.RunnerError as exc:
                print("stage %s failed after %.1f min:\n%s"
                      % (stage_id, (time.time() - started) / 60, exc), file=sys.stderr)
                return EXIT_ERROR
            finally:
                if _tracks_run(args):
                    run_state.write(args.root, run_id, external_container=None)
            print(_tr("  完了: %.1f分", "  Done: %.1f min")
                  % ((time.time() - started) / 60))
            produced.append(final)
            source = final
            audio_source = _canonical_audio_source(stage_id, audio_source, final)
            try:
                frames, current_fps = runner_mod.probe_container_video(
                    args.container, final)
            except runner_mod.RunnerError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_ERROR
            continue

        if stage_id in runner_mod.COMMAND_STAGES:
            started = time.time()
            try:
                final = runner_mod.run_command_stage(
                    workflow, container=args.container,
                    audio_source=audio_source,
                    output_path=runner_mod.container_path("%s.mp4" % out_prefix))
            except runner_mod.RunnerError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_ERROR
            print(_tr("  完了: %.1f分", "  Done: %.1f min")
                  % ((time.time() - started) / 60))
            if not guided:
                print("  output: %s" % final)
            produced.append(final)
            source = final
            continue

        started = time.time()
        chunked_rife = None
        try:
            if rife_chunk_frames:
                rife_config = dict(
                    ((recipe.get("postprocess") or {}).get("rife") or {}))
                rife_config["chunk_frames"] = rife_chunk_frames

                def chunk_progress(number, total, chunk, _probe):
                    print("  chunk %d/%d: source frames %d-%d -> %d frames"
                          % (number, total, chunk["start_frame"],
                             chunk["start_frame"] + chunk["input_frames"] - 1,
                             chunk["kept_frames"]))

                chunked_rife = runner_mod.run_chunked_rife_stage(
                    client, config=rife_config, source_video=source,
                    source_fps=current_fps, source_frames=frames,
                    out_prefix=out_prefix, audio_source=audio_source,
                    container=args.container, on_chunk=chunk_progress)
                outputs = [
                    path.removeprefix("/opt/ComfyUI/output/")
                    for path in (chunked_rife["video"], chunked_rife["audio"])
                ]
            else:
                sampler_maxima = _sampler_progress_maxima(workflow)
                prompt_id = client.submit(workflow)
                if not guided:
                    print("  prompt_id: %s" % prompt_id)
                history = client.wait(
                    prompt_id,
                    on_progress=lambda data: _record_live_progress(
                        args, stage_id, started, sampler_maxima, data),
                )
                outputs = runner_mod.outputs_of(history, output_node)
        except runner_mod.RunnerError as exc:
            if exc.code == "gpu-out-of-memory":
                message = _tr("GPUメモリ不足で生成に失敗しました。",
                              "Generation failed because GPU memory ran out.")
            elif exc.code == "host-out-of-memory":
                message = _tr("ホストのメモリ不足で生成に失敗しました。",
                              "Generation failed because host memory ran out.")
            else:
                message = str(exc)
            _worker_error(args, message, code=exc.code, detail=exc.detail,
                          context=exc.context)
            print("stage %s failed after %.1f min:\n%s"
                  % (stage_id, (time.time() - started) / 60, exc), file=sys.stderr)
            # The reader is here now; make the next step visible without
            # requiring a second command.
            failed_state = run_state.load(args.root, args.run_id) if _tracks_run(args) else None
            _print_remedies(args, failed_state or {
                "error_code": exc.code, "error_context": exc.context,
                "profile": profile["id"] if profile else None,
                "stage": stage_id})
            if produced:
                print("\nCompleted stages produced:", file=sys.stderr)
                for item in produced:
                    print("  %s" % item, file=sys.stderr)
                print(_tr("再開するには --stages <残りのステージ> --source-video <最後の出力> を指定します。",
                          "Resume with --stages <remaining> --source-video <last output>."),
                      file=sys.stderr)
            return EXIT_ERROR

        print(_tr("  完了: %.1f分", "  Done: %.1f min")
              % ((time.time() - started) / 60))
        if not guided:
            for item in outputs:
                print("  output: %s" % item)
        if not outputs:
            print("stage %s reported no output file" % stage_id, file=sys.stderr)
            return EXIT_ERROR
        produced.extend(outputs)
        try:
            video_output, audio_output = runner_mod.video_and_audio_outputs(outputs)
        except runner_mod.RunnerError as exc:
            print("stage %s returned invalid outputs: %s" % (stage_id, exc),
                  file=sys.stderr)
            return EXIT_ERROR
        video_path = runner_mod.container_path(video_output)
        audio_path = runner_mod.container_path(audio_output)
        try:
            expected_media_frames = _expected_media_frames(stage_id, frames)
            if chunked_rife:
                expected_media_frames = chunked_rife["expected_frames"]
            contract = runner_mod.ensure_audio_companion(
                args.container, video_path, audio_path, audio_source,
                expected_frames=expected_media_frames)
            _record_media_contract(
                args.root, run_id, stage_id, index, contract)
        except runner_mod.RunnerError as exc:
            message = _tr(
                "映像と音声の保存結果が一致せず、自動修復にも失敗しました。",
                "The saved video/audio media contract did not match and repair failed.")
            failure_contract = exc.context.get("media_contract")
            if failure_contract:
                try:
                    _record_media_contract(
                        args.root, run_id, stage_id, index, failure_contract)
                except runner_mod.RunnerError:
                    pass
            _worker_error(args, message, code="media-contract-failed",
                          detail=str(exc), context={
                              "stage": stage_id,
                              "evidence": "outputs/%s/media-contract.json" % run_id,
                          })
            print("stage %s media validation failed: %s" % (stage_id, exc),
                  file=sys.stderr)
            return EXIT_ERROR
        audio_source = _canonical_audio_source(
            stage_id, audio_source, audio_path)
        source = video_path
        try:
            source_frames, _ = runner_mod.probe_container_video(
                args.container, source)
        except runner_mod.RunnerError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        if stage_id in EXACT_FRAME_STAGES \
                and source_frames != frames:
            print(
                "stage %s produced %d video frames; expected exactly %d"
                % (stage_id, source_frames, frames), file=sys.stderr)
            return EXIT_ERROR
        if index < len(pipeline) and pipeline[index] == "face-detailer":
            if source_frames < frames:
                print(
                    "stage %s produced only %d video frames; face detail requires %d"
                    % (stage_id, source_frames, frames), file=sys.stderr)
                return EXIT_ERROR
        if args.mask_only and stage_id == "face-detailer":
            result["output"] = _repository_output_path(produced[-1])
            if _tracks_run(args):
                run_state.write(args.root, run_id, status="completed",
                                output=_repository_output_path(produced[-1]),
                                elapsed_seconds=round(time.time() - pipeline_started, 1))
            print(_tr("\nマスクを出力しました。本処理の前に追跡結果を確認してください。",
                      "\nMask rendered. Check the tracking before running the full pass."))
            return EXIT_OK

    if args.dry_run:
        return EXIT_OK

    print(_tr("\n生成完了: %.1f分", "\nGeneration complete: %.1f min")
          % ((time.time() - pipeline_started) / 60))
    output_path = _repository_output_path(produced[-1])
    result["output"] = output_path
    print(_tr("出力: %s", "Output: %s") % output_path)
    windows_export = None
    windows_export_error = None
    try:
        windows_export = _export_windows_video(
            args.root, run_id, output_path, env.get("platform"))
    except (OSError, subprocess.SubprocessError) as exc:
        windows_export_error = str(exc)
        result["windows_export_error"] = windows_export_error
        print(_tr(
            "Windows側へ動画をコピーできませんでした。WSL側の原本は保持されています: %s",
            "Could not copy the video to Windows. The WSL original is intact: %s")
            % exc, file=sys.stderr)
    if windows_export:
        result["windows_output"] = windows_export["windows_path"]
        print(_tr("Windows保存先: %s", "Windows output: %s")
              % windows_export["windows_path"])
    print(_tr("動画を開いて仕上がりを確認してください。",
              "Open the video to check the result."))
    elapsed_seconds = round(time.time() - pipeline_started, 1)
    if _tracks_run(args):
        state_values = {
            "status": "completed",
            "output": output_path,
            "elapsed_seconds": elapsed_seconds,
        }
        if windows_export:
            state_values["windows_output"] = windows_export["windows_path"]
        if windows_export_error:
            state_values["windows_export_error"] = windows_export_error
        run_state.write(args.root, run_id, **state_values)
    # A subset run measures a different amount of work, so it is not a sample of
    # what this recipe costs end to end.
    if pipeline == runner_mod.pipeline_for(recipe):
        runtime = _runtime_revision(args.root)
        timings.record(env, profile, recipe, seconds, elapsed_seconds,
                       recipe_options=recipe_options, pipeline=pipeline, runtime=runtime)
        _print_full_length_estimate(profile, recipe, env, catalog, seconds, full_seconds,
                                    recipe_options=recipe_options, pipeline=pipeline,
                                    runtime=runtime)
    return EXIT_OK


def _calibration_target(args, catalog, env):
    profile = _resolve_profile(catalog, env, args)
    if profile is None:
        return None, None, None
    interactive = not args.json and sys.stdin.isatty() and sys.stdout.isatty()
    recipe_options = _choose_plan_recipe_options(
        catalog, profile, args, interactive)
    if recipe_options is None:
        return profile, None, None
    recipe = catalog.recipe_for(profile, recipe_options)
    return profile, recipe, recipe_options


def _calibration_base_snapshot(profile):
    """Return the profile passed to the calibration harness."""
    snapshot = {key: value for key, value in profile.items()
                if not key.startswith("_")}
    snapshot["settings"] = {
        key: value for key, value in (profile.get("settings") or {}).items()
        if key not in calibration_state.BALLAST_SETTINGS
    }
    return snapshot


def _container_running(name):
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _calibration_container_environment(env, image_plan):
    values = [
        "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True",
        "PYTHONUNBUFFERED=1",
        "NVG_RUNTIME_BUILD_SHA=%s" % image_plan["build_sha256"],
        "NVG_IMAGE_REFERENCE=local:%s" % image_plan["build_sha256"],
    ]
    workaround = os.environ.get("NVG_WSL2_ALLOCATOR_WORKAROUND", "")
    if workaround not in ("", "0", "1"):
        raise ValueError("NVG_WSL2_ALLOCATOR_WORKAROUND must be 0 or 1")
    if (env.get("platform") == "windows-wsl2"
            and workaround != "0"):
        # Local calibration bypasses Compose. Match scripts/up.sh when a user
        # uses the default WSL2 compatibility mode or explicitly opts into it.
        values.append("LD_PRELOAD=/opt/vmm-rdma-interpose.so")
    return [item for value in values for item in ("-e", value)]


def cmd_calibrate(args):
    """Search block swap for the selected plan and persist the exact result."""
    search_mode = getattr(args, "search_mode", "boundary")
    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    env = _env_for(args)
    profile, recipe, recipe_options = _calibration_target(args, catalog, env)
    if profile is None:
        return _report_no_profile(catalog, env, args)
    if recipe is None:
        return EXIT_CANCELLED
    scenario = calibration_state.scenario_for(recipe)
    if scenario is None:
        print(_tr("この構成はblock swap検索に対応していません。",
                  "This plan does not support block-swap calibration."), file=sys.stderr)
        return EXIT_ERROR

    if args.import_profile_set:
        try:
            path, record = calibration_state.import_profile_set(
                args.import_profile_set, profile, recipe, recipe_options, env)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(_tr("キャリブレーション結果を登録できません: %s",
                      "Could not register calibration result: %s") % exc, file=sys.stderr)
            return EXIT_ERROR
        payload = {
            "status": "imported", "path": str(path), "scenario": scenario,
            "profile": record["profile"]["id"],
            "blocks_to_swap": record["profile"]["settings"]["blocks_to_swap"],
        }
        _emit(args, payload, lambda value: print(
            _tr("登録完了: %s\nblocks_to_swap: %s\n次回の同一planから自動適用します。",
                "Registered: %s\nblocks_to_swap: %s\nIt will apply to the next matching plan.")
            % (value["path"], value["blocks_to_swap"])))
        return EXIT_OK

    active = run_state.latest(args.root, running_only=True)
    if active and _pid_is_run(active):
        print(_tr("動画生成が実行中です。完了またはcancel後に検索してください。",
                  "Generation is running. Calibrate after it completes or is cancelled."),
              file=sys.stderr)
        return EXIT_ERROR
    image_plan = plan_mod.runtime_image_plan(args.root, recipe=recipe)
    if not image_plan["present"]:
        print(_tr("キャリブレーション対応イメージがありません。先にplanで準備してください。",
                  "The calibration-capable image is missing. Prepare it with plan first."),
              file=sys.stderr)
        return EXIT_NO_MATCH
    if not (env.get("gpus") and len(env["gpus"]) == 1):
        print(_tr("検索にはNVIDIA GPUが1台だけ見える環境が必要です。",
                  "Calibration requires exactly one visible NVIDIA GPU."), file=sys.stderr)
        return EXIT_NO_MATCH

    run_id = args.run_id or ("local-%s-calibration-%s" % (
        scenario, time.strftime("%Y%m%dT%H%M%S")))
    workspace = args.root / "results" / "local-calibration-workspace"
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    (args.root / "cache").mkdir(parents=True, exist_ok=True)
    image = image_plan["image"]
    models_dir = (args.root / "models").resolve()
    assets_dir = (args.root / "assets").resolve()
    command = [
        "docker", "run", "--rm", "--name", "narration-video-gen-calibration",
        "--gpus", "all", "--ipc", "host", "--shm-size", "8g",
    ]
    command += _calibration_container_environment(env, image_plan)
    command += [
        "-v", "%s:/workspace" % workspace.resolve(),
        # The calibration harness verifies weights under /workspace/models,
        # while ComfyUI discovers them under /opt/ComfyUI/models, so mount
        # both views explicitly.
        "-v", "%s:/workspace/models:ro" % models_dir,
        "-v", "%s:/opt/ComfyUI/models:ro" % models_dir,
        # The interpolation node uses its own checkpoint directory instead of
        # ComfyUI's model registry. Expose the pinned copy there so calibration
        # never falls back to an unverified network download.
        "-v", "%s:/opt/ComfyUI/custom_nodes/ComfyUI-Frame-Interpolation/ckpts/rife:ro"
        % (models_dir / "rife"),
        "-v", "%s:/opt/ComfyUI/input:ro" % assets_dir,
        "-v", "%s:/cache" % (args.root / "cache").resolve(),
        image, "python3", "/opt/nvg/calibration/calibrate.py",
        "calibrate", "--scenario", scenario, "--run-id", run_id,
        "--search-mode", search_mode,
        "--allow-unknown-image",
    ]
    # Search the selected plan itself (recipe and memory settings), not the
    # scenario's default profile; otherwise the result would belong to a
    # different recipe and could not be imported. Measure the real GPU: a
    # capacity-simulation ballast would shrink it.
    command += ["--base-profile-json",
                json.dumps(_calibration_base_snapshot(profile), ensure_ascii=True)]
    payload = {
        "status": "planned", "scenario": scenario, "base_profile": profile["id"],
        "recipe_options": recipe_options, "workspace": str(workspace),
        "run_id": run_id, "image": image, "search_mode": search_mode,
    }
    if args.dry_run:
        payload["command"] = command
        _emit(args, payload, lambda value: print(
            _tr("検索予定: %s\n基準プロファイル: %s\n結果保存先: %s",
                "Calibration planned: %s\nBase profile: %s\nResults: %s")
            % (scenario, profile["id"], workspace)))
        return EXIT_OK
    if not args.yes:
        if args.json:
            return _cli_error(args, "--yes is required to start calibration; use --dry-run to preview")
        if not sys.stdin.isatty() or not _confirm(_tr(
                "%s の短尺生成を複数回実行してblock swapを検索します。開始しますか？" % scenario,
                "Run several short generations to calibrate %s. Start?" % scenario)):
            print(_tr("中止しました。", "Cancelled."))
            return EXIT_OK

    was_running = _container_running("narration-video-gen-comfy")
    completed = None
    try:
        if was_running:
            subprocess.run(["docker", "stop", "narration-video-gen-comfy"],
                           check=True, stdout=subprocess.DEVNULL)
        completed = subprocess.run(
            command, text=True, capture_output=args.json, check=False)
    finally:
        # The calibration controller must write /opt/ComfyUI, so it runs as
        # root. Return only its bind-mounted evidence to the invoking user.
        subprocess.run([
            "docker", "run", "--rm", "-v", "%s:/workspace" % workspace.resolve(),
            image, "chown", "-R", "%d:%d" % (os.getuid(), os.getgid()),
            "/workspace/results",
        ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if was_running:
            subprocess.run([str(args.root / "scripts" / "up.sh"), "up", "-d"],
                           check=False, stdout=subprocess.DEVNULL)
    if completed is None or completed.returncode:
        if args.json and completed:
            payload.update({"status": "failed", "returncode": completed.returncode,
                            "stdout": completed.stdout, "stderr": completed.stderr})
            _emit(args, payload, lambda _value: None)
        else:
            print(_tr("block swap検索に失敗しました。結果: %s",
                      "Block-swap calibration failed. Results: %s") % workspace,
                  file=sys.stderr)
        return EXIT_ERROR

    profile_set = workspace / "results" / run_id / "profile-set.json"
    try:
        path, record = calibration_state.import_profile_set(
            profile_set, profile, recipe, recipe_options, env)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(_tr("検索は完了しましたが結果を登録できません: %s",
                  "Calibration completed but registration failed: %s") % exc,
              file=sys.stderr)
        return EXIT_ERROR
    payload.update({
        "status": "completed", "path": str(path),
        "profile": record["profile"]["id"],
        "blocks_to_swap": record["profile"]["settings"]["blocks_to_swap"],
        "profile_set": str(profile_set),
    })
    _emit(args, payload, lambda value: print(
        _tr("\n検索完了: blocks_to_swap=%s\n保存先: %s\n"
            "次回の同一planからこの値を自動適用します。",
            "\nCalibration complete: blocks_to_swap=%s\nSaved: %s\n"
            "The next matching plan will apply it automatically.")
        % (value["blocks_to_swap"], value["path"])))
    return EXIT_OK


def cmd_calibrations(args):
    env = _env_for(args)
    found = calibration_state.records(env=None if args.all_machines else env)
    if args.calibration_command in ("enable", "disable"):
        try:
            calibration_state.set_enabled(
                args.path, args.calibration_command == "enable")
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_ERROR
        found = calibration_state.records(env=None if args.all_machines else env)
    payload = [{
        "path": item["path"], "enabled": item.get("enabled", True),
        "base_profile": (item.get("plan") or {}).get("base_profile"),
        "recipe_options": (item.get("plan") or {}).get("recipe_options"),
        "gpu": (item.get("machine") or {}).get("gpu_name"),
        "blocks_to_swap": ((item.get("profile") or {}).get("settings") or {}).get(
            "blocks_to_swap"),
        "saved_at": item.get("saved_at"),
    } for item in found]

    def render(rows):
        if not rows:
            print(_tr("保存済みキャリブレーションはありません。",
                      "No saved calibrations."))
            return
        for row in rows:
            print("[%s] %s / blocks_to_swap=%s / %s" % (
                "ON" if row["enabled"] else "OFF", row["base_profile"],
                row["blocks_to_swap"], row["gpu"]))
            print("  %s" % row["path"])

    _emit(args, payload, render)
    return EXIT_OK


def _repository_output_path(path):
    """Return a stage output as a path the user can open, not a container one.

    Only container paths and repository-relative ones are rewritten. Any other
    absolute path is already openable and is left alone.
    """
    container_output_root = "/opt/ComfyUI/output/"
    if path.startswith(container_output_root):
        return "outputs/" + path[len(container_output_root):]
    windows = PureWindowsPath(path)
    if PurePosixPath(path).is_absolute() or windows.is_absolute() or windows.drive:
        return path
    if not path.startswith("outputs/"):
        return "outputs/" + path
    return path


def _windows_videos_paths():
    """Return the user's Videos directory as WSL and Windows paths.

    The Windows known-folder lookup respects a relocated Videos directory;
    ``wslpath`` then gives us a path that can be written without moving the
    generation workspace onto the slower Windows-mounted filesystem.
    """
    command = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "$path=[Environment]::GetFolderPath([Environment+SpecialFolder]::MyVideos); "
        "if (-not $path) {$path=Join-Path $env:USERPROFILE 'Videos'}; "
        "[Console]::Write($path)"
    )
    windows = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=15, check=False)
    windows_root = windows.stdout.strip()
    if windows.returncode != 0 or not windows_root:
        detail = windows.stderr.strip() or "Windows Videos folder was not returned"
        raise OSError(detail)
    converted = subprocess.run(
        ["wslpath", "-u", windows_root], capture_output=True, text=True,
        timeout=15, check=False)
    wsl_root = converted.stdout.strip()
    if converted.returncode != 0 or not wsl_root:
        detail = converted.stderr.strip() or "could not translate the Windows Videos path"
        raise OSError(detail)
    return Path(wsl_root), PureWindowsPath(windows_root)


def _export_windows_video(root, run_id, output_path, platform):
    """Copy one completed video to the Windows Videos known folder atomically."""
    if platform != "windows-wsl2":
        return None
    run_state.validate_run_id(run_id)
    source = Path(output_path).expanduser()
    if not source.is_absolute():
        source = Path(root) / source
    if not source.is_file():
        raise OSError("completed video not found: %s" % source)

    wsl_videos, windows_videos = _windows_videos_paths()
    destination_dir = wsl_videos / WINDOWS_EXPORT_DIRECTORY / run_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or ".mp4"
    destination = destination_dir / ("video" + suffix)
    temporary = destination.with_name(".%s.%d.partial" % (destination.name, os.getpid()))
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    windows_destination = (windows_videos / WINDOWS_EXPORT_DIRECTORY /
                           run_id / destination.name)
    return {
        "path": str(destination),
        "windows_path": str(windows_destination),
        "bytes": destination.stat().st_size,
    }


def _pid_is_run(state):
    try:
        pid = int(state.get("pid"))
        command = [part for part in Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
                   if part]
    except (TypeError, ValueError, OSError):
        return False
    run_id = str(state.get("run_id", "")).encode("utf-8")
    try:
        run_id_index = command.index(b"--run-id")
    except ValueError:
        return False
    return (bool(run_id)
            and any(b"narration-video-gen" in part for part in command)
            and b"run" in command
            and run_id_index + 1 < len(command)
            and command[run_id_index + 1] == run_id)


def cmd_status(args):
    state = _state_for_args(args)
    if state is None:
        _emit(args, {"status": "not_found", "run_id": args.run_id},
              lambda _data: print(_tr("生成履歴はありません。", "No generation runs found.")))
        return EXIT_NO_MATCH
    status = state.get("status", "unknown")
    if status in ("starting", "running") and not _pid_is_run(state):
        status = "stopped"
    labels = {
        "starting": _tr("開始中", "starting"),
        "running": _tr("生成中", "running"),
        "completed": _tr("完了", "completed"),
        "failed": _tr("失敗", "failed"),
        "cancelled": _tr("中止", "cancelled"),
        "stopped": _tr("停止", "stopped"),
        "unknown": _tr("不明", "unknown"),
    }
    payload = dict(state)
    payload["effective_status"] = status
    if payload.get("output"):
        payload["output"] = _repository_output_path(payload["output"])
    advice = _remedies_for_state(args, state)
    if advice:
        payload["remedies"] = [item.as_dict() for item in advice]

    def render(_payload):
        print(_tr("生成状況", "Generation status"))
        print(_tr("実行ID : %s", "Run ID : %s") % state.get("run_id", "-"))
        print(_tr("状態   : %s", "Status : %s") % labels.get(status, status))
        if state.get("stage"):
            stage = _tr(*STAGE_LABELS.get(state["stage"],
                                          (state["stage"], state["stage"])))
            print(_tr("工程   : %s (%s/%s)", "Stage  : %s (%s/%s)") % (
                stage, state.get("stage_index", "?"), state.get("stage_total", "?")))
        if status in ("starting", "running"):
            elapsed, remaining = _run_timing(state)
            if elapsed is not None:
                print(_tr("経過   : %s", "Elapsed: %s") % _short_minutes(elapsed))
            live_remaining = _live_stage_remaining(state)
            if live_remaining is not None:
                print(_tr("残り   : %s（現在の工程を実測中）",
                          "Remaining: %s (measured current stage)")
                      % _short_minutes(live_remaining))
            elif remaining is not None:
                if state.get("eta_source") == "local":
                    print(_tr("残り   : %s（このマシンの実績）",
                              "Remaining: %s (measured on this machine)")
                          % _short_minutes(remaining))
                else:
                    print(_tr("残り   : %s（参照値。実測中）",
                              "Remaining: %s (reference; measuring)")
                          % _short_minutes(remaining))
            else:
                print(_tr("残り   : 実測速度を計測中",
                          "Remaining: measuring live speed"))
        if state.get("error"):
            reason = state["error"]
            if state.get("error_code") == "gpu-out-of-memory":
                context = state.get("error_context") or {}
                requested, free = (context.get("allocation_requested"),
                                   context.get("memory_free"))
                if requested and free:
                    reason = _tr("GPUメモリ不足（空き%s、追加で%s必要）",
                                 "GPU memory exhausted (%s free; %s more requested)") % (
                                     free, requested)
                else:
                    reason = _tr("GPUメモリ不足", "GPU memory exhausted")
            elif state.get("error_code") == "host-out-of-memory":
                reason = _tr("ホストのメモリ不足", "Host memory exhausted")
            print(_tr("原因   : %s", "Reason : %s") % reason)
            _print_remedies(args, state)
        if state.get("output"):
            print(_tr("出力   : %s", "Output : %s")
                  % _repository_output_path(state["output"]))
        if state.get("windows_output"):
            print(_tr("Windows: %s", "Windows: %s") % state["windows_output"])
        elif state.get("windows_export_error"):
            print(_tr("Windowsへのコピー警告: %s", "Windows copy warning: %s")
                  % state["windows_export_error"])
        if state.get("log"):
            print(_tr("ログ   : %s", "Log    : %s") % state["log"])
        if status == "completed" and state.get("output"):
            print(_tr("\n動画を開いて仕上がりを確認してください。",
                      "\nOpen the video to check the result."))
        if status == "stopped":
            print(_tr("生成プロセスが終了しています。ログを確認してください。",
                      "The generation process exited; check the log."))

    _emit(args, payload, render)
    return EXIT_OK if status in ("starting", "running", "completed") else EXIT_ERROR


def cmd_cancel(args):
    from . import runner as runner_mod

    state = _state_for_args(args, running_only=True)
    if state is None:
        return _cli_error(args, _tr("実行中の生成はありません。", "No generation is running."),
                          EXIT_NO_MATCH)
    if state.get("status") not in ("starting", "running"):
        return _cli_error(args, _tr("指定した生成は実行中ではありません。",
                                   "The selected generation is not running."), EXIT_NO_MATCH)
    if not _pid_is_run(state):
        return _cli_error(args, _tr("生成プロセスを確認できません。statusとログを確認してください。",
                                   "The generation process is not active; check status and the log."))
    try:
        runner_mod.ComfyClient(args.server).call("/interrupt", {})
    except (OSError, ValueError):
        pass
    external_container = state.get("external_container")
    if (isinstance(external_container, str)
            and external_container.startswith("nvg-musetalk-")):
        subprocess.run(["docker", "rm", "-f", external_container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    try:
        os.kill(int(state["pid"]), signal.SIGTERM)
    except ProcessLookupError:
        refreshed = run_state.load(args.root, state["run_id"]) or state
        if refreshed.get("status") in ("completed", "failed"):
            return _cli_error(args, _tr("生成はすでに終了しています。",
                                       "The generation has already finished."), EXIT_NO_MATCH)
    run_state.write(args.root, state["run_id"], status="cancelled")
    _emit(args, {"status": "cancelled", "run_id": state["run_id"]},
          lambda _data: print(_tr("生成を中止しました: %s", "Generation cancelled: %s") % state["run_id"]))
    return EXIT_OK


def cmd_report(args):
    """Write results/<run-id>/run.json recording what was run and reviewed."""
    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    env = _env_for(args)
    profile = _resolve_profile(catalog, env, args)
    if profile is None:
        return _report_no_profile(catalog, env, args)
    recipe_options = _saved_recipe_options(args.root, profile)
    profile, recipe, local_calibration = _local_profile(
        profile, catalog, env, recipe_options, enabled=not bool(args.profile))

    record = {
        "schema_version": 1,
        "run_id": args.run_id,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": profile["id"],
        "recipe": profile["recipe"],
        "recipe_options": recipe_options,
        "local_calibration": local_calibration,
        "environment": {
            "platform": env["platform"],
            "kernel": env["kernel"],
            "gpu": detect_mod.primary_gpu(env),
            "memory": env["memory"],
            "windows_host": env.get("windows_host"),
            "container": env["container"],
        },
        # The reviewer's verdict is recorded separately from anything automated,
        # and there is no default: an unreviewed run stays "pending".
        "visual_review": args.visual_review,
        "reviewer_notes": args.notes,
    }
    if args.output:
        try:
            expected = {
                "width": recipe["resolution"][0],
                "height": recipe["resolution"][1],
                "audio_present": True,
            }
            if recipe.get("output_fps"):
                expected["fps"] = recipe["output_fps"]
            probe_options = {}
            if shutil.which("ffprobe") is None and Path(args.output).is_file():
                probe_options = {
                    "command_prefix": ["docker", "exec", args.container, "ffprobe"],
                    "probe_path": _stage_input(args.root, args.output),
                }
            record["result"] = verify_mod.verify_output(
                args.output, expected, **probe_options)
        except RuntimeError as exc:
            record["result"] = {"error": str(exc)}

    target = args.root / "results" / args.run_id
    target.mkdir(parents=True, exist_ok=True)
    path = target / "run.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _emit(args, {"path": str(path), "record": record},
          lambda _data: print(_tr("保存しました: %s", "Saved: %s") % path))
    return EXIT_OK


def cmd_matrix(args):
    """Render docs/hardware-matrix*.md from the profile catalog."""
    from .matrix import MATRIX_PATHS, render_matrix
    catalog = Catalog(args.root, profile_dirs=args.profile_dir)
    if args.write:
        targets = []
        for language, name in MATRIX_PATHS.items():
            target = args.root / "docs" / name
            target.write_text(render_matrix(catalog, language), encoding="utf-8")
            targets.append(target)
        _emit(args, {"paths": [str(target) for target in targets]},
              lambda _data: [print(_tr("保存しました: %s", "Saved: %s") % target)
                             for target in targets])
    else:
        text = render_matrix(catalog, args.language)
        _emit(args, {"markdown": text}, lambda _data: sys.stdout.write(text))
    return EXIT_OK


def cmd_hashes(args):
    """Verify downloaded model weights against manifests/models.lock.yaml."""
    lock = plan_mod.load_models_lock(args.root)
    models_dir = args.root / "models"
    results = []
    for model in lock.get("models", []):
        path = models_dir / model["path"]
        entry = {"id": model["id"], "path": model["path"], "state": "missing"}
        if path.is_file():
            if path.stat().st_size != model["bytes"]:
                entry["state"] = "size-mismatch"
            elif args.quick:
                entry["state"] = "present-unhashed"
            else:
                digest = verify_mod.sha256_file(path)
                entry["state"] = "ok" if digest == model["sha256"] else "hash-mismatch"
                entry["sha256"] = digest
        results.append(entry)

    def render(results):
        for entry in results:
            print("[%-16s] %s" % (entry["state"], entry["path"]))

    _emit(args, results, render)
    bad = [r for r in results if r["state"] in ("size-mismatch", "hash-mismatch")]
    return EXIT_ERROR if bad else EXIT_OK


def _tts_script_text(args):
    if getattr(args, "text", None) and getattr(args, "script", None):
        raise ValueError("use either --text or --script, not both")
    if getattr(args, "text", None):
        return args.text
    if getattr(args, "script", None):
        return Path(args.script).expanduser().read_text(encoding="utf-8")
    raise ValueError("--text or --script is required")


def _run_tts_backend(args, action):
    command = [str(args.root / "scripts" / "tts-backend.sh"), action,
               "--device", args.device]
    if getattr(args, "tts_profile", None):
        command += ["--profile", str(Path(args.tts_profile).resolve())]
    try:
        subprocess.run(command, cwd=str(args.root), check=True,
                       stdout=sys.stderr if args.json else None)
    except subprocess.CalledProcessError as exc:
        raise tts_service.TTSError(
            "TTS %s failed (exit %d); see the message above" %
            (action, exc.returncode)) from exc
    if action == "start":
        for _attempt in range(180):
            if tts_service.backend_health(args.backend)["reachable"]:
                break
            time.sleep(0.5)
        else:
            raise tts_service.TTSError(
                "TTS backend did not become ready within 90 seconds")


def _tts_ui_running(port):
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/config" % port, timeout=1) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        # A LAN-mode server protects the health probe along with every API.
        return exc.code == 401
    except (OSError, urllib.error.URLError):
        return False


def _tts_web_state_path(root):
    return Path(root) / "outputs" / "tts" / "web-server.json"


def _read_tts_web_state(root):
    path = _tts_web_state_path(root)
    if path.is_symlink():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if (not isinstance(record, dict) or not isinstance(record.get("pid"), int)
            or not isinstance(record.get("token"), str)):
        return None
    return record


def _tts_web_process_matches(record):
    """Verify the private token in /proc before signalling a recorded PID."""
    try:
        command = Path("/proc/%d/cmdline" % record["pid"]).read_bytes().split(b"\0")
    except (OSError, KeyError, TypeError, ValueError):
        return False
    token = record.get("token", "").encode("utf-8")
    return bool(token) and token in command and any(
        b"narration-video-gen" in item for item in command)


def _remove_tts_web_state(root, token=None):
    path = _tts_web_state_path(root)
    record = _read_tts_web_state(root)
    if token is not None and (not record or record.get("token") != token):
        return
    if path.is_symlink():
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _lan_ipv4_addresses():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if not address.startswith("127.") and address != "0.0.0.0":
                addresses.add(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
            if not address.startswith("127."):
                addresses.add(address)
        finally:
            probe.close()
    except OSError:
        pass
    return sorted(addresses)


def _tts_web_urls(host, port):
    urls = {"local": "http://127.0.0.1:%d" % port, "lan": []}
    if host in ("0.0.0.0", "::"):
        urls["lan"] = ["http://%s:%d" % (address, port)
                       for address in _lan_ipv4_addresses()]
    return urls


def _tts_web_auth_required(host):
    """Require login for every listen address except an actual loopback."""
    value = str(host or "").strip().strip("[]")
    if value.lower() == "localhost":
        return False
    try:
        return not ipaddress.ip_address(value).is_loopback
    except ValueError:
        # A hostname may resolve beyond this machine. Treat unknown names as
        # network-facing instead of weakening authentication by assumption.
        return True


def _tts_web_status(root):
    record = _read_tts_web_state(root)
    if not record:
        return {"running": False}
    process_matches = _tts_web_process_matches(record)
    running = process_matches and _tts_ui_running(record.get("port", 7861))
    payload = dict(record)
    payload["running"] = running
    if not process_matches:
        _remove_tts_web_state(root, record.get("token"))
    return payload


def _stop_tts_web(root):
    record = _read_tts_web_state(root)
    if not record or not _tts_web_process_matches(record):
        if record:
            _remove_tts_web_state(root, record.get("token"))
        return False
    pid = record["pid"]
    os.kill(pid, signal.SIGTERM)
    for _attempt in range(50):
        if not _tts_web_process_matches(record):
            break
        time.sleep(0.1)
    if _tts_web_process_matches(record):
        os.kill(pid, signal.SIGKILL)
    # The old listener can outlive its command-line identity for a moment.
    # Do not race the replacement bind when switching localhost/LAN modes.
    for _attempt in range(50):
        if not _tts_ui_running(record.get("port", 7861)):
            break
        time.sleep(0.02)
    _remove_tts_web_state(root, record.get("token"))
    return True


def _start_tts_web(root, host, port, backend, allowed_hosts=(), auth_required=False):
    auth_required = bool(auth_required or _tts_web_auth_required(host))
    desired = {"host": host, "port": int(port), "backend": backend,
               "allowed_hosts": list(allowed_hosts), "auth_required": bool(auth_required)}
    current = _tts_web_status(root)
    if current.get("running"):
        if all(current.get(key) == value for key, value in desired.items()):
            return current
        _stop_tts_web(root)
    if _tts_ui_running(port):
        raise tts_service.TTSError(
            "port %d already has an unmanaged TTS review page; stop it or choose --port" % port)

    log_path = Path(root) / "outputs" / "tts" / "web.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    command = [
        sys.executable, str(Path(root) / "bin" / "narration-video-gen"),
        "--root", str(root), "tts", "web", "start", "--serve", "--no-open",
        "--host", host, "--port", str(port), "--backend", backend,
        "--instance-token", token,
    ]
    if auth_required:
        command.append("--auth-required")
    command.append("--managed-web")
    for allowed in allowed_hosts:
        command.extend(("--allow-host", allowed))
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command, cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    urls = _tts_web_urls(host, port)
    record = dict(desired, schema_version=1, pid=process.pid, token=token,
                  started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  log=str(log_path), urls=urls)
    try:
        narration.write_json_atomic(_tts_web_state_path(root), record)
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    try:
        os.chmod(_tts_web_state_path(root), 0o600)
    except OSError:
        pass
    for _attempt in range(50):
        if _tts_ui_running(port):
            record["running"] = True
            return record
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if _tts_web_process_matches(record):
        os.kill(process.pid, signal.SIGTERM)
    _remove_tts_web_state(root, token)
    raise tts_service.TTSError(
        "TTS review page did not start; see %s" % log_path)


def _ensure_tts_ui(args, job_id):
    # A generate command must not decide on its own to publish the unauthenticated
    # review page to the LAN. Listen on loopback unless --ui-host says otherwise.
    port = int(getattr(args, "ui_port", 7861))
    host = getattr(args, "ui_host", "127.0.0.1")
    if not _tts_ui_running(port):
        _start_tts_web(args.root, host, port, args.backend)
    return "http://127.0.0.1:%d/#job=%s" % (port, job_id)


def cmd_tts(args):
    action = args.tts_command
    if action == "web":
        web_action = getattr(args, "web_action", "start")
        if web_action == "status":
            payload = _tts_web_status(args.root)
            public = {key: value for key, value in payload.items() if key != "token"}
            _emit(args, public, lambda data: print(
                _tr("起動中", "running") if data["running"] else
                _tr("停止中", "stopped")))
            if payload.get("running") and not args.json:
                print(_tr("ローカル: %s", "Local: %s") % payload["urls"]["local"])
                for url in payload["urls"].get("lan", []):
                    print("LAN: %s" % url)
            return EXIT_OK if payload.get("running") else EXIT_NO_MATCH
        if web_action == "stop":
            stopped = _stop_tts_web(args.root)
            _emit(args, {"stopped": stopped}, lambda _data: print(
                _tr("停止しました。", "Stopped.") if stopped else
                _tr("停止中です。", "Already stopped.")))
            return EXIT_OK

        if web_action == "reset-password":
            first = getpass.getpass("TTS WebUI password (10+ characters): ")
            second = getpass.getpass("Repeat password: ")
            if first != second:
                raise ValueError("passwords did not match")
            tts_web.set_password(args.root, first)
            _emit(args, {"username": tts_web.AUTH_USER, "password_reset": True},
                  lambda _data: print("TTS WebUI password was reset."))
            return EXIT_OK

        if web_action == "password-status":
            configured = bool(tts_web.password_configured(args.root))
            _emit(args, {"configured": configured, "username": tts_web.AUTH_USER},
                  lambda data: print(_tr("設定済み", "configured") if data["configured"]
                                     else _tr("未設定", "not configured")))
            return EXIT_OK if configured else EXIT_NO_MATCH

        host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
        if args.lan and args.host:
            raise ValueError("use either --lan or --host, not both")
        url = _tts_web_urls(host, args.port)["local"]
        auth_required = _tts_web_auth_required(host)
        if auth_required and not args.managed_web and not tts_web.password_configured(args.root):
            raise ValueError("LAN公開には先に `tts web reset-password` でパスワードを設定して")
        if args.serve:
            try:
                tts_web.serve(args.root, host=host, port=args.port,
                              backend=args.backend,
                              allowed_hosts=args.allow_host or (),
                              auth_required=auth_required or args.auth_required)
            finally:
                if args.instance_token:
                    _remove_tts_web_state(args.root, args.instance_token)
            return EXIT_OK
        if args.foreground:
            if not args.no_open and not args.json:
                threading.Timer(0.5, lambda: _open_browser(url)).start()
            tts_web.serve(args.root, host=host, port=args.port,
                          backend=args.backend,
                          allowed_hosts=args.allow_host or (), auth_required=auth_required)
            return EXIT_OK

        payload = _start_tts_web(args.root, host, args.port, args.backend,
                                 args.allow_host or (), auth_required=auth_required)
        payload["model_prepared"] = tts_service.models_prepared(args.root)
        payload["backend_health"] = tts_service.backend_health(args.backend)
        if not args.no_open and not args.json:
            _open_browser(payload["urls"]["local"])

        def render(data):
            print(_tr("TTS Web UIをバックグラウンドで起動しました。",
                      "Started the TTS Web UI in the background."))
            print(_tr("ローカル: %s", "Local: %s") % data["urls"]["local"])
            for lan_url in data["urls"].get("lan", []):
                print("LAN: %s" % lan_url)
            if host in ("0.0.0.0", "::") and not data["urls"].get("lan"):
                print("LAN: http://<this-PC-IP>:%d" % args.port)
            if auth_required:
                print(_tr("LAN公開: ユーザー名はtts固定です。設定済みパスワードでログインしてください。",
                          "LAN access uses the fixed username tts; log in with the configured password."))
            print(_tr("停止: ./bin/narration-video-gen tts web stop",
                      "Stop: ./bin/narration-video-gen tts web stop"))
        # The instance token authorizes removal of the managed-service state.
        # It is internal process identity, never part of public JSON output.
        _emit(args, {key: value for key, value in payload.items()
                     if key != "token"}, render)
        return EXIT_OK
    if action == "plan":
        parts = narration.split_script(_tts_script_text(args))
        payload = {"parts": narration.serialise_parts(parts)}

        def render(data):
            for item in data["parts"]:
                print("%d. %s  [%d ms]" % (
                    item["index"], item["text"], item["gap_after_ms"]))
        _emit(args, payload, render)
        return EXIT_OK
    if action == "prepare":
        _run_tts_backend(args, "prepare")
        payload = tts_service.record_models_prepared(args.root)
        _emit(args, payload, lambda _data: print(_tr(
            "TTSモデルと実行環境を準備しました。",
            "Prepared the TTS model and runtime.")))
        return EXIT_OK
    if action == "backend":
        if args.backend_action == "status":
            health = tts_service.backend_health(args.backend)
            _emit(args, health, lambda data: print(
                _tr("起動中", "running") if data["reachable"] else
                _tr("停止中", "stopped")))
            return EXIT_OK if health["reachable"] else EXIT_NO_MATCH
        _run_tts_backend(args, args.backend_action)
        return EXIT_OK
    if action == "generate":
        if not tts_service.backend_health(args.backend)["reachable"]:
            raise tts_service.TTSError(
                "TTS backend is stopped; run `narration-video-gen tts backend start`")
        script = _tts_script_text(args)
        output = Path(args.output).expanduser() if args.output else None
        manifest, directory = tts_service.generate_narration(
            args.root, script, args.character, server=args.backend,
            caption=args.caption, seed=args.seed, outro_ms=args.outro_ms,
            output=output, run_asr=not args.skip_asr,
            on_progress=None if args.json else lambda index, total, text: print(
                _tr("%d/%d 生成中: %s", "%d/%d generating: %s") %
                (index, total, text)))
        if args.json:
            payload = dict(manifest)
            payload["directory"] = str(directory)
            _emit(args, payload, lambda _data: None)
        else:
            url = _ensure_tts_ui(args, manifest["job_id"])
            final_path = output.resolve() if output else directory / "narration.wav"
            print(_tr("生成しました: %s", "Generated: %s") % final_path)
            print(_tr("試聴・間合い調整: %s", "Listen and adjust pauses: %s") % url)
        return EXIT_OK
    if action == "adopt":
        if not args.confirm_listened:
            raise tts_service.TTSError(
                "--confirm-listened is required after listening to every part and the joined WAV")
        tts_service.confirm_human_review(args.root, args.job_id)
        target = tts_service.adopt_as_input_set(args.root, args.job_id, name=args.name)
        _emit(args, {"input_set": target.name, "path": str(target)},
              lambda _data: print(_tr("動画入力セット: %s",
                                      "Video input set: %s") % target))
        return EXIT_OK
    if action == "regenerate":
        manifest = tts_service.regenerate_part(
            args.root, args.job_id, args.part, server=args.backend,
            duration_scale=args.duration_scale, seed=args.seed,
            run_asr=not args.skip_asr,
            on_progress=None if args.json else print)
        if args.json:
            _emit(args, manifest, lambda _data: None)
        else:
            print(_tr("再生成しました。試聴: %s", "Regenerated. Review: %s") %
                  _ensure_tts_ui(args, args.job_id))
        return EXIT_OK
    raise ValueError("unknown TTS action: %s" % action)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _positive_float(value):
    try:
        number = float(value)
    except ValueError:
        number = float("nan")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(_tr("0より大きい数値を指定してください。",
                                             "Enter a number greater than zero."))
    return number


def _positive_int(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError(_tr("1以上の整数を指定してください。",
                                             "Enter a whole number greater than zero."))
    return number


def _run_id_argument(value):
    try:
        return run_state.validate_run_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(_tr(
            "実行IDは英数字で始まる128文字以内の英数字・ドット・ハイフン・アンダースコアで指定してください。",
            str(exc))) from exc


def build_parser():
    parser = argparse.ArgumentParser(
        prog="narration-video-gen",
        description=_tr("画像と音声から動画を作成します。初回は plan、準備後は run を実行してください。",
                        "Create a video from an image and audio. Start with plan, then run."),
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="repository root (default: auto-detected)")
    parser.add_argument("--profile-dir", type=Path, action="append", default=[],
                        help="also load named hardware profiles from this directory")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("detect", help="probe this machine and report what it can run")
    p.add_argument("--out", help="also write the environment record to this path")
    p.add_argument("--measure", action="store_true",
                   help="also measure sustained throughput, temperature and clocks")
    p.add_argument("--measure-seconds", type=int, default=measure_mod.DEFAULT_SECONDS,
                   help="length of the measured load in seconds")
    p.set_defaults(func=cmd_detect, env_file=None)

    p = sub.add_parser("select", help="pick the best verified profile for this machine")
    p.add_argument("--profile", help="select one internal profile explicitly (advanced)")
    p.add_argument("--recipe", help="restrict to one recipe id")
    p.add_argument("--model", help="choose wan21 or wan22 (full family ids also work)")
    p.add_argument("--resolution", help="restrict to 480p or 720p")
    p.add_argument("--explain", action="store_true", help="also list excluded profiles")
    p.add_argument("--env-file", help="use a saved environment record instead of probing")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("list", help="list every published profile")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one profile in full")
    p.add_argument("profile", nargs="?", help="profile id (default: saved selection)")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("plan", help=_tr("作成内容を選び、生成に必要なものを準備", "choose settings and prepare generation"))
    p.add_argument("--profile", help="profile id (default: auto-select)")
    p.add_argument("--recipe")
    p.add_argument("--model")
    p.add_argument("--resolution")
    p.add_argument("--seconds", type=_positive_float, help="length of your narration audio")
    p.add_argument("--lip-sync-enhancement", choices=["off", "musetalk"],
                   help="save the standard or experimental MuseTalk pipeline")
    p.add_argument("--face-detailer", choices=["on", "off"],
                   help="enable or disable VACE face refinement in the saved plan")
    p.add_argument("--frame-interpolation", choices=["on", "off"],
                   help="enable the standard RIFE 60 fps finish or keep the source frame rate")
    p.add_argument("--check", action="store_true", help="show preparation status only")
    p.add_argument("--advanced", action="store_true",
                   help=_tr("リップシンク・顔補正の設定も対話で選択", "also choose lip sync and face refinement interactively"))
    p.add_argument("--env-file")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser(
        "calibrate", help="find and persist block swap for the selected plan")
    p.add_argument("--profile", help="base profile (default: saved/auto selection)")
    p.add_argument("--recipe")
    p.add_argument("--model")
    p.add_argument("--resolution")
    p.add_argument("--lip-sync-enhancement", choices=["off", "musetalk"],
                   help="calibrate the standard or MuseTalk plan identity")
    p.add_argument("--face-detailer", choices=["on", "off"],
                   help="calibrate the plan identity with face refinement on or off")
    p.add_argument("--run-id", type=_run_id_argument)
    p.add_argument(
        "--search-mode", choices=["boundary", "fast"], default="boundary",
        help="boundary confirms the exact edge; fast uses the previous safety jump")
    p.add_argument("--import-profile-set", type=Path,
                   help="register an already completed calibration result")
    p.add_argument("--yes", action="store_true",
                   help="start the multi-run GPU search without prompting")
    p.add_argument("--dry-run", action="store_true",
                   help="show the selected calibration without starting GPU work")
    p.add_argument("--env-file")
    p.set_defaults(func=cmd_calibrate, check=False)

    p = sub.add_parser("calibrations", help="list or enable saved local calibrations")
    calibration_sub = p.add_subparsers(dest="calibration_command")
    calibration_sub.add_parser("list", help="list saved calibration records")
    for action in ("enable", "disable"):
        action_parser = calibration_sub.add_parser(
            action, help="%s one calibration record" % action)
        action_parser.add_argument("path", type=Path)
    p.add_argument("--all-machines", action="store_true",
                   help="include records for GPUs other than the current one")
    p.add_argument("--env-file")
    p.set_defaults(func=cmd_calibrations, calibration_command="list")

    p = sub.add_parser("verify", help="check a produced video against its recipe")
    p.add_argument("path")
    p.add_argument("--profile", help="profile the video was produced with")
    p.add_argument("--frames", type=_positive_int, help="expected frame count")
    p.add_argument("--audio", help="narration WAV the run used; derives the frame count")
    p.add_argument("--expect", help="JSON file with expected properties")
    p.add_argument("--env-file", help=argparse.SUPPRESS)
    p.add_argument("--container", default="narration-video-gen-comfy",
                   help="container used when ffprobe is not installed on the host")
    p.set_defaults(func=cmd_verify, recipe=None, model=None, resolution=None)

    p = sub.add_parser("run", help=_tr("入力素材を選び、動画を生成", "choose inputs and generate a video"))
    p.add_argument("--profile", help="profile id (default: auto-select)")
    p.add_argument("--recipe")
    p.add_argument("--model")
    p.add_argument("--resolution")
    p.add_argument("--image", help="portrait image (generating stages)")
    p.add_argument("--audio", help="narration WAV (generation or canonical post-stage audio)")
    p.add_argument("--length", choices=["short", "full"],
                   help="generation length (required for non-interactive generation)")
    p.add_argument("--stages", help="comma-separated subset, e.g. 'rife' or "
                                    "'face-detailer,rife' (default: the recipe's)")
    p.add_argument("--source-video", help="input video when starting from a post-stage")
    p.add_argument("--frames", type=_positive_int, help="frame count when it cannot be measured")
    p.add_argument("--mask-only", action="store_true",
                   help="render the face detailer's tracking mask and stop")
    p.add_argument("--run-id", type=_run_id_argument, help="identifier for outputs/<run-id>/; also enables "
                                     "status and cancel for direct runs")
    p.add_argument("--server", default="127.0.0.1:8188")
    p.add_argument("--container", default="narration-video-gen-comfy",
                   help="container the ffmpeg stages run in")
    p.add_argument("--accept-unverified", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--force", action="store_true",
                   help="proceed even though this machine does not meet the requirements")
    p.add_argument("--dry-run", action="store_true", help="print the workflow and stop")
    p.add_argument("--background-worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--env-file")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help=_tr("生成の進捗と出力先を確認", "show generation progress and output"))
    p.add_argument("--run-id", type=_run_id_argument, help="show one run instead of the latest")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cancel", help=_tr("実行中の生成を中止", "cancel the running generation"))
    p.add_argument("--run-id", type=_run_id_argument, help="cancel one run instead of the latest running run")
    p.add_argument("--server", default="127.0.0.1:8188")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("report", help="record a finished run under results/")
    p.add_argument("--run-id", type=_run_id_argument, required=True)
    p.add_argument("--profile")
    p.add_argument("--output", help="the produced video, to verify and hash")
    p.add_argument("--visual-review", choices=["passed", "failed", "pending"],
                   default="pending")
    p.add_argument("--notes", help="what you saw when you watched it")
    p.add_argument("--container", default="narration-video-gen-comfy",
                   help="container used when ffprobe is not installed on the host")
    p.add_argument("--env-file")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("matrix", help="render the hardware matrix from the catalog")
    p.add_argument("--write", action="store_true",
                   help="write docs/hardware-matrix.md and docs/hardware-matrix_en.md")
    p.add_argument("--language", choices=("ja", "en"), default="ja",
                   help="language printed without --write (default: %(default)s)")
    p.set_defaults(func=cmd_matrix)

    p = sub.add_parser("hashes", help="verify downloaded weights against the lock file")
    p.add_argument("--quick", action="store_true", help="check size only, skip hashing")
    p.set_defaults(func=cmd_hashes)

    p = sub.add_parser("tts", help=_tr("台本から音声を作成する画面を開く", "open the narration creation page"))
    tts_sub = p.add_subparsers(dest="tts_command")
    t = tts_sub.add_parser("web", help="manage the local narration creation UI")
    t.add_argument("web_action", nargs="?", choices=(
        "start", "status", "stop", "reset-password", "password-status"),
                   default="start", help="background service action (default: start)")
    t.add_argument("--lan", action="store_true",
                   help="listen on the LAN as well as localhost (password required)")
    t.add_argument("--host", default=None,
                   help="advanced listen address (default: localhost)")
    t.add_argument("--port", type=int, default=7861)
    t.add_argument("--backend", default=tts_service.DEFAULT_SERVER)
    t.add_argument("--no-open", action="store_true", help="do not open a browser")
    t.add_argument("--foreground", action="store_true",
                   help="keep the web server attached to this terminal")
    t.add_argument("--allow-host", action="append", default=[],
                   help="also accept this Host header (repeatable); IP addresses "
                        "and this machine's own name are always accepted")
    t.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    t.add_argument("--instance-token", help=argparse.SUPPRESS)
    t.add_argument("--auth-required", action="store_true", help=argparse.SUPPRESS)
    t.add_argument("--managed-web", action="store_true", help=argparse.SUPPRESS)
    t.set_defaults(func=cmd_tts)

    t = tts_sub.add_parser(
        "regenerate", help="regenerate one problematic part of a narration job")
    t.add_argument("job_id")
    t.add_argument("part", type=int)
    t.add_argument("--duration-scale", type=float,
                   help="use only for a part whose predicted duration is wrong")
    t.add_argument("--seed", type=int,
                   help="generate this part again with a different sampling seed")
    t.add_argument("--skip-asr", action="store_true")
    t.add_argument("--backend", default=tts_service.DEFAULT_SERVER)
    t.add_argument("--ui-port", type=int, default=7861)
    t.add_argument("--ui-host", default="127.0.0.1",
                   help="listen address for the review page this command starts")
    t.set_defaults(func=cmd_tts)

    t = tts_sub.add_parser("plan", help="preview script splitting without loading a model")
    t.add_argument("--script", help="UTF-8 script file")
    t.add_argument("--text", help="script text supplied directly")
    t.set_defaults(func=cmd_tts)

    t = tts_sub.add_parser(
        "prepare", help="build the local backend and download pinned models")
    t.add_argument("--device", choices=["auto", "cuda", "rocm", "cpu"], default="auto")
    t.set_defaults(func=cmd_tts)

    t = tts_sub.add_parser(
        "backend", help="start, stop, or inspect the local TTS engine")
    t.add_argument("backend_action", choices=["start", "stop", "status"])
    t.add_argument("--tts-profile", help="TTS hardware profile JSON path")
    t.add_argument("--device", choices=["auto", "cuda", "rocm", "cpu"], default="auto")
    t.add_argument("--backend", default=tts_service.DEFAULT_SERVER)
    t.set_defaults(func=cmd_tts)

    t = tts_sub.add_parser(
        "generate", help="generate a WAV from a script using the local engine")
    t.add_argument("--script", help="UTF-8 script file")
    t.add_argument("--text", help="script text supplied directly")
    t.add_argument("--character", choices=sorted(narration.CHARACTERS), default="aoi")
    t.add_argument("--caption", help="override the character's default speaking style")
    t.add_argument("--seed", type=int, help="fixed sampling seed")
    t.add_argument("--outro-ms", type=int, default=1500)
    t.add_argument("--output", help="also copy the completed WAV to this path")
    t.add_argument("--skip-asr", action="store_true",
                   help="skip the recommended per-part Whisper transcript check")
    t.add_argument("--backend", default=tts_service.DEFAULT_SERVER)
    t.add_argument("--ui-port", type=int, default=7861)
    t.add_argument("--ui-host", default="127.0.0.1",
                   help="listen address for the review page this command starts")
    t.set_defaults(func=cmd_tts)

    t = tts_sub.add_parser(
        "adopt", help="turn a completed narration job into a video input set")
    t.add_argument("job_id")
    t.add_argument("--name", help="input set name (default: tts-<job-id>)")
    t.add_argument("--confirm-listened", action="store_true",
                   help="confirm that every part and the joined WAV were reviewed")
    t.set_defaults(func=cmd_tts)

    p.set_defaults(func=cmd_tts, tts_command="web", web_action="start",
                   host=None, lan=False, port=7861,
                   backend=tts_service.DEFAULT_SERVER, no_open=False,
                   foreground=False, allow_host=[], serve=False,
                   instance_token=None)

    def add_json_option(command_parser):
        for action in command_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    child.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                                       help=_tr("JSON形式で出力", "emit machine-readable JSON"))
                    add_json_option(child)

    add_json_option(parser)
    return parser


def _print_getting_started(output=None):
    """First contact: name the two commands instead of an argparse error."""
    output = output or sys.stdout
    print(_tr("動画を生成するには、次の2つを順に実行します。",
              "Generating a video takes these two commands, in order:"), file=output)
    print(_tr("\n  ./bin/narration-video-gen plan   # 内容を選び、モデルと実行イメージを準備",
              "\n  ./bin/narration-video-gen plan   # choose, then prepare models and image"),
          file=output)
    print(_tr("  ./bin/narration-video-gen run    # 入力を選び、動画を生成",
              "  ./bin/narration-video-gen run    # choose inputs and generate"), file=output)
    print(_tr("\n台本から音声を作るには、./bin/narration-video-gen tts を実行します。",
              "\nTo create audio from a script, run ./bin/narration-video-gen tts."), file=output)
    print(_tr("\nコマンド一覧は --help を参照してください。",
              "\nSee --help for the full command list."), file=output)


def main(argv=None):
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code and "--json" in argv:
            print(json.dumps({"error": "Invalid command arguments. See stderr for details."}))
            return exc.code
        raise
    if getattr(args, "command", None) is None:
        if args.json:
            # A bare --json call is an automation mistake, not a request for
            # prose: answer in JSON and keep failing as argparse used to.
            print(json.dumps({
                "error": "no command given",
                "next_commands": ["plan", "run"],
            }, indent=2))
            return EXIT_ERROR
        _print_getting_started()
        return EXIT_OK
    if args.root is None:
        args.root = repo_root()
    else:
        args.root = args.root.resolve()
    if args.command in ("plan", "run", "tts"):
        _warn_windows_mounted_checkout(args)
    args._json_output = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext():
            result = args.func(args)
        if (_tracks_run(args) and result != EXIT_OK):
            current = run_state.load(args.root, args.run_id) or {}
            if current.get("status") not in ("failed", "cancelled", "completed"):
                _worker_error(args, _tr("生成プロセスが終了しました。",
                                        "The generation process exited."))
        if args.json and not getattr(args, "_json_emitted", False):
            payload = {"status": "completed" if result == EXIT_OK else "failed", "exit_code": result}
            if result != EXIT_OK:
                payload["error"] = "Command failed. See stderr for details."
            _emit(args, payload, lambda _data: None)
        return result
    except measure_mod.MeasureError as exc:
        return _cli_error(args, _tr("測定できませんでした: %s", "The measurement could not run: %s") % exc)
    except CatalogError as exc:
        _worker_error(args, str(exc))
        return _cli_error(args, str(exc))
    except (OSError, ValueError, wave.Error, tts_service.TTSError) as exc:
        _worker_error(args, str(exc))
        return _cli_error(args, str(exc))
    except KeyboardInterrupt:
        return _cli_error(args, _tr("中止しました。", "Cancelled."), EXIT_CANCELLED)


if __name__ == "__main__":
    sys.exit(main())
