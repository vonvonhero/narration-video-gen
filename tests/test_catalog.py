"""Tests that the catalog stays internally consistent.

Run with: python3 tests/test_catalog.py   (no pytest required)

These are the checks that would otherwise have to be done by reading YAML
carefully, which is exactly the kind of review that stops happening.
"""

import hashlib
import http.client
from functools import wraps
import inspect
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from narration_video_gen import yamlmin  # noqa: E402
from narration_video_gen.catalog import Catalog, CatalogError  # noqa: E402
from narration_video_gen.compat import load_yaml_file  # noqa: E402
from narration_video_gen.plan import estimate_runtime, required_models, runtime_build_identity  # noqa: E402
from narration_video_gen.select import (confidence_key, evidence_matches_gpu, evaluate,
                                is_fully_verified, select)  # noqa: E402
from narration_video_gen import selection_state  # noqa: E402
from narration_video_gen import run_state  # noqa: E402
from narration_video_gen import calibration_state  # noqa: E402
from narration_video_gen import narration  # noqa: E402
from narration_video_gen.verify import frames_for_audio  # noqa: E402
from narration_video_gen import tts_service  # noqa: E402
from narration_video_gen import tts_web  # noqa: E402
from narration_video_gen import ui_locale  # noqa: E402

FAILURES = []


def expected_ui_language(language):
    """Pin the asserted UI language, including child processes; restore on exit."""
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with patch.dict(os.environ, {"NVG_UI_LANGUAGE": language}):
                return function(*args, **kwargs)
        return run
    return decorate


def check(condition, message):
    if condition:
        print("  ok   %s" % message)
    else:
        print("  FAIL %s" % message)
        FAILURES.append(message)


def section(title):
    print("\n== %s" % title)


def test_ui_language_fixture_restores_environment():
    section("UI language fixture isolation")
    for outer_language in ("ja", "en"):
        with patch.dict(os.environ, {"NVG_UI_LANGUAGE": outer_language}):
            @expected_ui_language("en")
            def english_fixture():
                child = subprocess.run(
                    [sys.executable, "-c",
                     "import os; print(os.environ['NVG_UI_LANGUAGE'])"],
                    capture_output=True, text=True, check=False)
                return ui_locale.language(), child.stdout.strip()

            check(english_fixture() == ("en", "en")
                  and os.environ["NVG_UI_LANGUAGE"] == outer_language,
                  "English UI fixture overrides and restores %s in parent/child" % outer_language)

            @expected_ui_language("ja")
            def failing_fixture():
                raise RuntimeError("fixture cleanup")

            try:
                failing_fixture()
            except RuntimeError:
                pass
            check(os.environ["NVG_UI_LANGUAGE"] == outer_language,
                  "UI fixture restores %s after exceptions" % outer_language)


# ---------------------------------------------------------------------------

def test_bundled_yaml_parser_matches_pyyaml():
    """The fallback parser must agree with PyYAML on every file we ship."""
    section("bundled YAML parser")
    try:
        import yaml
    except ImportError:
        print("  skip PyYAML not installed; cannot cross-check")
        return
    for path in sorted(ROOT.rglob("*.yaml")):
        mine = yamlmin.safe_load(path.read_text(encoding="utf-8"))
        theirs = yaml.safe_load(path.read_text(encoding="utf-8"))
        check(mine == theirs, "matches PyYAML: %s" % path.relative_to(ROOT))


def test_catalog_loads():
    section("catalog")
    catalog = Catalog(ROOT)
    check(len(catalog.profiles) > 0, "profiles load (%d)" % len(catalog.profiles))
    check(len(catalog.recipes) > 0, "recipes load (%d)" % len(catalog.recipes))
    for profile in catalog.sorted_profiles():
        stem = Path(profile["_path"]).stem
        check(stem == profile["id"], "%s: filename matches id" % profile["id"])
    return catalog


def test_timing_reference_drives_runtime_estimate(catalog):
    section("timing observations")
    profile = catalog.profiles["linux-wan21-480p-vram16"]
    estimate = estimate_runtime(profile, catalog.recipe_for(profile), 28.59,
                                catalog=catalog)
    check(estimate.get("known")
          and estimate.get("minutes") == 48.92
          and estimate.get("timing_scope") == "e2e"
          and estimate.get("timing_environment") == "runpod",
          "the designated E2E reference, not a throttled observation, drives ETA")
    observed = estimate.get("observation_range") or {}
    check(observed.get("count") == 2
          and observed.get("min_minutes") == 48.92
          and observed.get("max_minutes") == 75.88,
          "comparable E2E observations remain available as an environment range")

    invalid = json.loads(json.dumps(profile))
    invalid.pop("_path", None)
    invalid["id"] = "invalid-timing"
    invalid["timing"]["reference"]["wall_clock_seconds"] = -1
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "invalid.yaml"
        path.write_text(json.dumps(invalid), encoding="utf-8")
        try:
            Catalog(ROOT, profile_dirs=[Path(directory)])
        except CatalogError as exc:
            rejected = "wall_clock_seconds must be positive" in str(exc)
        else:
            rejected = False
    check(rejected, "invalid timing records are rejected")


def test_external_profile_overlay(catalog):
    section("external profile overlay")
    base = catalog.profiles["linux-wan22-720p-vram16"]
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        first = temporary / "first"
        second = temporary / "second"
        first.mkdir()
        second.mkdir()
        overlay = json.loads(json.dumps(base))
        overlay.pop("_path", None)
        overlay.setdefault("settings", {})["face_detailer_size"] = 384
        (first / "wan22-720p.yaml").write_text(
            json.dumps(overlay), encoding="utf-8")
        loaded = Catalog(ROOT, profile_dirs=[first])
        check(loaded.profiles[base["id"]]["settings"]["face_detailer_size"] == 384
              and loaded.profiles[base["id"]]["_path"]
              == str(first / "wan22-720p.yaml"),
              "an explicit immutable snapshot can replace its repository profile")

        (second / "duplicate.yaml").write_text(
            json.dumps(overlay), encoding="utf-8")
        try:
            Catalog(ROOT, profile_dirs=[first, second])
        except CatalogError as exc:
            rejected = "duplicate external profile id" in str(exc)
        else:
            rejected = False
        check(rejected, "two external overlays cannot ambiguously replace the same id")


def test_composed_stage_runtime_estimate(catalog):
    section("composed runtime estimate")
    profile = json.loads(json.dumps(
        catalog.profiles["linux-wan22-720p-vram16"]))
    profile.pop("timing", None)
    profile["id"] = "partial-wan22-720p"
    profile["evidence"].update({
        "duration_class": "short",
        "frames": 129,
        "seconds": 8.0625,
        "visual_review": "pending",
        "stage_minutes": {"s2v": 54},
    })
    recipe = catalog.recipe_for(profile)
    estimate = estimate_runtime(profile, recipe, 5.5625, catalog=catalog)
    stages = {item["stage"]: item for item in estimate.get("stage_estimates", [])}
    check(estimate.get("known") and estimate.get("composed")
          and estimate.get("minutes") == 40
          and stages.get("s2v", {}).get("source_profile") == "partial-wan22-720p"
          and stages.get("face-detailer", {}).get("source_profile")
          == "linux-wan22-720p-vram16",
          "partial same-GPU stage evidence composes a pre-sampler ETA")


def test_every_recipe_model_is_locked(catalog):
    section("model references resolve")
    for recipe in catalog.recipes.values():
        if recipe.get("kind", "video") == "video":
            check(recipe["short_test_frames"] == 89,
                  "%s: guided short test is 89 frames (about 5.6 seconds)"
                  % recipe["id"])
        if not recipe.get("models"):
            continue
        try:
            models = required_models(ROOT, recipe)
            check(len(models) == len(recipe["models"]),
                  "%s: all %d models resolve" % (recipe["id"], len(models)))
        except ValueError as exc:
            check(False, "%s: %s" % (recipe["id"], exc))


def test_lock_entries_are_complete():
    section("models.lock.yaml completeness")
    lock = load_yaml_file(ROOT / "manifests" / "models.lock.yaml")
    seen = set()
    for entry in lock["models"]:
        for field in ("id", "path", "url", "bytes", "sha256", "license"):
            check(entry.get(field) is not None, "%s: has %s" % (entry["id"], field))
        check(len(entry["sha256"]) == 64, "%s: sha256 is 64 hex chars" % entry["id"])
        check(entry["id"] not in seen, "%s: id is unique" % entry["id"])
        seen.add(entry["id"])


def test_evidence_is_never_stronger_than_it_should_be(catalog):
    """A profile that needs acknowledgement must not be labelled recommended."""
    section("evidence consistency")
    for family in ("wan21", "wan22"):
        capacity_profile = catalog.profiles["windows-%s-480p-vram8-simulated" % family]
        check(capacity_profile["evidence"]["visual_review"] == ("passed" if family == "wan22" else "pending")
              and not is_fully_verified(capacity_profile),
              "%s: Q4 review is separate from physical qualification" % family)
    measured = catalog.profiles["windows-wan22-480p-vram8-simulated"]
    inferred = catalog.profiles["windows-wan21-480p-vram8-simulated"]
    check(measured["evidence"]["duration_class"] == "full"
          and measured["evidence"]["execution_mode"] == "staged-resumed-after-wsl-restart"
          and measured["settings"]["face_detailer_size"] == 320
          and measured["settings"]["face_detailer_blocks_to_swap"] == 40,
          "RAM24 Wan2.2 Q4 full evidence preserves resumed execution and measured face settings")
    check(inferred["qualification_state"] == "inferred"
          and inferred["evidence"]["visual_review"] == "pending"
          and inferred["status"] == "experimental",
          "Wan2.1 Q4 RAM24 is inferred, not measured or visually qualified")
    for profile in (measured, inferred):
        check(profile["requires"]["physical_ram_gib_min"] == 24
              and profile["requires"]["host_ram_gib_min"] == 16
              and profile["requires"]["swap_gib_min"] == 48,
              "%s: RAM24 / WSL16 / swap48 is explicit" % profile["id"])
    for profile in catalog.sorted_profiles():
        evidence = profile["evidence"]
        if (profile["status"] != "not-recommended"
                and profile["requires"].get("gpu_vram_gib_min", 99) <= 12):
            recipe = catalog.recipe_for(profile)
            base = ("wan21-i2v-14b-480p-q4ks" if recipe["model_family"]
                    == "wan21-infinitetalk" else "wan22-s2v-14b-q4ks")
            check(base in recipe["models"] and recipe["id"].endswith("-q4ks"),
                  "%s: selectable low-VRAM profile uses the Q4_K_S base" % profile["id"])
        if profile["status"] == "recommended":
            check(evidence["duration_class"] == "full",
                  "%s: recommended implies full length" % profile["id"])
            check(evidence["gpu_evidence"] == "physical",
                  "%s: recommended implies a physical GPU" % profile["id"])
        if evidence["visual_review"] == "failed":
            check(profile["status"] == "not-recommended",
                  "%s: a rejected result is not-recommended" % profile["id"])
        if evidence["duration_class"] == "gate":
            check(not is_fully_verified(profile),
                  "%s: a gate probe is never fully verified" % profile["id"])
        if evidence["gpu_evidence"] == "capacity-simulated":
            check("simulated" in profile["id"],
                  "%s: simulated results say so in the id" % profile["id"])
        if profile.get("settings", {}).get("tiled_vae"):
            check(profile["status"] == "not-recommended",
                  "%s: tiled VAE is never recommended" % profile["id"])


def test_selection_prefers_the_right_profile(catalog):
    section("selection")

    def env(platform, vram, ram, swap, physical_ram=None, gpu_name="test"):
        record = {
            "platform": platform,
            "gpus": [{"name": gpu_name, "vram_gib": vram,
                      "vram_mib": int(vram * 1024),
                      "driver_version": "999"}],
            "memory": {"ram_gib": ram, "swap_gib": swap},
            "container": {"docker_available": True, "nvidia_runtime": True},
            "disk": {"models": {"free_gib": 500.0}},
        }
        if physical_ram:
            record["windows_host"] = {"physical_ram_gib": physical_ram}
        return record

    # A 24 GiB card must get the unswapped Wan2.2 profile, not the 16 GiB one:
    # the 16 GiB settings would swap blocks that fit and add needless runtime.
    best = select(catalog, env("linux", 24, 63, 32), resolution="480p")["best"]
    check(best and best["id"] == "linux-wan22-480p-vram24",
          "24 GiB Linux picks the unswapped 24 GiB profile (got %s)"
          % (best and best["id"]))

    # An A4000 reports 15.99 GiB, not 16.0. It must still match its own profile.
    best = select(catalog, env("linux", 15.99, 19.46, 32), resolution="480p")["best"]
    check(best and best["id"] == "linux-wan22-480p-vram16",
          "a card reporting 15.99 GiB matches the 16 GiB tier (got %s)"
          % (best and best["id"]))

    best = select(catalog, env("linux", 15.99, 19.46, 32))["best"]
    check(best and best["id"] == "linux-wan22-720p-vram16",
          "16 GiB Linux defaults to Wan 2.2 720p (got %s)"
          % (best and best["id"]))

    # A gate probe must never win, even though its VRAM tier is higher.
    best = select(catalog, env("windows-wsl2", 8, 15.62, 48,
                               physical_ram=23.93), resolution="480p")["best"]
    check(best and best["id"] == "windows-wan22-480p-vram8-simulated",
          "RAM24 / WSL16 / 8 GiB Windows offers the measured Q4 staged-full profile")
    from narration_video_gen.matrix import _cell
    check("途中で再開" in _cell(catalog.profiles["windows-wan22-480p-vram8-simulated"])
          and "推定" in _cell(catalog.profiles["windows-wan21-480p-vram8-simulated"]),
          "public matrix separates resumed Wan2.2 evidence from unmeasured Wan2.1 inference")
    best = select(catalog, env("windows-wsl2", 12, 20, 64, physical_ram=32))["best"]
    check(best and best["id"] == "windows-wan22-480p-vram8-simulated",
          "Windows preserves Wan 2.2 preference with the new Q4 candidate (got %s)"
          % (best and best["id"]))

    best = select(catalog, env("windows-wsl2", 8, 20, 64,
                               physical_ram=32))["best"]
    check(best and best["id"] == "windows-wan22-480p-vram8-simulated",
          "8 GiB Windows defaults to the Wan 2.2 Q4 candidate (got %s)"
          % (best and best["id"]))

    best = select(catalog, env("windows-wsl2", 16, 20, 64,
                               physical_ram=32))["best"]
    check(best and best["id"] == "windows-wan22-720p-vram16",
          "16 GiB Windows defaults to the physical Wan 2.2 profile (got %s)"
          % (best and best["id"]))

    wan21_windows = catalog.profiles["windows-wan21-480p-vram16"]
    a4000 = env("windows-wsl2", 16, 20, 64, physical_ram=32,
                gpu_name="NVIDIA RTX A4000")
    rtx5060ti_wan21 = env("windows-wsl2", 16, 20, 64, physical_ram=32,
                          gpu_name="NVIDIA GeForce RTX 5060 Ti")
    unknown = env("windows-wsl2", 16, 20, 64, physical_ram=32,
                  gpu_name="NVIDIA Unknown 16GB GPU")
    check(is_fully_verified(wan21_windows, a4000)
          and is_fully_verified(wan21_windows, rtx5060ti_wan21)
          and is_fully_verified(wan21_windows, unknown),
          "full physical evidence applies to every eligible GPU in the VRAM tier")
    check(not is_fully_verified(
              wan21_windows,
              env("windows-wsl2", 12, 20, 64, physical_ram=32,
                  gpu_name="NVIDIA Unknown 12GB GPU"))
          and not is_fully_verified(
              wan21_windows,
              env("linux", 16, 20, 64, gpu_name="NVIDIA Unknown 16GB GPU")),
          "verification still requires the profile's VRAM tier and platform")
    check(evidence_matches_gpu(wan21_windows, a4000)
          and evidence_matches_gpu(wan21_windows, rtx5060ti_wan21)
          and not evidence_matches_gpu(wan21_windows, unknown),
          "exact GPU names remain provenance information only")
    unknown_result = select(catalog, unknown, model_family="wan21-infinitetalk",
                            resolution="480p")
    check(unknown_result["best"] is not None
          and unknown_result["best"]["fully_verified"]
          and unknown_result["best"]["settings"]["blocks_to_swap"] == 19,
          "an unknown 16 GiB GPU reuses the verified 16 GiB settings unchanged")

    wan22_windows = catalog.profiles["windows-wan22-480p-vram16"]
    rtx5060ti = env("windows-wsl2", 16, 20, 64, physical_ram=32,
                    gpu_name="NVIDIA GeForce RTX 5060 Ti")
    a4000_wsl = env("windows-wsl2", 16, 20, 64, physical_ram=32,
                    gpu_name="NVIDIA RTX A4000")
    check(is_fully_verified(wan22_windows, rtx5060ti)
          and is_fully_verified(wan22_windows, a4000_wsl)
          and wan22_windows["settings"]["blocks_to_swap"] == 22,
          "additional full-reviewed GPUs reuse the qualified profile settings")

    result = select(catalog, env("windows-wsl2", 16, 19.5, 64,
                                 physical_ram=31.93),
                    model_family="wan22-s2v", resolution="480p")
    check(result["best"]
          and result["best"]["id"] == "windows-wan22-480p-vram16",
          "nominal 20 GiB WSL and 32 GiB Windows RAM tolerate accounting overhead")

    # The re-qualified 720p profile supports a 32 GiB physical host when WSL has
    # 20 GiB RAM and the safety-adjusted 48 GiB swap operating value.
    result = select(catalog, env("windows-wsl2", 16, 20, 48, physical_ram=32),
                    resolution="720p")
    check(result["best"] is not None,
          "Windows 720p is offered on a qualified 32 GiB host")
    result = select(catalog, env("windows-wsl2", 16, 20, 32, physical_ram=32),
                    resolution="720p")
    check(result["best"] is None,
          "Windows 720p requires the 48 GiB swap operating value")

    wan22_720 = catalog.profiles["windows-wan22-720p-vram16"]
    result = select(catalog, env("windows-wsl2", 16, 20, 48,
                                 physical_ram=32,
                                 gpu_name="NVIDIA GeForce RTX 5060 Ti"),
                    model_family="wan22-s2v",
                    resolution="720p")
    check(result["best"] is not None
          and result["best"]["id"] == wan22_720["id"]
          and wan22_720["settings"]["blocks_to_swap"] == 40
          and wan22_720["settings"]["wsl_swap_gib"] == 48
          and wan22_720["settings"]["offload_transformer_before_vae_decode"]
          and not wan22_720["settings"]["encode_tiled"]
          and not wan22_720["settings"]["decode_tiled"],
          "Windows Wan2.2 720p selects the calibrated native bs40 profile")
    check(wan22_720["status"] == "acceptable"
          and wan22_720["evidence"]["duration_class"] == "full"
          and wan22_720["evidence"]["pipeline_stages"] == ["s2v", "face-detailer", "rife", "retime"]
          and wan22_720["evidence"]["visual_review"] == "passed"
          and is_fully_verified(wan22_720, rtx5060ti),
          "Windows Wan2.2 720p resumed full pipeline has confirmed human review")
    check(is_fully_verified(wan22_720, a4000_wsl),
          "Windows Wan2.2 720p qualification applies across eligible 16 GiB GPUs")
    result = select(catalog, env("windows-wsl2", 16, 15.62, 48,
                                 physical_ram=23.93,
                                 gpu_name="NVIDIA GeForce RTX 5060 Ti"),
                    model_family="wan22-s2v", resolution="720p")
    check(result["best"] is not None and result["best"]["fully_verified"]
          and wan22_720["settings"]["wsl_ram_gib"] == 16,
          "Windows Wan2.2 720p is qualified at physical RAM24 / WSL16")
    candidate = catalog.profiles["windows-wan21-720p-vram16-ram24-candidate"]
    check(candidate["status"] == "experimental"
          and candidate["evidence"]["visual_review"] == "pending"
          and not is_fully_verified(candidate, rtx5060ti)
          and catalog.profiles["windows-wan21-720p-vram16"]["requires"]["physical_ram_gib_min"] == 32,
          "untested Wan2.1 RAM24 candidate does not replace RAM32 qualification")

    # Unknown is not sufficient: with no physical RAM reading, withhold.
    result = select(catalog, env("windows-wsl2", 16, 32, 32), resolution="720p")
    check(result["best"] is None,
          "Windows 720p is withheld when physical RAM cannot be measured")

    # A 12 GiB card must not be handed a 16 GiB profile by the VRAM tolerance.
    ok, blockers, _ = evaluate(catalog.profiles["linux-wan21-480p-vram16"],
                               env("linux", 12, 32, 32))
    check(not ok and any("VRAM" in b for b in blockers),
          "a 12 GiB card is rejected by the 16 GiB profile")

    # Explicit internal profile selection is retained as an advanced path.
    result = select(catalog, env("linux", 15.99, 19.46, 32),
                    profile_id="linux-wan21-480p-vram16", include_ineligible=True)
    check(result["best"] and result["best"]["id"] == "linux-wan21-480p-vram16"
          and len(result["eligible"]) == 1,
          "an explicitly named internal profile is evaluated by itself")

    # Recovery advice should use the nearest attainable tuning profile, not a
    # higher-status 24 GiB profile that needs much larger upgrades.
    result = select(catalog, env("linux", 6, 8, 8),
                    model_family="wan21-infinitetalk", resolution="480p",
                    include_ineligible=True)
    check(result["closest"] and result["closest"]["id"]
          == "linux-wan21-480p-vram8-simulated",
          "an unavailable choice explains the nearest internal configuration")


@expected_ui_language("en")
def test_interactive_selection_and_temporary_state(catalog):
    section("guided selection state")
    from narration_video_gen.catalog import CatalogError
    from narration_video_gen.cli import (_choose_plan_recipe_options,
                                 _interactive_plan_profile, _interactive_plan_target,
                                 _interactive_select_request,
                                 _normalise_model_family, _resolve_profile)

    environment = {
        "platform": "linux",
        "gpus": [{"name": "A4000", "vram_gib": 15.99, "vram_mib": 16376,
                  "driver_version": "999"}],
        "memory": {"ram_gib": 19.46, "swap_gib": 32},
        "container": {"docker_available": True, "nvidia_runtime": True},
        "disk": {"models": {"free_gib": 500.0}},
    }
    answers = iter(("1", "2"))
    output = StringIO()
    request = _interactive_select_request(
        catalog, environment, input_fn=lambda _prompt: next(answers), output=output)
    check(request == ("wan21-infinitetalk", "720p"),
          "guided selection asks only for model and resolution")
    rendered = output.getvalue()
    check("Wan 2.1 InfiniteTalk" in rendered and "480p" in rendered and "720p" in rendered,
          "guided selection shows human choices instead of profile ids")

    windows_environment = {
        "platform": "windows-wsl2",
        "gpus": [{"name": "NVIDIA GeForce RTX 5060 Ti", "vram_gib": 15.93,
                  "vram_mib": 16311,
                  "driver_version": "999"}],
        "memory": {"ram_gib": 19.5, "swap_gib": 64},
        "windows_host": {"physical_ram_gib": 31.93},
        "container": {"docker_available": True, "nvidia_runtime": True},
        "disk": {"models": {"free_gib": 500.0}},
    }
    windows_answers = iter(("", ""))
    windows_output = StringIO()
    request = _interactive_select_request(
        catalog, windows_environment,
        input_fn=lambda _prompt: next(windows_answers), output=windows_output)
    windows_rendered = windows_output.getvalue()
    check(request == ("wan22-s2v", "720p")
          and "720p  [recommended]" in windows_rendered
          and "720p  [recommended; unverified]" not in windows_rendered,
          "Windows plan defaults to the fully verified Wan 2.2 profile")
    wan21_windows_answers = iter(("1", "1"))
    wan21_windows_output = StringIO()
    from unittest.mock import patch
    with patch.object(ui_locale, "language", return_value="ja"):
        _interactive_select_request(
            catalog, windows_environment,
            input_fn=lambda _prompt: next(wan21_windows_answers),
            output=wan21_windows_output)
    wan21_windows_rendered = wan21_windows_output.getvalue()
    check("480p  [生成可能・確認済み]" in wan21_windows_rendered
          and "720p  [生成可能・未確認]" in wan21_windows_rendered,
          "resolution choices distinguish verified generation from explicit blockers")

    windows_swap32 = dict(
        windows_environment,
        memory={"ram_gib": 19.5, "swap_gib": 32},
    )
    setup_answers = iter(("2", "2"))
    setup_output = StringIO()
    with tempfile.TemporaryDirectory() as directory:
        setup_root = Path(directory)
        setup_profile, cancelled = _interactive_plan_profile(
            catalog, windows_swap32, setup_root,
            input_fn=lambda _prompt: next(setup_answers), output=setup_output)
        setup_args = SimpleNamespace(
            root=setup_root, lip_sync_enhancement=None, face_detailer=None,
            frame_interpolation=None, check=False, json=False)
        setup_options = _choose_plan_recipe_options(
            catalog, setup_profile, setup_args, interactive=False)
        saved_setup = selection_state.load(setup_root)
        check(not cancelled
              and setup_profile["id"] == "windows-wan22-720p-vram16"
              and setup_options == ["face-detailer-on"]
              and saved_setup["host_setup"]["wsl_swap_gib"] == 48
              and saved_setup["host_setup"]["current_swap_gib"] == 32,
              "Windows keeps a 720p choice that only needs a WSL resource upgrade")
    check("720p  [runnable after setup; verified" in setup_output.getvalue()
          and "This configuration needs a WSL resource change" in setup_output.getvalue(),
          "the guided plan labels swap-remediable 720p instead of unavailable")
    locale_environment = {
        name: os.environ.get(name)
        for name in ("LC_ALL", "NVG_CONFIG_HOME", "NVG_UI_LANGUAGE")
    }
    with tempfile.TemporaryDirectory() as locale_config:
        try:
            os.environ["NVG_CONFIG_HOME"] = locale_config
            os.environ.pop("NVG_UI_LANGUAGE", None)
            os.environ["LC_ALL"] = "ja_JP.UTF-8"
            japanese_output = StringIO()
            japanese_answers = iter(("1", "1"))
            _interactive_select_request(
                catalog, environment,
                input_fn=lambda _prompt: next(japanese_answers),
                output=japanese_output)

            Path(locale_config, "ui-language").write_text("ja\n", encoding="utf-8")
            os.environ["LC_ALL"] = "C.UTF-8"
            saved_japanese = ui_locale.language()
            os.environ["NVG_UI_LANGUAGE"] = "en"
            explicit_english = ui_locale.language()
        finally:
            for name, previous in locale_environment.items():
                if previous is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous
    check("作成内容を選択" in japanese_output.getvalue()
          and "モデル:" in japanese_output.getvalue()
          and "single portrait" not in japanese_output.getvalue(),
          "guided selection follows the Japanese message locale")
    check(saved_japanese == "ja" and explicit_english == "en",
          "explicit and saved UI languages override the Linux locale")
    check(_normalise_model_family("wan21") == "wan21-infinitetalk"
          and _normalise_model_family("wan2.2") == "wan22-s2v",
          "short model names map to internal model families")

    # free_disk_gib_min is the space required for a first download, not the
    # space required to run after every weight is already present. Exercise both
    # saved and filtered selection, which resolve a profile before cmd_plan can
    # apply its later disk check.
    from narration_video_gen import cli
    wan22_environment = dict(environment, disk={"models": {"free_gib": 30.0}})
    wan22 = catalog.profiles["linux-wan22-720p-vram16"]
    original_build_plan = cli.plan_mod.build_plan
    cli.plan_mod.build_plan = lambda *_args, **_kwargs: {"disk": {"sufficient": True}}
    try:
        with tempfile.TemporaryDirectory() as directory:
            disk_root = Path(directory)
            selection_state.save(disk_root, wan22["id"], "wan22-s2v", "720p")
            saved_args = SimpleNamespace(
                root=disk_root, profile=None, recipe=None, model=None, resolution=None)
            filtered_args = SimpleNamespace(
                root=disk_root, profile=None, recipe=None, model="wan22", resolution="720p")
            saved = _resolve_profile(catalog, wan22_environment, saved_args)
            filtered = _resolve_profile(catalog, wan22_environment, filtered_args)
            check(saved and saved["id"] == wan22["id"]
                  and filtered and filtered["id"] == wan22["id"],
                  "ready weights bypass only the stale download disk budget")
    finally:
        cli.plan_mod.build_plan = original_build_plan

    from narration_video_gen.cli import _choose_plan_recipe_options
    with tempfile.TemporaryDirectory() as directory:
        option_root = Path(directory)
        profile = catalog.profiles["linux-wan21-480p-vram16"]
        selection_state.save(
            option_root, profile["id"], "wan21-infinitetalk", "480p")
        option_args = SimpleNamespace(
            root=option_root, lip_sync_enhancement="musetalk",
            face_detailer="on", check=False, json=False)
        options = _choose_plan_recipe_options(
            catalog, profile, option_args, interactive=False)
        saved = selection_state.load(option_root)
        enhanced = catalog.recipe_for(profile, options)
        check(options == ["musetalk", "face-detailer-on"]
              and saved.get("plan_configured") is True
              and saved.get("recipe_options") == ["musetalk", "face-detailer-on"],
              "plan saves the named MuseTalk pipeline choice")
        check(enhanced["pipeline_stages"] ==
              ["infinitetalk", "musetalk", "face-detailer", "rife", "retime"]
              and "musetalk-v15-unet" in enhanced["models"],
              "the MuseTalk option always includes VACE and 60 fps post-stages")
        check(saved.get("recipe_options") == ["musetalk", "face-detailer-on"],
              "the named option remains available for run records")

        without_face = catalog.recipe_for(
            profile, ["musetalk", "face-detailer-off"])
        check(without_face["pipeline_stages"] ==
              ["infinitetalk", "musetalk", "rife", "retime"],
              "Face Detailer can be disabled without losing other plan options")
        required_without_face = {
            model["id"] for model in cli.plan_mod.required_models(ROOT, without_face)}
        check("wan22-fun-vace-a14b-low-fp8" not in required_without_face
              and "sam2.1-hiera-small" not in required_without_face
              and "yunet-face-detector" not in required_without_face,
              "a disabled Face Detailer removes its stage-only downloads")
        profile_default_off = dict(
            profile, settings=dict(profile["settings"],
                                   face_detailer_enabled=False))
        default_off_recipe = catalog.recipe_for(profile_default_off)
        check("face-detailer" not in default_off_recipe["pipeline_stages"],
              "a custom profile can make Face Detailer off by default")

    with tempfile.TemporaryDirectory() as directory:
        face_root = Path(directory)
        profile = catalog.profiles["linux-wan21-480p-vram16"]
        selection_state.save(
            face_root, profile["id"], "wan21-infinitetalk", "480p")
        face_args = SimpleNamespace(
            root=face_root, lip_sync_enhancement="off", face_detailer="off",
            check=False, json=False)
        face_options = _choose_plan_recipe_options(
            catalog, profile, face_args, interactive=False)
        check(face_options == ["face-detailer-off"]
              and selection_state.load(face_root)["recipe_options"]
              == ["face-detailer-off"],
              "plan persists an explicit Face Detailer off choice")

    with tempfile.TemporaryDirectory() as directory:
        interpolation_root = Path(directory)
        profile = catalog.profiles["linux-wan21-480p-vram16"]
        selection_state.save(
            interpolation_root, profile["id"], "wan21-infinitetalk", "480p")
        interpolation_args = SimpleNamespace(
            root=interpolation_root, lip_sync_enhancement="off",
            face_detailer="on", frame_interpolation="off",
            check=False, json=False)
        interpolation_options = _choose_plan_recipe_options(
            catalog, profile, interpolation_args, interactive=False)
        without_interpolation = catalog.recipe_for(profile, interpolation_options)
        check(interpolation_options == ["face-detailer-on", "frame-interpolation-off"]
              and selection_state.load(interpolation_root)["recipe_options"]
              == ["face-detailer-on", "frame-interpolation-off"],
              "plan persists the CLI-only frame interpolation opt-out")
        check(without_interpolation["pipeline_stages"] ==
              ["infinitetalk", "face-detailer", "retime"]
              and without_interpolation["output_fps"] == 16
              and without_interpolation["postprocess"]["retime"]["target_fps"] == 16,
              "the opt-out removes RIFE but retains audio-length retiming at source fps")
        required_without_interpolation = {
            model["id"] for model in cli.plan_mod.required_models(
                ROOT, without_interpolation)}
        check("rife49" not in required_without_interpolation,
              "the opt-out removes the RIFE-only model download")

        preserve_args = SimpleNamespace(
            root=interpolation_root, lip_sync_enhancement=None,
            face_detailer=None, frame_interpolation=None,
            check=False, json=False)
        preserved = _choose_plan_recipe_options(
            catalog, profile, preserve_args, interactive=False)
        check(preserved == interpolation_options,
              "a later bare plan preserves the saved interpolation opt-out")

        restore_args = SimpleNamespace(
            root=interpolation_root, lip_sync_enhancement=None,
            face_detailer=None, frame_interpolation="on",
            check=False, json=False)
        restored_options = _choose_plan_recipe_options(
            catalog, profile, restore_args, interactive=False)
        restored = catalog.recipe_for(profile, restored_options)
        check(restored_options == ["face-detailer-on"]
              and restored["pipeline_stages"] ==
              ["infinitetalk", "face-detailer", "rife", "retime"]
              and restored["output_fps"] == 60,
              "--frame-interpolation on restores the standard 60 fps pipeline")
        parsed = cli.build_parser().parse_args(
            ["plan", "--frame-interpolation", "off"])
        check(parsed.command == "plan" and parsed.frame_interpolation == "off",
              "the opt-out is exposed on plan without changing the wizard")

        try:
            catalog.recipe_for(
                profile, ["frame-interpolation-on", "frame-interpolation-off"])
            check(False, "conflicting interpolation choices are rejected")
        except CatalogError:
            check(True, "conflicting interpolation choices are rejected")

    with tempfile.TemporaryDirectory() as directory:
        plan_root = Path(directory)
        plan_answers = iter(("1", "1"))
        plan_output = StringIO()
        planned, cancelled = _interactive_plan_profile(
            catalog, environment, plan_root,
            input_fn=lambda _prompt: next(plan_answers), output=plan_output)
        saved = selection_state.load(plan_root)
        check(not cancelled and planned
              and planned["id"] == "linux-wan21-480p-vram16"
              and planned["platform"] == "linux"
              and saved and saved["profile"] == planned["id"],
              "argument-free plan returns the complete profile and saves its selection")

        switch_answers = iter(("2", "2", "1"))
        switched, cancelled = _interactive_plan_target(
            catalog, environment, plan_root,
            input_fn=lambda _prompt: next(switch_answers), output=StringIO())
        switch_args = SimpleNamespace(
            root=plan_root, lip_sync_enhancement=None, face_detailer=None,
            check=False, json=False)
        switched_options = _choose_plan_recipe_options(
            catalog, switched, switch_args, interactive=False)
        saved = selection_state.load(plan_root)
        check(not cancelled and switched and switched["id"] == "linux-wan22-480p-vram16"
              and switched_options == ["face-detailer-on"]
              and saved and saved["profile"] == switched["id"]
              and saved.get("plan_configured") is True,
              "a saved plan can interactively switch to Wan 2.2 for the next run")
    downloader = (ROOT / "scripts" / "download-models.sh").read_text(encoding="utf-8")
    check("Usage: scripts/download-models.sh [--profile <profile-id>] [--profile-dir <directory>]..." in downloader
          and 'plan_args=()' in downloader
          and 'global_plan_args=()' in downloader
          and 'plan "${plan_args[@]}"' in downloader,
          "model download reuses saved selection and custom profile directories")
    check('Model download: %d required, %d already present, %d to download (%s)'
          in downloader
          and '[downloading]' in downloader
          and 'NVG_DOWNLOAD_PROGRESS' in downloader
          and 'shutil.get_terminal_size' in downloader
          and 'all:%d%%' in downloader
          and '%d remaining' in downloader
          and 'line.ljust(width)' in downloader
          and '\\033[K' not in downloader
          and 'ETA:%s' in downloader
          and 'curl", "-fsSL"' in downloader,
          "model downloads keep compact cross-terminal progress and an overall ETA")
    windows_setup = (ROOT / "scripts" / "setup-windows.ps1").read_text(
        encoding="utf-8-sig")
    check("[Console]::WindowWidth" in windows_setup
          and "COLUMNS=$progressColumns NVG_DOWNLOAD_PROGRESS=inline"
          in windows_setup,
          "Windows setup passes its console width to single-line model progress")
    check('cannot write model directory %s. It may have been created by Docker as root'
          in downloader
          and 'sudo chown %s:%s %s' in downloader,
          "model downloads explain bind-mount ownership failures before invoking curl")
    launcher = (ROOT / "scripts" / "up.sh").read_text(encoding="utf-8")
    check('mkdir -p "$root/models/rife"' in launcher,
          "the Compose launcher creates the RIFE bind-mount source as the project user")

    with tempfile.TemporaryDirectory() as directory:
        state_root = Path(directory)
        try:
            path = selection_state.save(
                state_root, "linux-wan21-720p-vram16", "wan21-infinitetalk", "720p")
            payload = selection_state.load(state_root)
            check(payload and payload["profile"] == "linux-wan21-720p-vram16"
                  and path.stat().st_mode & 0o077 == 0,
                  "selection state is private and scoped to this checkout and boot")

            args = SimpleNamespace(root=state_root, profile=None, recipe=None,
                                   model=None, resolution=None)
            profile = _resolve_profile(catalog, environment, args)
            check(profile and profile["id"] == "linux-wan21-720p-vram16",
                  "an argument-free downstream command reuses the saved selection")

            incompatible = dict(environment)
            incompatible["memory"] = {"ram_gib": 8, "swap_gib": 8}
            try:
                _resolve_profile(catalog, incompatible, args)
                refused = False
            except CatalogError as exc:
                refused = ("no longer matches" in str(exc)
                           or "適合しません" in str(exc))
            check(refused, "saved selection is rechecked instead of silently reused")

            selection_state.clear(state_root)
            path.write_text("[]\n", encoding="utf-8")
            path.chmod(0o600)
            check(selection_state.load(state_root) is None,
                  "selection state ignores valid JSON with the wrong structure")
            selection_state.clear(state_root)
            target = state_root / "must-not-be-deleted"
            target.write_text("safe", encoding="utf-8")
            path.symlink_to(target)
            check(selection_state.load(state_root) is None,
                  "selection state rejects a symlink")
            selection_state.clear(state_root)
            check(path.is_symlink() and target.read_text(encoding="utf-8") == "safe",
                  "selection cleanup does not follow or remove a symlink target")
            path.unlink()
        finally:
            selection_state.clear(state_root)


def test_frame_rounding():
    section("frame count rounding")
    # Wan packs latents in groups of four: the count must satisfy 4n+1.
    for seconds, fps, expected in [(26.67, 16, 429), (29.43, 16, 473), (8.0625, 16, 129)]:
        got = frames_for_audio(seconds, fps)
        check(got == expected, "%.4f s at %d fps -> %d (got %d)"
              % (seconds, fps, expected, got))
        check((got - 1) % 4 == 0, "%d satisfies 4n+1" % got)


def test_cli_inputs_are_container_addressable():
    section("container input paths")
    from narration_video_gen.cli import _stage_input

    asset = ROOT / "assets" / "characters" / "sakura" / "images" / \
        "sakura-portrait-seated-01.png"
    check(_stage_input(ROOT, asset, loader=True)
          == "characters/sakura/images/sakura-portrait-seated-01.png",
          "nested asset keeps its path below the ComfyUI input root")

    with tempfile.TemporaryDirectory() as directory:
        temporary_root = Path(directory) / "repo"
        temporary_root.mkdir()
        external = Path(directory) / "portrait.png"
        external.write_bytes(b"external test input")
        name = _stage_input(temporary_root, external, loader=True)
        check(name.startswith(".narration-video-gen-inputs/") and name.endswith("-portrait.png [output]"),
              "external loader input is staged and annotated as output")
        relative = name.removesuffix(" [output]")
        staged = temporary_root / "outputs" / relative
        check(staged.read_bytes() == external.read_bytes(),
              "staged input has the original content")
        check(_stage_input(temporary_root, staged)
              == "/opt/ComfyUI/output/%s" % relative,
              "an outputs video maps to its absolute container path")


def test_wsl2_allocator_workaround_defaults_on_with_opt_out():
    section("WSL2 allocator workaround default and opt-out")
    lock = load_yaml_file(ROOT / "manifests" / "containers.lock.yaml")
    workaround = lock["wsl2_allocator_workaround"]
    source = ROOT / workaround["source"]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    check(digest == workaround["source_sha256"],
          "interposer source matches containers.lock.yaml")

    wsl = load_yaml_file(ROOT / "docker" / "compose.wsl2.yaml")
    legacy = load_yaml_file(
        ROOT / "docker" / "compose.wsl2-rdma-workaround.yaml")
    linux = load_yaml_file(ROOT / "docker" / "compose.linux.yaml")
    shared = load_yaml_file(ROOT / "docker" / "compose.yaml")
    wsl_env = (wsl.get("services", {}).get("comfy") or {}).get(
        "environment", {})
    legacy_env = legacy["services"]["comfy"]["environment"]
    linux_env = linux["services"]["comfy"].get("environment", {})
    shared_env = shared["services"]["comfy"]["environment"]
    check(shared_env.get("PYTORCH_CUDA_ALLOC_CONF") == "expandable_segments:True",
          "all GPU runs enable the allocator used by the published evidence")
    check("LD_PRELOAD" not in wsl_env,
          "the base WSL2 overlay leaves activation to the launcher")
    check(legacy_env.get("LD_PRELOAD") == workaround["output"],
          "the legacy WSL2 overlay loads the pinned allocator shim")
    check("LD_PRELOAD" not in linux_env,
          "Linux does not load the WSL2 allocator shim")
    check("TORCH_CUDA_EXPANDABLE_SEGMENTS_RDMA" not in wsl_env,
          "unsupported stock-PyTorch environment variable is absent")
    launcher = (ROOT / "scripts" / "up.sh").read_text(encoding="utf-8")
    check("NVG_WSL2_ALLOCATOR_WORKAROUND" in launcher
          and "workaround_default=1" in launcher
          and "compose.wsl2-rdma-workaround.yaml" in launcher,
          "the Compose launcher enables the compatibility overlay by default under WSL2")

    from narration_video_gen.cli import _calibration_container_environment
    image_plan = {"build_sha256": "test-build"}
    calibration_wsl = _calibration_container_environment(
        {"platform": "windows-wsl2"}, image_plan)
    calibration_linux = _calibration_container_environment(
        {"platform": "linux"}, image_plan)
    check("LD_PRELOAD=/opt/vmm-rdma-interpose.so" in calibration_wsl,
          "WSL2 local calibration enables the shim by default")
    check("LD_PRELOAD=/opt/vmm-rdma-interpose.so" not in calibration_linux,
          "Linux local calibration does not load the WSL2 allocator shim")

    previous = os.environ.get("NVG_WSL2_ALLOCATOR_WORKAROUND")
    try:
        os.environ["NVG_WSL2_ALLOCATOR_WORKAROUND"] = "0"
        opted_out_calibration_wsl = _calibration_container_environment(
            {"platform": "windows-wsl2"}, image_plan)
        opted_out_calibration_linux = _calibration_container_environment(
            {"platform": "linux"}, image_plan)
    finally:
        if previous is None:
            os.environ.pop("NVG_WSL2_ALLOCATOR_WORKAROUND", None)
        else:
            os.environ["NVG_WSL2_ALLOCATOR_WORKAROUND"] = previous
    check("LD_PRELOAD=/opt/vmm-rdma-interpose.so" not in opted_out_calibration_wsl,
          "explicit opt-out disables the shim during WSL2 local calibration")
    check("LD_PRELOAD=/opt/vmm-rdma-interpose.so" not in opted_out_calibration_linux,
          "the WSL2 setting never affects Linux calibration")


def test_container_wrappers_reconstruct_compose_environment():
    section("container Compose environment")
    helper = ROOT / "scripts" / "lib" / "container-env.sh"
    build_script = ROOT / "scripts" / "build-image.sh"
    up_script = ROOT / "scripts" / "up.sh"
    lock = load_yaml_file(ROOT / "manifests" / "containers.lock.yaml")
    comfy_commit = next(
        component["commit"] for component in lock["components"]
        if component["name"] == "ComfyUI"
    )
    build_sha = runtime_build_identity(ROOT)
    expected_image = "narration-video-gen-comfy:%s" % build_sha[:12]

    loaded = subprocess.run(
        ["bash", "-c", '. "$1"; load_container_env "$2"; '
         'printf "%s\\n%s\\n%s\\n%s\\n%s\\n" "$COMFY_IMAGE" "$BASE_DIGEST" '
         '"$TRANSFORMERS_VERSION" "$COMFYUI_COMMIT" "$RUNTIME_BUILD_SHA"',
         "container-env-test", str(helper), str(ROOT)],
        capture_output=True, text=True, check=False,
    )
    values = loaded.stdout.splitlines()
    check(loaded.returncode == 0 and values == [
        expected_image, lock["base_image"]["digest"],
        str(lock["python_runtime"]["transformers"]), comfy_commit, build_sha,
    ], "shared helper derives the image tag and pinned build identity")

    build_text = build_script.read_text(encoding="utf-8")
    up_text = up_script.read_text(encoding="utf-8")
    check("load_container_env \"$root\"" in build_text,
          "image build loads its Compose environment")
    check("load_container_env \"$root\"" in up_text,
          "container startup independently loads its Compose environment")
    compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    check("BASE_IMAGE: ${BASE_IMAGE:?" in compose
          and "BASE_DIGEST: ${BASE_DIGEST:?" in compose
          and "TRANSFORMERS_VERSION: ${TRANSFORMERS_VERSION:?" in compose
          and "RUNTIME_BUILD_SHA: ${RUNTIME_BUILD_SHA:?" in compose,
          "Compose passes pinned runtime dependencies and build identity")
    dockerfile = (ROOT / "docker" / "comfy.Dockerfile").read_text(encoding="utf-8")
    check("io.narration-video-gen.runtime-build-sha" in dockerfile
          and 'transformers==%s\\n' in dockerfile
          and '"output_hidden_states" in parameters' in dockerfile,
          "the image pins and validates the InfiniteTalk Transformers API")

    with tempfile.TemporaryDirectory() as directory:
        identity_root = Path(directory)
        (identity_root / "manifests").mkdir()
        (identity_root / "docker").mkdir()
        shutil.copy2(ROOT / "manifests" / "containers.lock.yaml",
                     identity_root / "manifests" / "containers.lock.yaml")
        shutil.copy2(ROOT / "docker" / "comfy.Dockerfile",
                     identity_root / "docker" / "comfy.Dockerfile")
        shutil.copy2(ROOT / ".dockerignore", identity_root / ".dockerignore")
        shutil.copytree(ROOT / "docker" / "patches",
                        identity_root / "docker" / "patches")
        shutil.copytree(ROOT / "docker" / "nvg_nodes",
                        identity_root / "docker" / "nvg_nodes",
                        ignore=shutil.ignore_patterns("__pycache__", "*.py[co]"))
        (identity_root / "src" / "narration_video_gen").mkdir(parents=True)
        calibration_source = identity_root / "src" / "narration_video_gen" / "cli.py"
        calibration_source.write_text("# calibration CLI\n", encoding="utf-8")
        before = runtime_build_identity(identity_root)
        ignored_cache = identity_root / "docker" / "nvg_nodes" / "__pycache__"
        ignored_cache.mkdir()
        (ignored_cache / "node.cpython-312.pyc").write_bytes(b"local bytecode cache")
        cache_changed = runtime_build_identity(identity_root)
        lock_path = identity_root / "manifests" / "containers.lock.yaml"
        lock_path.write_text(
            lock_path.read_text(encoding="utf-8").replace(
                "  comfy: null", "  comfy: sha256:release-metadata"),
            encoding="utf-8")
        metadata_only = runtime_build_identity(identity_root)
        lock_with_metadata = lock_path.read_bytes()
        lock_path.write_text(
            lock_path.read_text(encoding="utf-8").replace(
                "  transformers: 5.15.0", "  transformers: 5.14.0"),
            encoding="utf-8")
        runtime_dependency_changed = runtime_build_identity(identity_root)
        lock_path.write_bytes(lock_with_metadata)
        dockerignore_path = identity_root / ".dockerignore"
        dockerignore_path.write_text(
            dockerignore_path.read_text(encoding="utf-8") + "\n# changed context rules\n",
            encoding="utf-8")
        context_changed = runtime_build_identity(identity_root)
        dockerignore_path.write_bytes((ROOT / ".dockerignore").read_bytes())
        patch_path = next((identity_root / "docker" / "patches").iterdir())
        patch_path.write_text(
            patch_path.read_text(encoding="utf-8") + "\n# identity test\n",
            encoding="utf-8")
        patch_changed = runtime_build_identity(identity_root)
        calibration_source.write_text(
            "# calibration CLI changed\n", encoding="utf-8")
        calibration_changed = runtime_build_identity(identity_root)
        dockerfile_path = identity_root / "docker" / "comfy.Dockerfile"
        dockerfile_path.write_text(
            dockerfile_path.read_text(encoding="utf-8") + "\n# changed build definition\n",
            encoding="utf-8")
        build_changed = runtime_build_identity(identity_root)
    check(before == cache_changed and cache_changed == metadata_only
          and metadata_only != runtime_dependency_changed
          and metadata_only != context_changed
          and metadata_only != patch_changed
          and patch_changed != calibration_changed
          and calibration_changed != build_changed,
          "runtime identity ignores caches/release metadata and tracks dependencies, context, patches, calibration CLI, and build changes")
    start_ref_patch = (ROOT / "docker" / "patches" /
                       "wanvideowrapper-start-from-ref.patch").read_text(encoding="utf-8")
    check('"start_from_ref": ("BOOLEAN"' in start_ref_patch
          and '"ref_hold_frames": ("INT"' in start_ref_patch
          and 'generated frame"}),' in start_ref_patch,
          "start-from-reference patch declares configurable hold frames with valid input separators")
    check('"offload_transformer_before_vae_decode": ("BOOLEAN"' in start_ref_patch
          and "s2v_decode_offload and force_offload" in start_ref_patch,
          "FramePack decode offload is an explicit opt-in node setting")
    check("self.cache_state = [None] * len(self.cache_state)" in start_ref_patch,
          "FramePack transformer reload resets cache identifiers after clearing cache data")
    check("Offloading initial S2V FramePack continuation frames" not in start_ref_patch
          and "ref_motion_image = ref_motion_image.cpu()" not in start_ref_patch
          and "Keeping S2V FramePack continuation frames" in start_ref_patch
          and 'torch.device("cpu")' in start_ref_patch
          and "image_for_output if s2v_decode_offload else image" in start_ref_patch,
          "the initial FramePack buffer stays on GPU and later windows offload it")
    check("image_for_output = image.cpu()" in start_ref_patch
          and "if r + 1 < s2v_num_repeat" in start_ref_patch,
          "FramePack output stays on CPU and final windows skip unused continuation buffers")
    check("del ref_motion, input_motion_latents, latent" in start_ref_patch
          and "del sample_scheduler, timesteps" in start_ref_patch,
          "completed-window tensors and scheduler state leave VRAM before S2V reload")
    check("torch.isfinite(value).all().item()" in start_ref_patch
          and 'check_framepack_tensor(latent, "sampled latent", r)' in start_ref_patch
          and 'check_framepack_tensor(image, "decoded video", r)' in start_ref_patch
          and 'check_framepack_tensor(noise_pred, f"prediction step {i + 1}", r)' in start_ref_patch
          and 'check_framepack_tensor(ref_motion, "motion latent", r)' in start_ref_patch,
          "FramePack checks finite motion, sampled latents, and decoded video per window")
    check("spatially and temporally constant decoded video" in start_ref_patch,
          "FramePack refuses a completely constant decoded window instead of claiming success")

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        mock_docker = temporary / "docker"
        mock_docker.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n%s\\n%s\\n%s\\n%s\\n' \"$COMFY_IMAGE\" \"$BASE_DIGEST\" "
            "\"$COMFYUI_COMMIT\" \"$RUNTIME_BUILD_SHA\" \"$*\" > \"$DOCKER_CAPTURE\"\n",
            encoding="utf-8",
        )
        mock_docker.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = "%s%s%s" % (
            temporary, os.pathsep, environment.get("PATH", ""))

        captures = []
        results = []
        for script, arguments, capture_name in (
                (build_script, [], "build.capture"),
                (up_script, ["config"], "up.capture")):
            capture = temporary / capture_name
            environment["DOCKER_CAPTURE"] = str(capture)
            results.append(subprocess.run(
                [str(script), *arguments], env=environment,
                capture_output=True, text=True, check=False,
            ))
            captures.append(capture.read_text(encoding="utf-8").splitlines()
                            if capture.exists() else [])

        check(all(result.returncode == 0 for result in results),
              "build and startup wrappers run in separate clean processes")
        check(all(lines[:4] == [expected_image, lock["base_image"]["digest"],
                               comfy_commit, build_sha]
                  for lines in captures),
              "both wrappers pass the same independently reconstructed pins to Docker")
        check(len(captures[0]) == 5 and captures[0][4].endswith("build comfy"),
              "build wrapper invokes the pinned Compose build")
        check(len(captures[1]) == 5 and captures[1][4].endswith("config"),
              "startup wrapper invokes Compose without inherited build exports")


@expected_ui_language("en")
def test_linux_setup_is_safe_and_pinned():
    section("Linux setup")
    script = ROOT / "scripts" / "setup-linux.sh"
    text = script.read_text(encoding="utf-8")
    check(script.stat().st_mode & 0o111,
          "setup script is executable")
    check("set -euo pipefail" in text,
          "setup script fails on unset variables and command errors")
    check("mode=\"wizard\"" in text and "wizard)" in text,
          "no-argument mode is the interactive wizard")
    check("--check | --setup | --install-driver | --test-gpu | --remove-swap" in text,
          "setup documents its separated operating modes")
    check('choose only one of --check, --setup, --install-driver, or --remove-swap' in text,
          "setup rejects conflicting operating modes")
    check('--test-gpu is a standalone action; --check is always read-only' in text,
          "check cannot be combined with an action")
    check('confirm_reboot' in text and 'systemctl reboot' in text
          and 'need_confirmation "--reboot"' not in text,
          "reboot has a dedicated interactive confirmation")
    check('confirm_optional_reboot' in text
          and 'activate_docker_group_or_reboot' in text
          and 'SSH_CONNECTION' in text and 'SSH_TTY' in text
          and 'if confirm_optional_reboot' in text,
          "docker group activation offers SSH relogin or an optional reboot")
    check("WSL_INTEROP" in text and "grep -qi microsoft" in text
          and '--setup is disabled under WSL' in text
          and '--install-driver is disabled under WSL' in text
          and '--remove-swap is disabled under WSL' in text,
          "WSL host changes are refused")
    check("account_in_docker_group" in text
          and "session_has_docker_group" in text,
          "configured and active docker groups are checked separately")
    check('id -G "$user"' in text and '"$user" = "root"' in text
          and '"$(id -u)" -eq 0' in text,
          "primary docker groups and direct root execution are handled")
    check("driver-install-boot-id" in text and "current_boot_id" in text,
          "driver reboot state is tied to the current boot")
    check("clear_driver_marker_if_satisfied" in text
          and text.count("clear_driver_marker_if_satisfied") >= 5,
          "guided and explicit successful actions consume the driver marker")
    wizard_start = text.index("run_wizard()")
    docker_cancel = text.index('say "Dockerのセットアップを中止しました。"',
                               wizard_start)
    docker_prompt = text.rfind("if ! confirm; then", wizard_start,
                               docker_cancel)
    docker_cleanup = text.index("clear_driver_marker_if_satisfied",
                                docker_cancel)
    check(text.find("clear_driver_marker_if_satisfied", wizard_start,
                    docker_prompt) == -1
          and docker_prompt < docker_cleanup,
          "guided setup defers marker cleanup until after approval")
    check("NVIDIAドライバーを利用できません。Secure Boot/MOKとログを確認" in text,
          "a stale driver marker is reported without assuming when failure began")
    check("nvidia_runtime_registered_privileged" in text,
          "approved actions recheck the NVIDIA runtime with elevated access")
    check("今すぐ再起動しますか？ [y/N]:" in text
          and "プロセス、コンテナ、リモート接続がすべて停止" not in text,
          "reboot confirmation stays concise")
    check("Linux setup status" in text
          and "[OK] NVIDIA GPU detected:" in text
          and "[NG] NVIDIA driver is not usable" in text
          and "[--] Docker Engine: ドライバーセットアップ後に確認" in text,
          "status output uses the documented fixed hierarchy")
    check("続行しますか？ [y/N]:" in text
          and "Continue? [y/N]:" in text
          and "今すぐ更新しますか？ [y/N]:" in text
          and "Refresh now? [y/N]:" in text
          and "Type yes to continue" not in text,
          "interactive confirmation is concise and defaults to No")
    check('${LC_ALL:-${LC_MESSAGES:-${LANG:-C}}}' in text
          and 'ui_config_dir/ui-language' in text
          and 'NVG_UI_LANGUAGE' in text
          and 'ui_language="ja"' in text,
          "setup language prefers app settings and falls back to the OS locale")
    check("apt-get update" in text and "apt-get upgrade -y" in text
          and "apt_upgrade_count" in text,
          "guided setup refreshes APT and offers package upgrades separately")
    check("apt-preflight" in text and "apt_preflight_max_age=86400" in text,
          "APT preflight is not repeated on every guided continuation")
    check("Skipped; continuing setup." in text
          and "return 10" not in text,
          "declining the APT refresh continues the guided setup")
    check(text.count('sudo_run rm -f "$marker_tmp"') >= 3,
          "partial APT preflight markers are cleaned up")
    check("system_reboot_pending" in text
          and "reboot before installing the NVIDIA driver" in text,
          "pending system reboots block driver installation")
    check('0300|0302' in text and '0x0300*|0x0302*' in text,
          "NVIDIA PCI detection accepts display-class devices only")
    check('24.04:noble|26.04:resolute)' in text,
          "setup supports Ubuntu 24.04 and 26.04 only")
    check('case "${VERSION_ID:-}:${VERSION_CODENAME:-}"' in text,
          "setup validates Ubuntu version and codename as a pair")
    check("Suites: $ubuntu_codename" in text and "Suites: noble" not in text,
          "Docker repository follows the validated Ubuntu codename")
    check("nvidia-smi -L" in text and 'say "NVIDIAドライバー: OK"' in text,
          "setup preserves a usable NVIDIA driver")
    check("has_nvidia_pci_device" in text
          and "no NVIDIA GPU is visible; check GPU passthrough or hardware" in text,
          "setup refuses driver installation without an NVIDIA PCI device")
    check("docker ps -q" in text and "restarts Docker" in text,
          "setup stops before changing Docker with running containers")
    linux_requirements = [
        load_yaml_file(path)["requires"]
        for path in (ROOT / "profiles" / "linux").glob("*.yaml")
    ]
    linux_swap_targets = [req.get("swap_gib_min", 0) for req in linux_requirements]
    check(max(linux_swap_targets) == 32 and "swap_target_gib=32" in text,
          "guided swap target covers the largest Linux profile requirement")
    check('managed_swapfile="/swapfile-narration-video-gen"' in text
          and "swap_missing_gib" in text
          and "recommended_profile_with_target_swap" not in text
          and "推奨プロファイル" not in text
          and "既存swapは変更せず、${add_gib} GiBを追加し" in text
          and "合計${swap_target_gib} GiBへ永続化します" in text,
          "guided setup adds only missing swap without inventing a profile reason")
    check('ext4|xfs)' in text and "swap_disk_reserve_gib" in text
          and "swap_os_reserve_gib=20" in text
          and "catalog_model_disk_reserve_gib" in text
          and max(req.get("free_disk_gib_min", 0)
                  for req in linux_requirements) == 80,
          "swap creation is limited by filesystem and disk reserves")
    fstab_start = text.index("write_managed_swap_fstab()")
    fstab_end = text.index("\n}\n", fstab_start)
    fstab_body = text[fstab_start:fstab_end]
    check("findmnt --verify --tab-file" in fstab_body
          and fstab_body.index("findmnt --verify --tab-file")
              < fstab_body.index('sudo_run cp --preserve=mode,ownership "$fstab_path" "$backup"')
              < fstab_body.index('sudo_run mv "$fstab_tmp" "$fstab_path"'),
          "managed fstab is validated and backed up before replacement")
    remove_start = text.index("remove_managed_swap()")
    remove_end = text.index("\n}\n", remove_start)
    remove_body = text[remove_start:remove_end]
    check("swapoff" in remove_body
          and remove_body.index("swapoff")
              < remove_body.index("remove_managed_swap_fstab")
              < remove_body.index('sudo_run rm -f "$managed_swapfile"'),
          "managed swap removal disables swap before restoring fstab and deleting the file")
    check("MemAvailable:" in remove_body
          and "used_bytes + 1073741824" in remove_body,
          "managed swap removal refuses unsafe page migration")
    lock = load_yaml_file(ROOT / "manifests" / "containers.lock.yaml")
    probe = lock.get("gpu_probe_image", {})
    check(probe.get("reference", "").startswith("nvidia/cuda:"),
          "GPU probe image is recorded in the container lock")
    check(len(probe.get("digest", "").removeprefix("sha256:")) == 64,
          "GPU probe image is pinned by digest")
    check('lock["gpu_probe_image"]' in text,
          "setup reads the GPU probe image from the lock")
    check("docker_works_directly" in text
          and "if docker_works_directly; then\n    docker run" in text,
          "GPU probe uses the active docker group without sudo")
    check("/tmp/narration-video-gen-%s" in text
          and "gpu_probe_fingerprint" in text
          and "current_boot_id" in text
          and "record_gpu_probe" in text
          and "gpu_probe_verified" in text,
          "GPU probe success is cached per user and boot in temporary state")
    check("[OK] Docker GPU access verified for this boot" in text
          and "Setup complete: ./bin/narration-video-gen plan" in text,
          "a successful GPU probe is not requested again during the same boot")
    check("open_tts_web_ui" in text
          and "tts web start --lan" in text
          and "tts web reset-password" in text
          and "This PC only (no password)" in text,
          "Linux setup chooses local or password-protected LAN TTS access")
    check("stat -c '%u'" in text and "stat -c '%a'" in text
          and '[ "$mode" = "700" ]' in text and 'chmod 0700 "$dir"' in text
          and '[ ! -L "$dir/gpu-probe" ]' in text,
          "temporary GPU probe state validates ownership and rejects symlinks")

    help_result = subprocess.run(
        [str(script), "--help"], capture_output=True, text=True, check=False)
    check(help_result.returncode == 0 and "scripts/setup-linux.sh" in help_result.stdout,
          "setup help works without probing or changing the host")
    conflict_env = os.environ.copy()
    conflict_env["LC_ALL"] = "C"
    conflict_result = subprocess.run(
        [str(script), "--check", "--test-gpu"],
        capture_output=True, text=True, check=False, env=conflict_env)
    check(conflict_result.returncode != 0
          and "--check is always read-only" in conflict_result.stderr,
          "read-only check rejects GPU test before probing the host")
    remove_result = subprocess.run(
        [str(script), "--remove-swap"],
        capture_output=True, text=True, check=False, env=conflict_env)
    check(remove_result.returncode != 0
          and "re-run with --yes" in remove_result.stderr,
          "managed swap removal requires an explicit approved action")
    wsl_remove_env = conflict_env.copy()
    wsl_remove_env["WSL_INTEROP"] = "fixture"
    wsl_remove_result = subprocess.run(
        [str(script), "--remove-swap", "--yes"],
        capture_output=True, text=True, check=False, env=wsl_remove_env)
    check(wsl_remove_result.returncode != 0
          and "--remove-swap is disabled under WSL" in wsl_remove_result.stderr,
          "managed Linux swap removal is refused under WSL before mutation")

    source_prefix = text.split("\nrequire_supported_host\n", 1)[0]
    with tempfile.TemporaryDirectory(prefix="video-gen-lab-swap-test-") as tmp:
        fixture = Path(tmp)
        fixture_fstab = fixture / "fstab"
        fixture_meminfo = fixture / "meminfo"
        fixture_fstab.write_text(
            "UUID=root / ext4 defaults 0 1\n", encoding="utf-8")
        fixture_meminfo.write_text(
            "MemAvailable: 16777216 kB\nSwapTotal: 8388608 kB\n",
            encoding="utf-8")
        fixture_env = os.environ.copy()
        fixture_env.update({
            "LC_ALL": "C",
            "TEST_FSTAB": str(fixture_fstab),
            "TEST_MEMINFO": str(fixture_meminfo),
            "TEST_SWAP": str(fixture / "managed-swap"),
        })
        fixture_script = source_prefix + r'''
fstab_path="$TEST_FSTAB"
meminfo_path="$TEST_MEMINFO"
managed_swapfile="$TEST_SWAP"
sudo_run() {
  if [ "${1:-}" = "findmnt" ]; then return 0; fi
  "$@"
}
write_managed_swap_fstab
fstab_has_managed_swap
printf 'missing=%s\n' "$(swap_missing_gib)"
remove_managed_swap_fstab
if fstab_has_managed_swap; then exit 41; fi
grep -q '^UUID=root / ext4 defaults 0 1$' "$fstab_path"
printf '%s\n' "$swap_fstab_begin" >"$fstab_path"
if (write_managed_swap_fstab); then exit 42; fi
'''
        fixture_result = subprocess.run(
            ["bash"], input=fixture_script, capture_output=True, text=True,
            check=False, env=fixture_env, cwd=ROOT)
        check(fixture_result.returncode == 0
              and "missing=24" in fixture_result.stdout,
              "mock swap flow handles its fstab block, rejects malformed markers, and computes the 24 GiB deficit")

    with tempfile.TemporaryDirectory() as locale_config:
        english_env = os.environ.copy()
        english_env.update({"LC_ALL": "C", "LC_MESSAGES": "ja", "LANG": "ja",
                            "NVG_CONFIG_HOME": locale_config})
        english_env.pop("NVG_UI_LANGUAGE", None)
        english_result = subprocess.run(
            [str(script), "--check"], capture_output=True, text=True,
            check=False, env=english_env)
        if english_result.returncode == 0:
            japanese_env = dict(english_env, NVG_UI_LANGUAGE="ja")
            japanese_result = subprocess.run(
                [str(script), "--check"], capture_output=True, text=True,
                check=False, env=japanese_env)
            check("Linux setup status" in english_result.stdout
                  and "Continue" not in english_result.stdout,
                  "English locale renders the read-only status in English")
            check(japanese_result.returncode == 0
                  and "Linuxセットアップ状況" in japanese_result.stdout,
                  "explicit Japanese app language overrides the Linux locale")
        else:
            print("  skip locale output check; this host is outside setup-linux support")


@expected_ui_language("en")
def test_windows_setup_is_guided_and_safe():
    section("Windows setup")
    launcher = ROOT / "setup.cmd"
    script = ROOT / "scripts" / "setup-windows.ps1"
    cleanup_launcher = ROOT / "cleanup.cmd"
    cleanup_script = ROOT / "scripts" / "cleanup-windows.ps1"
    launcher_text = launcher.read_text(encoding="utf-8")
    script_bytes = script.read_bytes()
    check(script_bytes.startswith(b"\xef\xbb\xbf"),
          "Windows PowerShell wizard has a UTF-8 BOM for PowerShell 5.1")
    text = script.read_text(encoding="utf-8-sig")
    cleanup_launcher_text = cleanup_launcher.read_text(encoding="utf-8")
    cleanup_bytes = cleanup_script.read_bytes()
    cleanup_text = cleanup_script.read_text(encoding="utf-8-sig")

    check(cleanup_bytes.startswith(b"\xef\xbb\xbf"),
          "Windows cleanup has a UTF-8 BOM for PowerShell 5.1")
    check("scripts\\cleanup-windows.ps1" in cleanup_launcher_text
          and '"-Check" set "NVG_PAUSE=0"' in cleanup_launcher_text,
          "cleanup.cmd launches the cleanup wizard and keeps preview non-interactive")
    check("scripts/cleanup-models.py" in cleanup_text
          and 'if ($Models -or $Images -or $CompactWsl)' in cleanup_text
          and "if (-not $Check) { exit 2 }" in cleanup_text,
          "cleanup refuses destructive work while a download or generation is active")
    check("Repair-ManagedModelPermissions" in cleanup_text
          and '$expectedRepo = "/home/$User/$RepositoryDirectory"' in cleanup_text
          and "find $target -xdev" in cleanup_text
          and 'chown -h -- "${User}:${User}"' in cleanup_text
          and "Repair-ManagedModelPermissions $Distribution $repo $user" in cleanup_text,
          "cleanup safely repairs container-owned TTS cache permissions without following symlinks")
    check("$ProjectImageRepositories" in cleanup_text
          and "narration-video-gen-comfy" in cleanup_text
          and "narration-video-gen-musetalk" in cleanup_text
          and "narration-video-gen-tts" in cleanup_text
          and "Get-DockerBuildCacheUsage" in cleanup_text
          and "他プロジェクトと区別できないため保持" in cleanup_text
          and "docker system prune" not in cleanup_text,
          "cleanup removes only explicitly owned Docker images")
    container_remove = cleanup_text.index(
        'Invoke-Docker $Distribution @("container", "rm", "-f", $container.Names)')
    model_remove = cleanup_text.index(
        "Repair-ManagedModelPermissions $Distribution $repo $user")
    check(container_remove < model_remove
          and "$projectContainers = @(Get-ProjectContainers $Distribution" in cleanup_text,
          "cleanup releases exact project container mounts before deleting models")
    check("Get-DistroVhd" in cleanup_text
          and "Get-DockerDataVhds" in cleanup_text
          and "fstrim -av" in cleanup_text
          and "wsl.exe --shutdown" in cleanup_text
          and "Optimize-VHD -Path '$path' -Mode Full" in cleanup_text
          and "diskpart.exe" not in cleanup_text
          and "先にsetup.cmdを動画用途で実行してください" in cleanup_text
          and "$totalReclaimed" in cleanup_text,
          "cleanup trims and compacts only resolved Ubuntu and Docker data VHDX files")
    check("$ManagedWslMemoryGiB = 20" in cleanup_text
          and "$ManagedWslSwapGiB = 32" in cleanup_text
          and "Get-WslResourceResetPlan" in cleanup_text
          and "Reset-ManagedWslResources" in cleanup_text
          and ".cleanup-backup-" in cleanup_text,
          "cleanup restores only the setup defaults and backs up .wslconfig")

    check("$VideoWslSwapGiB = 32" in text and "swap >= 31*1024*1024" in text,
          "Windows setup configures and accepts the 32 GiB swap default")
    check("Ensure-OptimizeVhdSupport" in text
          and "Microsoft-Hyper-V-Management-PowerShell" in text
          and "Enable-WindowsOptionalFeature -Online" in text
          and "-All -NoRestart" in text
          and 'if ($Purpose -eq "Video")' in text
          and "if (-not (Ensure-OptimizeVhdSupport))" in text,
          "video setup prepares Optimize-VHD without forcing an extra restart")
    for profile in Catalog(ROOT).profiles.values():
        if profile.get("platform") == "windows-wsl2":
            expected = 48 if profile["recipe"] in (
                "wan21-infinitetalk-720p", "wan22-s2v-720p") or profile["id"] in (
                "windows-wan21-480p-vram8-simulated",
                "windows-wan22-480p-vram8-simulated") else 32
            check(profile["settings"].get("wsl_swap_gib") == expected
                  and profile["requires"].get("swap_gib_min", 0) <= expected,
                  "Windows profile %s uses its published swap operating value"
                  % profile["id"])

    check("powershell.exe" in launcher_text
          and "scripts\\setup-windows.ps1" in launcher_text,
          "ASCII-named launcher enters the PowerShell wizard")
    check('ValidateSet("Tts", "Video")' in text
          and '[switch]$Check' in text,
          "wizard separates TTS-only setup from video and has a read-only check")
    check("CurrentUICulture.TwoLetterISOLanguageName" in text
          and "Save-WslUiLanguage" in text
          and "narration-video-gen/ui-language" in text
          and "if (-not $Check" in text,
          "wizard saves the Windows UI language without changing the WSL locale")
    check("Get-SavedPurpose" in text
          and "Save-SetupPurpose" in text
          and '"setup-purpose.txt"' in text
          and "$savedPurpose = Get-SavedPurpose" in text
          and "$Purpose = Get-SavedPurpose" not in text
          and "elseif (-not $Check)" in text
          and text.index("Save-SetupPurpose $Purpose")
          < text.index('Write-Section "Windows"'),
          "the first purpose choice is saved before resumable setup work")
    check("Get-CimInstance Win32_ComputerSystem" in text
          and "Get-PSDrive -Name C" in text,
          "Windows physical RAM and C drive space are shown before preparation")
    check("Start-Process wsl.exe -Verb RunAs" in text
          and "Confirm-Action" in text
          and 'wsl --install failed' in text,
          "WSL installation is explicit, elevated, and checked")
    check('"--no-launch"' in text
          and "Test-UbuntuInitialized" in text
          and "exit と入力してEnterを押す" in text,
          "Ubuntu installation explains and verifies the separate first-run flow")
    check("function Get-WslOsRelease" in text
          and "-- cat /etc/os-release" in text
          and '$release.Id -eq "ubuntu"' in text
          and '$release.VersionId -in @("24.04", "26.04")' in text
          and "VERSION_CODENAME" not in text,
          "Ubuntu release validation avoids fragile shell argument parsing")
    check('$HOME/{1}' in text and "$RepositoryDirectory = \"narration-video-gen\"" in text
          and "git clone" in text,
          "the runtime checkout is placed in the WSL filesystem")
    check("Docker Desktopがありません" in text
          and "$DockerDesktopPackageId = \"Docker.DockerDesktop\"" in text
          and '"--exact", "--source", "winget"' in text
          and "--accept-package-agreements" in text
          and "--accept-source-agreements" in text
          and "Start-Process $winget.Source -NoNewWindow -Wait -PassThru" in text
          and '"--disable-interactivity"' in text
          and "--disable-interactivity | Out-Host" not in text
          and "Confirm-Action" in text
          and "--allow-reboot" not in text,
          "Docker Desktop keeps WinGet progress attached without polluting function output")
    check("Settings > Resources > WSL Integration" in text
          and "Get-DockerDesktopExecutable" in text
          and "Docker DesktopのDashboardが開いたら" in text
          and "Enable-DockerWslIntegration" in text
          and "Wait-DockerInWsl" in text,
          "Docker Desktop launch and WSL integration have explicit setup handoffs")
    check("Stop-DockerDesktopForWslShutdown" in text
          and 'Start-Process $control -ArgumentList "-Shutdown"' in text
          and "$script:RestartDockerAfterWslShutdown = $true" in text
          and "WaitForExit(30000)" in text
          and "Wait-DockerDesktopEngine 30" in text,
          "WSL resource changes safely restart Docker and bound engine probes")
    check("NVIDIAドライバーを確認できません" in text
          and "nvidia.com/Download" in text
          and "pnputil" not in text,
          "the Windows GPU driver is never changed automatically")
    check("Ensure-VideoWslResources" in text
          and "Set-Wsl2Resources" in text
          and "Copy-Item -LiteralPath $WslConfigPath" in text
          and r"/MemTotal/{ram=\$2}" in text
          and 'Confirm-Action ".wslconfigを動画生成用に更新しますか？"' in text
          and 'Confirm-Action "wsl --shutdownを実行しますか？"' in text,
          "video setup can back up and prepare global WSL resources with separate confirmations")
    check("Get-PendingPlanWslSetup" in text
          and "Apply-PendingPlanWslSetup" in text
          and "720p用WSL設定" in text
          and '.wslconfigのswapを$($Request.SwapGiB)GBへ増やしますか？' in text
          and "plan --profile {1}" in text,
          "the Windows menu offers and applies the selected 720p swap upgrade")
    check("Update-RepositoryAndWindowsLauncher" in text
          and "Install-WindowsLaunchFilesFromRepository" in text
          and "リポジトリとWindows起動ファイルを更新" in text
          and "status --porcelain" in text
          and "fetch --prune origin" in text
          and "merge --ff-only origin/main" in text
          and "originが公式URLと一致しない" in text,
          "the Windows menu safely updates both the WSL checkout and launcher files")
    check("scripts/setup-linux.sh --check" in text
          and "scripts/setup-linux.sh --test-gpu --yes" in text,
          "the wizard reuses shared diagnosis and the pinned GPU probe")
    check("docker info --format '{{json .Runtimes}}'" in text,
          "PowerShell 5.1 preserves the Docker runtime format argument")
    check("Show-VideoMenu" in text
          and "Start-TtsWebUi" in text
          and "Reset-TtsWebUiPassword" in text
          and "キャラクターと音声を作る (Web UI)" in text
          and "./bin/narration-video-gen tts" in text
          and "tts web start --lan" in text
          and "動画の構成選択と準備 (plan)" in text
          and "動画を生成 (run)" in text
          and "生成状況を確認 (status)" in text
          and "実行中の生成を中止 (cancel)" in text
          and "容量を解放する (cleanup)" in text
          and "Start-Process -FilePath $cleanupLauncher" in text
          and "Show-VideoMenu $Distribution $progressColumns" in text,
          "Windows beginners can create a character, narration, and video from setup.cmd")
    check("Install-DesktopShortcut" in text
          and "Ensure-DesktopShortcut" in text
          and '"Narration Video Gen.lnk"' in text
          and '"Narration Video Gen Cleanup.lnk"' not in text
          and '$InstalledLauncherPath = Join-Path $SetupStateDirectory "setup.cmd"' in text
          and '"scripts\\setup-windows.ps1"' in text
          and "[Environment]::GetFolderPath(\"Desktop\")" in text
          and "$shortcut.TargetPath = $InstalledLauncherPath" in text
          and "$shortcut.WorkingDirectory = $SetupStateDirectory" in text
          and "$InstalledCleanupLauncherPath" in text
          and "$InstalledCleanupScriptPath" in text
          and "Confirm-ActionDefaultYes" in text,
          "successful Windows setup offers a relocation-safe desktop shortcut")
    install_ubuntu = text[text.index("function Install-Ubuntu"):text.index("function Get-DockerDesktopExecutable")]
    check("Ensure-DesktopShortcut" in install_ubuntu
          and install_ubuntu.index("Ensure-DesktopShortcut") < install_ubuntu.index("Start-Process wsl.exe -Verb RunAs")
          and "$resumeShortcutReady = Test-Path -LiteralPath $DesktopShortcutPath" in install_ubuntu
          and "デスクトップの「Narration Video Gen」を開くと続きから進みます" in install_ubuntu,
          "Windows setup creates the restart shortcut before WSL installation")

    from narration_video_gen import cli as cli_mod
    original_platform = cli_mod.detect_mod.detect_platform
    original_run = cli_mod.subprocess.run
    original_open = cli_mod.webbrowser.open
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env", {})
        return SimpleNamespace(returncode=0)

    try:
        cli_mod.detect_mod.detect_platform = lambda: "windows-wsl2"
        cli_mod.subprocess.run = fake_run
        cli_mod.webbrowser.open = lambda _url: captured.setdefault("fallback", True)
        opened = cli_mod._open_browser("http://127.0.0.1:7861")
    finally:
        cli_mod.detect_mod.detect_platform = original_platform
        cli_mod.subprocess.run = original_run
        cli_mod.webbrowser.open = original_open
    check(opened and captured["command"][0] == "powershell.exe"
          and captured["env"].get("NVG_BROWSER_URL") == "http://127.0.0.1:7861"
          and "NVG_BROWSER_URL" in captured["env"].get("WSLENV", "").split(":")
          and "fallback" not in captured,
          "TTS exports its URL and opens the Windows browser without shell interpolation")

    from contextlib import redirect_stderr
    warning = StringIO()
    try:
        cli_mod.detect_mod.detect_platform = lambda: "windows-wsl2"
        with redirect_stderr(warning):
            cli_mod._warn_windows_mounted_checkout(SimpleNamespace(
                json=False, root=Path("/mnt/c/narration-video-gen")))
    finally:
        cli_mod.detect_mod.detect_platform = original_platform
    check("Windows-mounted drive" in warning.getvalue(),
          "a CLI launched from /mnt/c warns before model-heavy work")


def test_passwordless_sudo_helper_is_guarded():
    section("passwordless sudo helper")
    script = ROOT / "scripts" / "passwordless-sudo.sh"
    text = script.read_text(encoding="utf-8")
    check(bool(script.stat().st_mode & 0o111), "sudo helper is executable")
    check("set -euo pipefail" in text, "sudo helper uses strict shell mode")
    check("  status|disable)" in text and "  enable)" in text,
          "sudo helper exposes explicit actions")
    check("SUDO_USER" in text and "SUDO_UID" in text,
          "sudo helper identifies the invoking non-root account")
    check("unsupported user name" in text and "getent passwd" in text,
          "sudo helper validates the target account")
    check("is not in the sudo group" in text,
          "sudo helper does not grant privileges to a new account")
    check("NOPASSWD: ALL" in text, "sudo helper makes the requested grant explicit")
    check("/etc/sudoers.d/.narration-video-gen-passwordless.XXXXXX" in text
          and 'root_run ln "$system_tmp" "$dropin"' in text,
          "sudo helper installs with an ignored temporary file and no-clobber link")
    check("root:root:440" in text and "render_legacy_dropin" in text
          and 'render_dropin "$expires" "$variant"' in text and "test -L" in text,
          "sudo helper recognizes only the exact managed file")
    check("max_seconds=$((12 * 3600))" in text and "invalid duration" in text,
          "sudo helper bounds the length of a grant")
    check("[ -t 0 ] && [ -t 2 ]" in text and "needs systemd" in text,
          "sudo helper is enabled by a person and only with a working expiry")
    check("OnCalendar=" in text and "WantedBy=sysinit.target" in text
          and "ExecStart=/usr/bin/rm -f -- $dropin" in text,
          "sudo helper removes the grant at the deadline and at the next boot")
    check(text.count("'DefaultDependencies=no'") == 2,
          "sudo helper keeps the boot-time removal free of an ordering cycle")
    check("NOTAFTER=" in text and "'^Sudo version '" in text,
          "sudo helper lets the original sudo enforce the deadline itself")
    check("systemctl restart" in text and "is-active --quiet" in text,
          "sudo helper schedules the removal before granting anything")
    check(text.count("/usr/sbin/visudo -cf") >= 2,
          "sudo helper validates the drop-in and complete sudoers policy")
    check('root_run test "$dropin" -ef "$system_tmp"' in text
          and 'root_run rm -f -- "$dropin"' in text
          and "created_dropin=false" in text,
          "sudo helper rolls back an incomplete installation")
    check("trap cleanup EXIT" in text and "trap 'exit 130' INT" in text
          and "trap 'exit 143' TERM" in text,
          "sudo helper exits after cleaning up on signals")
    check("trap '' HUP INT TERM" in text and "trap - HUP INT TERM" in text,
          "sudo helper has an uninterruptible commit boundary")
    check('/usr/bin/sudo -u "$target_user" /usr/bin/sudo -k' in text
          and "sudo -k" in text,
          "sudo helper invalidates the target user's credential cache")
    completed = subprocess.run(
        [str(script), "status"], capture_output=True, text=True, check=False,
    )
    check(completed.returncode == 0, "sudo helper status is read-only and usable")
    manuals = (ROOT / "docs" / "manual" / "linux.md").read_text(encoding="utf-8")
    manuals += (ROOT / "docs" / "manual" / "linux_en.md").read_text(encoding="utf-8")
    check("passwordless-sudo.sh enable --for" in manuals
          and "passwordless-sudo.sh disable" in manuals,
          "Linux manuals document enabling and disabling the helper")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    check("passwordless-sudo.sh enable --for" in agents
          and "Do not run `enable` yourself" in agents,
          "AGENTS.md leaves enabling passwordless sudo to the user")


def test_stage_workflows_are_well_formed(catalog):
    """Every stage must build, and every node reference must resolve."""
    section("stage workflows")
    from narration_video_gen import runner as runner_mod
    from narration_video_gen import stages as stages_mod

    invalid_profile = dict(next(iter(catalog.profiles.values())))
    invalid_profile["settings"] = {
        **(invalid_profile.get("settings") or {}), "vae_native_upsample": "false"}
    try:
        catalog._validate_profile(invalid_profile, "native-vae-test")
        check(False, "native VAE setting rejects truthy string values")
    except CatalogError:
        check(True, "native VAE setting rejects truthy string values")

    exact_video, audio_video = runner_mod.video_and_audio_outputs([
        "run/01-generate_00001-audio.mp4",
        "run/01-generate_00001.mp4",
    ])
    check(exact_video.endswith("01-generate_00001.mp4"),
          "post-stages keep the exact requested frame count")
    check(audio_video.endswith("01-generate_00001-audio.mp4"),
          "post-stages retain the muxed narration as their audio source")
    derived_video, reported_audio = runner_mod.video_and_audio_outputs([
        "run/01-generate_00001-audio.mp4",
    ])
    check(derived_video == "run/01-generate_00001.mp4"
          and reported_audio.endswith("01-generate_00001-audio.mp4"),
          "a mux-only history entry resolves its saved exact-frame sibling")
    try:
        runner_mod.video_and_audio_outputs([
            "run/01-generate_00001.mp4",
            "run/other_00001-audio.mp4",
        ])
        check(False, "an unrelated audio-bearing output is rejected")
    except runner_mod.RunnerError:
        check(True, "an unrelated audio-bearing output is rejected")

    exact_probe = {
        "path": "/output/raw.mp4", "bytes": 1000,
        "video_frames": 89, "video_packets": 89,
        "video_fps": 16.0, "video_duration_seconds": 5.5625,
        "audio_frames": None, "audio_packets": None,
        "audio_duration_seconds": None, "format_duration_seconds": 5.5625,
    }
    short_probe = dict(exact_probe, path="/output/raw-audio.mp4",
                       bytes=900, video_frames=86, video_packets=86,
                       video_duration_seconds=5.375)
    repaired_probe = dict(exact_probe, path="/output/raw-audio.mp4", bytes=1200,
                          audio_frames=261, audio_packets=262,
                          audio_duration_seconds=5.562)
    originals = (runner_mod._flush_runtime_file, runner_mod.probe_container_media,
                 runner_mod._run_runtime_command, runner_mod.time.sleep)
    commands = []
    probes = iter([exact_probe, exact_probe, short_probe,
                   repaired_probe, repaired_probe])
    try:
        runner_mod._flush_runtime_file = lambda *_args: {"returncode": 0}
        runner_mod.probe_container_media = lambda *_args: next(probes)
        runner_mod._run_runtime_command = lambda _container, command: (
            commands.append(command) or
            {"command": command, "returncode": 0, "stdout": "", "stderr": ""})
        runner_mod.time.sleep = lambda _seconds: None
        contract = runner_mod.ensure_audio_companion(
            "self", "/output/raw.mp4", "/output/raw-audio.mp4", "/input/audio.wav",
            expected_frames=89)
    finally:
        (runner_mod._flush_runtime_file, runner_mod.probe_container_media,
         runner_mod._run_runtime_command, runner_mod.time.sleep) = originals
    check(contract["status"] == "repaired"
          and contract["audio_companion_before"]["video_frames"] == 86
          and contract["audio_companion_after"]["video_frames"] == 89,
          "a short VHS audio companion is detected and repaired from stable raw")
    check(any(command and command[0] == "ffmpeg" for command in commands)
          and any(command and command[0] == "mv" for command in commands),
          "media repair uses a temporary mux followed by atomic replacement")

    overlong_probe = dict(exact_probe, video_frames=160, video_packets=160,
                          video_duration_seconds=10.0)
    commands = []
    probes = iter([overlong_probe, overlong_probe, short_probe])
    try:
        runner_mod._flush_runtime_file = lambda *_args: {"returncode": 0}
        runner_mod.probe_container_media = lambda *_args: next(probes)
        runner_mod._run_runtime_command = lambda _container, command: (
            commands.append(command) or
            {"command": command, "returncode": 0, "stdout": "", "stderr": ""})
        runner_mod.time.sleep = lambda _seconds: None
        try:
            runner_mod.ensure_audio_companion(
                "self", "/output/raw.mp4", "/output/raw-audio.mp4",
                "/input/audio.wav", expected_frames=89)
            check(False, "an overlong FramePack raw file is rejected before mux repair")
        except runner_mod.RunnerError as exc:
            contract = exc.context.get("media_contract") or {}
            check(exc.code == "media-contract-failed"
                  and contract.get("status") == "unexpected-raw-frame-count"
                  and contract.get("audio_companion_before", {}).get(
                      "video_frames") == 86
                  and not commands,
                  "an overlong FramePack raw file is rejected before mux repair")
    finally:
        (runner_mod._flush_runtime_file, runner_mod.probe_container_media,
         runner_mod._run_runtime_command, runner_mod.time.sleep) = originals

    check(runner_mod.face_detail_frame_count(455) == 453,
          "a mux-shortened source is rounded down to a VACE-safe count")
    check(runner_mod.face_detail_frame_count(489, 457) == 457,
          "an explicit safe cap can consume a longer raw generation")
    for available, requested in ((86, 89), (455, 455)):
        try:
            runner_mod.face_detail_frame_count(available, requested)
            check(False, "invalid face detail frame contract is rejected")
        except runner_mod.RunnerError:
            check(True, "invalid face detail frame contract is rejected")

    from narration_video_gen import cli as cli_mod
    check(cli_mod._expected_media_frames("s2v", 89) == 89
          and cli_mod._expected_media_frames("face-detailer", 89) == 89
          and cli_mod._expected_media_frames("rife", 89) is None,
          "strict frame contracts exclude the interpolating RIFE stage")

    for recipe_id in ("wan21-infinitetalk-480p", "wan21-infinitetalk-720p",
                      "wan22-s2v-480p", "wan22-s2v-720p",
                      "wan21-infinitetalk-480p-q4ks", "wan21-infinitetalk-720p-q4ks",
                      "wan22-s2v-480p-q4ks", "wan22-s2v-720p-q4ks"):
        recipe = catalog.recipes[recipe_id]
        profile = next((p for p in catalog.sorted_profiles()
                        if p["recipe"] == recipe_id), None)
        if profile is None:
            # A recipe can exist before a hardware-specific profile is qualified.
            profile = next(p for p in catalog.sorted_profiles()
                           if p["recipe"] == recipe_id.rsplit("-q4ks", 1)[0])
        pipeline = runner_mod.pipeline_for(recipe)
        models = runner_mod.resolve_models(ROOT, recipe, pipeline)
        if recipe_id.endswith("-q4ks"):
            role = "base" if recipe["model_family"] == "wan21-infinitetalk" else "s2v"
            check(models[role].endswith("Q4_K_S.gguf"),
                  "%s: graph resolves the Q4 model role" % recipe_id)
        source = "/opt/ComfyUI/output/previous.mp4"
        for index, stage_id in enumerate(pipeline, start=1):
            workflow, output_node = runner_mod.build_stage(
                stage_id, recipe=recipe, profile=profile, models=models,
                image_name="portrait.png", audio_name="narration.wav",
                frames=429, out_prefix="test/%02d-%s" % (index, stage_id),
                source_video=source,
                audio_source="/opt/ComfyUI/output/pre-rife-audio.mp4",
            )
            if stage_id in runner_mod.COMMAND_STAGES:
                # Not a graph: an ffmpeg argv, which must carry the audio
                # duration placeholder the runner fills in.
                check(workflow[0] == "ffmpeg", "%s/%s: builds an ffmpeg command"
                      % (recipe_id, stage_id))
                check("{audio_duration}" in workflow,
                      "%s/%s: audio duration is measured, not hard-coded"
                      % (recipe_id, stage_id))
                check("{audio_codec_args}" in workflow,
                      "%s/%s: MP4 audio handling follows the probed codec"
                      % (recipe_id, stage_id))
                continue
            ids = set(workflow)
            vae_nodes = [node for node in workflow.values()
                         if node.get("class_type") == "WanVideoVAELoader"]
            if vae_nodes:
                check(all("native_upsample" not in node["inputs"] for node in vae_nodes),
                      "%s/%s: default VAE loader cache inputs are unchanged"
                      % (recipe_id, stage_id))
                native_profile = dict(profile, settings={
                    **(profile.get("settings") or {}), "vae_native_upsample": True})
                native_workflow, native_output = runner_mod.build_stage(
                    stage_id, recipe=recipe, profile=native_profile, models=models,
                    image_name="portrait.png", audio_name="narration.wav",
                    frames=429, out_prefix="test/%02d-%s" % (index, stage_id),
                    source_video=source,
                    audio_source="/opt/ComfyUI/output/pre-rife-audio.mp4")
                native_loaders = [node for node in native_workflow.values()
                                  if node.get("class_type") == "WanVideoVAELoader"]
                enabled = all(node["inputs"].pop("native_upsample", None) is True
                              for node in native_loaders)
                check(enabled and native_workflow == workflow
                      and native_output == output_node,
                      "%s/%s: native expansion changes only the VAE loader input"
                      % (recipe_id, stage_id))
            dangling = []
            for node_id, node in workflow.items():
                for key, value in node["inputs"].items():
                    # A node reference is ["<node id>", <output index>].
                    if (isinstance(value, list) and len(value) == 2
                            and isinstance(value[0], str) and value[0] not in ids):
                        dangling.append("%s.%s -> %s" % (node_id, key, value[0]))
            check(not dangling, "%s/%s: %d nodes, references resolve%s"
                  % (recipe_id, stage_id, len(workflow),
                     "" if not dangling else " (%s)" % ", ".join(dangling)))
            check(output_node in ids,
                  "%s/%s: output node %s exists" % (recipe_id, stage_id, output_node))
            if stage_id == "infinitetalk":
                offload_profile = dict(profile, settings={
                    **(profile.get("settings") or {}),
                    "offload_transformer_before_vae_decode": True})
                offload_workflow, offload_output = runner_mod.build_stage(
                    stage_id, recipe=recipe, profile=offload_profile, models=models,
                    image_name="portrait.png", audio_name="narration.wav",
                    frames=429, out_prefix="test/%02d-%s" % (index, stage_id),
                    source_video=source,
                    audio_source="/opt/ComfyUI/output/pre-rife-audio.mp4")
                enabled = offload_workflow["14"]["inputs"]["force_offload"] is True
                offload_workflow["14"]["inputs"]["force_offload"] = False
                check(enabled and offload_workflow == workflow
                      and offload_output == output_node,
                      "%s: decode offload changes only the MultiTalk loop control"
                      % recipe_id)
                positive = workflow["13"]["inputs"]["positive_prompt"].lower()
                forbidden = ("young japanese", "long dark hair", "olive green",
                             "modern living room")
                check("same subject" in positive
                      and not any(term in positive for term in forbidden),
                      "%s/%s: prompt follows arbitrary reference images"
                      % (recipe_id, stage_id))
                check(workflow["18"]["inputs"] == {
                          "image": ["16", 0], "batch_index": 0, "length": 429}
                      and workflow["17"]["inputs"]["images"] == ["18", 0],
                      "%s/%s: raw output is capped to the planned exact frame count"
                      % (recipe_id, stage_id))
            if stage_id == "s2v":
                embeds = workflow["12"]["inputs"]
                sampler = workflow["14"]["inputs"]
                positive = workflow["10"]["inputs"]["positive_prompt"]
                frame_cap = workflow.get("19", {}).get("inputs", {})
                expects_decode_offload = recipe["resolution"][1] >= 720
                check(embeds.get("start_from_ref") is True
                      and embeds.get("enable_framepack") is True
                      and embeds.get("ref_image") == ["5", 0]
                      and embeds.get("ref_hold_frames") == 3
                      and embeds.get("offload_transformer_before_vae_decode")
                      is expects_decode_offload
                      and "context_options" not in sampler,
                      "%s/%s: starts from the reference image with profile-scoped decode offload"
                      % (recipe_id, stage_id))
                check(frame_cap == {
                          "image": ["15", 0], "batch_index": 0, "length": 429}
                      and workflow["16"]["inputs"]["images"] == ["19", 0],
                      "%s/%s: FramePack output is capped before raw and audio saves"
                      % (recipe_id, stage_id))
                forbidden = ("young japanese", "long dark hair", "olive green",
                             "modern living room")
                check("same subject" in positive.lower()
                      and not any(term in positive.lower() for term in forbidden),
                      "%s/%s: prompt follows arbitrary reference images"
                      % (recipe_id, stage_id))
            if stage_id == "face-detailer":
                detector = workflow.get("3", {})
                tracker_inputs = workflow.get("5", {}).get("inputs", {})
                detail_prompt = workflow["22"]["inputs"]["positive_prompt"].lower()
                check(detector.get("class_type") == "NVGDetectPrimaryFace"
                      and tracker_inputs.get("bboxes") == ["3", 0]
                      and "coordinates_positive" not in tracker_inputs,
                      "%s/%s: detects the primary face instead of using fixed points"
                      % (recipe_id, stage_id))
                check(workflow["2"]["inputs"]["model"] ==
                      "sam2.1_hiera_small.safetensors",
                      "%s/%s: SAM2 receives the base name that selects the locked fp16 file"
                      % (recipe_id, stage_id))
                check("same subject" in detail_prompt
                      and "woman" not in detail_prompt
                      and "japanese" not in detail_prompt,
                      "%s/%s: face detail follows the detected subject"
                      % (recipe_id, stage_id))
                settings = profile.get("settings") or {}
                expected_size = settings.get("face_detailer_size", 320)
                expected_blocks = settings.get(
                    "face_detailer_blocks_to_swap",
                    settings.get("blocks_to_swap", 40))
                check(workflow["10"]["inputs"]["width"] == expected_size
                      and workflow["10"]["inputs"]["height"] == expected_size
                      and workflow["16"]["inputs"]["width"] == expected_size
                      and workflow["16"]["inputs"]["height"] == expected_size,
                      "%s/%s: uses the profile-scoped detail activation size"
                      % (recipe_id, stage_id))
                check(workflow["18"]["inputs"]["blocks_to_swap"] == expected_blocks,
                      "%s/%s: uses the profile-scoped detail block-swap budget"
                      % (recipe_id, stage_id))
                check(workflow["27"]["class_type"] == "LoadAudio"
                      and workflow["27"]["inputs"]["audio"]
                      == "pre-rife-audio.mp4 [output]"
                      and workflow["26"]["inputs"]["audio"] == ["27", 0],
                      "%s/%s: loads audio separately from the exact-frame video"
                      % (recipe_id, stage_id))
            if stage_id == "rife":
                check(workflow["4"]["class_type"] == "LoadAudio"
                      and workflow["4"]["inputs"]["audio"]
                      == "pre-rife-audio.mp4 [output]"
                      and workflow["3"]["inputs"]["audio"] == ["4", 0],
                      "%s/%s: keeps exact-frame video and audio on separate inputs"
                      % (recipe_id, stage_id))

    for profile_id in ("linux-wan21-480p-vram16",
                       "windows-wan21-480p-vram16",
                       "linux-wan22-480p-vram16"):
        settings = catalog.profiles[profile_id]["settings"]
        check(settings.get("face_detailer_size") == 320
              and settings.get("face_detailer_blocks_to_swap") == 20,
              "%s: preserves the locally verified full face-detail headroom"
              % profile_id)
    windows_wan21 = catalog.profiles["windows-wan21-480p-vram16"]
    check(windows_wan21["evidence"]["pipeline_stages"]
          == ["infinitetalk", "face-detailer", "rife", "retime"],
          "windows-wan21-480p-vram16: publishes the verified full pipeline")
    wan22_720_settings = catalog.profiles[
        "linux-wan22-720p-vram16"]["settings"]
    check(wan22_720_settings.get("face_detailer_size") == 384
          and wan22_720_settings.get("face_detailer_blocks_to_swap") == 30,
          "linux-wan22-720p-vram16: preserves the verified full post-stage settings")
    wan21_720_settings = catalog.profiles[
        "linux-wan21-720p-vram16"]["settings"]
    check(wan21_720_settings.get("face_detailer_size") == 384
          and wan21_720_settings.get("face_detailer_blocks_to_swap") == 30,
          "linux-wan21-720p-vram16: preserves the verified full post-stage settings")

    # Stage ordering rules.
    wan21_profile = catalog.profiles["linux-wan21-480p-vram16"]
    enhanced = catalog.recipe_for(wan21_profile, ["musetalk"])
    enhanced_pipeline = runner_mod.pipeline_for(enhanced)
    runner_mod.resolve_models(ROOT, enhanced, enhanced_pipeline)
    muse_config, muse_output = runner_mod.build_stage(
        "musetalk", recipe=enhanced, profile=wan21_profile, models={},
        source_video="/opt/ComfyUI/output/wan21.mp4",
        out_prefix="test/musetalk")
    check(muse_config.get("bbox_shift") == 0 and muse_output is None,
          "MuseTalk stage uses the recipe-owned bbox shift")
    check(runner_mod.musetalk_container_name("run/02-musetalk.mp4").startswith(
              "nvg-musetalk-")
          and runner_mod.musetalk_container_name("run/02-musetalk.mp4")
              == runner_mod.musetalk_container_name("run/02-musetalk.mp4"),
          "MuseTalk sidecars have deterministic cancellation-safe names")
    muse_runner = (ROOT / "src" / "narration_video_gen" / "runner.py").read_text(
        encoding="utf-8")
    check('"--user", "%d:%d" % (os.getuid(), os.getgid())' in muse_runner,
          "MuseTalk output is owned by the invoking user")
    check(stages_mod.order_stages(["rife", "face-detailer"]) == ["face-detailer", "rife"],
          "post-stages are reordered into execution order")
    check(stages_mod.order_stages(["rife", "musetalk", "face-detailer"])
          == ["musetalk", "face-detailer", "rife"],
          "MuseTalk is always followed by face detail and interpolation")
    check(stages_mod.needs_source_video(["rife"]),
          "a rife-only pipeline needs a source video")
    check(not stages_mod.needs_source_video(["s2v", "rife"]),
          "a pipeline with a generating stage does not")
    for bad, why in (
        (["s2v", "infinitetalk"], "two generating stages"),
        (["nope"], "an unknown stage"),
    ):
        try:
            stages_mod.order_stages(bad)
            check(False, "%s is rejected" % why)
        except stages_mod.StageError:
            check(True, "%s is rejected" % why)


def test_rife_only_on_a_wan21_recipe(catalog):
    """The post-stages must work against a Wan2.1 recipe, not just Wan2.2."""
    section("post-stages are model-independent")
    from narration_video_gen import runner as runner_mod
    from narration_video_gen import stages as stages_mod

    recipe = dict(catalog.recipes["wan21-infinitetalk-480p"])
    recipe["postprocess"] = {"rife": {"model": "rife49", "multiplier": 4}}
    profile = catalog.profiles["linux-wan21-480p-vram16"]
    workflow, output_node = runner_mod.build_stage(
        "rife", recipe=recipe, profile=profile, models={},
        source_video="/opt/ComfyUI/output/wan21.mp4",
        audio_source="/opt/ComfyUI/output/wan21-audio.mp4",
        out_prefix="test/rife")
    check(workflow["2"]["inputs"]["multiplier"] == 4,
          "16 fps -> 64 fps is a 4x interpolation")
    check(output_node in workflow, "rife output node exists")

    chunks = runner_mod.rife_chunk_ranges(553, 64, 4)
    check(chunks[0] == {
              "start_frame": 0, "input_frames": 64,
              "generated_frames": 253, "drop_first_output_frame": False,
              "kept_frames": 253}
          and chunks[1]["start_frame"] == 63
          and chunks[-1]["start_frame"] == 504
          and chunks[-1]["input_frames"] == 49
          and sum(item["kept_frames"] for item in chunks) == 2209,
          "chunked RIFE overlaps one source frame and preserves 553 -> 2209")
    accounting_holds = True
    for total in range(2, 140):
        for chunk_size in (2, 7, 64, 129):
            for factor in (2, 4):
                plan = runner_mod.rife_chunk_ranges(total, chunk_size, factor)
                accounting_holds = accounting_holds and (
                    sum(item["kept_frames"] for item in plan)
                    == (total - 1) * factor + 1)
    check(accounting_holds,
          "RIFE chunk frame accounting holds across lengths, sizes, and factors")

    chunk_workflow, chunk_output = stages_mod.build_rife({
        "model": "rife49", "multiplier": 4, "include_audio": False,
        "frame_load_cap": 64, "skip_first_frames": 63,
        "drop_first_output_frame": True, "output_frame_count": 253,
    }, "/opt/ComfyUI/output/source.mp4", 16, "test/rife-chunk")
    check(chunk_workflow["1"]["inputs"]["frame_load_cap"] == 64
          and chunk_workflow["1"]["inputs"]["skip_first_frames"] == 63
          and chunk_workflow["5"]["inputs"] == {
              "image": ["2", 0], "batch_index": 1, "length": 252}
          and chunk_workflow["3"]["inputs"]["images"] == ["5", 0]
          and "audio" not in chunk_workflow["3"]["inputs"]
          and "4" not in chunk_workflow
          and chunk_output == "3",
          "chunked RIFE caps input, removes one overlap frame, and omits audio")

    class FakeChunkClient:
        def __init__(self):
            self.outputs = {}
            self.frees = []

        def submit(self, workflow):
            prompt_id = "chunk-%d" % (len(self.outputs) + 1)
            prefix = workflow["3"]["inputs"]["filename_prefix"]
            self.outputs[prompt_id] = prefix + "_00001.mp4"
            return prompt_id

        def wait(self, prompt_id):
            return {"outputs": {"3": {"gifs": [
                {"filename": Path(self.outputs[prompt_id]).name,
                 "subfolder": str(Path(self.outputs[prompt_id]).parent)}
            ]}}}

        def call(self, path, payload):
            self.frees.append((path, payload))

    fake_client = FakeChunkClient()
    runtime_commands = []

    def fake_probe(_container, path):
        frames = (5 if path.endswith("0001_00001.mp4") else
                  4 if path.endswith("0002_00001.mp4") else 9)
        return {
            "video_frames": frames, "video_packets": frames,
            "video_fps": 32.0, "video_duration_seconds": frames / 32.0,
        }

    def fake_runtime(_container, command):
        runtime_commands.append(command)
        return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}

    with patch.object(runner_mod, "probe_container_media", fake_probe), \
            patch.object(runner_mod, "_run_runtime_command", fake_runtime), \
            patch.object(runner_mod, "run_command_stage",
                         lambda _argv, _container, _audio, output: output):
        chunked = runner_mod.run_chunked_rife_stage(
            fake_client,
            config={"model": "rife49", "multiplier": 2, "chunk_frames": 3},
            source_video="/opt/ComfyUI/output/source.mp4", source_fps=16,
            source_frames=5, out_prefix="test/01-rife",
            audio_source="/opt/ComfyUI/output/source-audio.mp4",
            container="test-container")
    check(len(fake_client.outputs) == 2 and len(fake_client.frees) == 2
          and all(path == "/free" and payload == {
              "unload_models": True, "free_memory": True}
                  for path, payload in fake_client.frees)
          and chunked["expected_frames"] == 9,
          "chunked RIFE runs prompts sequentially and frees each chunk")
    check(any(command[:2] == ["ffmpeg", "-hide_banner"]
              and "concat" in command and "copy" in command
              for command in runtime_commands),
          "chunked RIFE joins the validated chunks by stream copy")
    manifest_command = next(command for command in runtime_commands
                            if command[:2] == ["python3", "-c"])
    check("duration 0.156250000" in manifest_command[-1]
          and "duration 0.125000000" in manifest_command[-1],
          "chunk concat uses exact frame-derived durations instead of MP4 rounding")

    # A factor RIFE cannot do has to be refused rather than silently rounded.
    recipe["postprocess"]["rife"]["multiplier"] = 3
    try:
        runner_mod.build_stage("rife", recipe=recipe, profile=profile, models={},
                               source_video="x.mp4", out_prefix="t")
        check(False, "a 3x multiplier is refused")
    except (stages_mod.StageError, runner_mod.RunnerError):
        check(True, "a 3x multiplier is refused")


def test_cli_json_is_parseable():
    section("CLI JSON output")
    for args in (["list"], ["matrix"]):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "narration-video-gen"), "--root", str(ROOT),
             "--json", *args],
            capture_output=True, text=True, check=False,
        )
        if args == ["matrix"]:
            check(completed.returncode == 0, "matrix runs")
            continue
        check(completed.returncode == 0, "%s exits 0" % " ".join(args))
        try:
            json.loads(completed.stdout)
            check(True, "%s emits valid JSON" % " ".join(args))
        except ValueError as exc:
            check(False, "%s emits valid JSON: %s" % (" ".join(args), exc))

    state = selection_state.state_path(ROOT)
    before = state.read_bytes() if state.is_file() and not state.is_symlink() else None
    environment = {
        "platform": "linux",
        "kernel": "test",
        "gpus": [{"name": "A4000", "vram_gib": 15.99, "vram_mib": 16376,
                  "driver_version": "999"}],
        "memory": {"ram_gib": 19.46, "swap_gib": 32},
        "container": {"docker_available": True, "nvidia_runtime": True},
        "disk": {"models": {"free_gib": 500.0},
                 "outputs": {"free_gib": 500.0}},
        "warnings": [],
    }
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "environment.json"
        env_file.write_text(json.dumps(environment), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "narration-video-gen"),
             "--root", str(ROOT), "--json", "select", "--model", "wan21",
             "--resolution", "480p", "--env-file", str(env_file)],
            capture_output=True, text=True, check=False,
        )
    try:
        selected = json.loads(completed.stdout)
    except ValueError:
        selected = None
    after = state.read_bytes() if state.is_file() and not state.is_symlink() else None
    check(completed.returncode == 0 and selected
          and selected["best"]["id"] == "linux-wan21-480p-vram16",
          "JSON select accepts human model aliases without prompting")
    check(before == after, "JSON select does not change temporary human selection state")

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        env_file = temporary / "environment.json"
        env_file.write_text(json.dumps(environment), encoding="utf-8")
        mock_docker = temporary / "docker"
        mock_docker.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        mock_docker.chmod(0o755)
        plan_env = os.environ.copy()
        plan_env["PATH"] = "%s%s%s" % (
            temporary, os.pathsep, plan_env.get("PATH", ""))
        plan_env["LC_ALL"] = "ja_JP.UTF-8"
        plan_env["NVG_UI_LANGUAGE"] = "ja"
        readable = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "narration-video-gen"),
             "--root", str(ROOT), "plan", "--check",
             "--profile", "linux-wan21-480p-vram16", "--seconds", "30",
             "--env-file", str(env_file)],
            env=plan_env, capture_output=True, text=True, check=False,
        )
        machine = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "narration-video-gen"),
             "--root", str(ROOT), "--json", "plan",
             "--profile", "linux-wan21-480p-vram16", "--seconds", "30",
             "--env-file", str(env_file)],
            env=plan_env, capture_output=True, text=True, check=False,
        )
    try:
        machine_plan = json.loads(machine.stdout)
    except ValueError:
        machine_plan = None
    check(readable.returncode == 0
          and "30秒動画の生成: 推定51分" in readable.stdout
          and "同一構成の観測範囲: 約51〜80分" in readable.stdout
          and ("[--] モデル: 未取得（" in readable.stdout
               or "[OK] モデル: 取得済み" in readable.stdout)
          and "[--] 実行イメージ: 未ビルド" in readable.stdout
          and "extrapolat" not in readable.stdout.lower(),
          "human plan shows concise localized preparation states and estimate")
    check(machine.returncode == 0 and machine_plan
          and machine_plan["runtime_estimate"]["minutes"] == 51
          and machine_plan["runtime_estimate"]["observation_range"]["count"] == 2
          and machine_plan["runtime_estimate"]["extrapolated"] is True
          and machine_plan["runtime_image"]["present"] is False,
          "JSON plan keeps detailed read-only estimate and image status")


@expected_ui_language("en")
def test_guided_plan_and_run_helpers():
    section("guided preparation and generation")
    from narration_video_gen.cli import (_choose_input_set, _choose_run_length, _confirm,
                                 _canonical_audio_source,
                                 _input_set_candidates, _render_generation_summary,
                                 _release_comfy_between_stages,
                                 _short_test_frames)
    from narration_video_gen import runner as runner_mod
    from narration_video_gen.runner import ComfyClient, trim_wav

    def write_input_wav(path):
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0" * 16000)

    check(_confirm("Continue?", input_fn=lambda _: "y")
          and not _confirm("Continue?", input_fn=lambda _: ""),
          "guided confirmations accept y and default to No")
    original_audio = "/input/narration.wav"
    generated_audio = _canonical_audio_source(
        "infinitetalk", original_audio, "/outputs/01-generate-audio.mp4")
    face_audio = _canonical_audio_source(
        "face-detailer", generated_audio, "/outputs/02-face-audio.mp4")
    rife_audio = _canonical_audio_source(
        "rife", face_audio, "/outputs/03-rife-audio.mp4")
    check(generated_audio == original_audio
          and face_audio == original_audio and rife_audio == original_audio,
          "visual post-stages keep the first narration track instead of AAC padding")
    class FreeClient:
        def __init__(self):
            self.calls = []

        def call(self, path, data):
            self.calls.append((path, data))
            return {}

    free_client = FreeClient()
    check(not _release_comfy_between_stages(free_client, 1, True)
          and not _release_comfy_between_stages(free_client, 2, False)
          and _release_comfy_between_stages(free_client, 2, True)
          and free_client.calls == [
              ("/free", {"unload_models": True, "free_memory": True})],
          "in-process GPU stages release prior models at stage boundaries")
    class EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_ignored):
            return False

        def read(self):
            return b""

    original_urlopen = runner_mod.urllib.request.urlopen
    runner_mod.urllib.request.urlopen = lambda *_args, **_kwargs: EmptyResponse()
    try:
        check(ComfyClient().call(
            "/free", {"unload_models": True, "free_memory": True}) is None,
            "ComfyUI mutation endpoints accept an empty successful response")
    finally:
        runner_mod.urllib.request.urlopen = original_urlopen
    wan21 = {"id": "wan21", "fps": 16, "short_test_frames": 89,
             "window": 49, "motion_frames": 9}
    wan22 = {"id": "wan22", "fps": 16, "short_test_frames": 89,
             "context": {"frames": 81, "overlap": 16}}
    check(_short_test_frames(wan21) == 89,
          "Wan 2.1 short test uses the recipe's 5.6-second frame count")
    check(_short_test_frames(wan22) == 89,
          "Wan 2.2 short test stays near 5.6 seconds and preserves 4n+1 packing")
    length = _choose_run_length(wan21, 27.0, input_fn=lambda _: "1",
                                output=StringIO())
    check(length["mode"] == "test" and length["frames"] == 89
          and abs(length["seconds"] - 5.5625) < 0.0001,
          "the recommended 27-second Wan 2.1 run is a 5.6-second short test")
    automated = _choose_run_length(
        wan21, 27.0, requested="short",
        input_fn=lambda _: (_ for _ in ()).throw(AssertionError("prompted")),
        output=StringIO())
    check(automated["mode"] == "test" and automated["frames"] == 89,
          "automation can request the same short test without prompting")
    previous_locale = os.environ.get("LC_ALL")
    try:
        os.environ["LC_ALL"] = "ja_JP.UTF-8"
        length_output = StringIO()
        with patch.dict(os.environ, {"NVG_UI_LANGUAGE": "ja"}):
            japanese_length = _choose_run_length(
                wan21, 27.0, input_fn=lambda _: "1", output=length_output)
    finally:
        if previous_locale is None:
            os.environ.pop("LC_ALL", None)
        else:
            os.environ["LC_ALL"] = previous_locale
    check(japanese_length["label"] == "短尺(テスト)"
          and "短尺(テスト)" in length_output.getvalue()
          and "window" not in length_output.getvalue().lower(),
          "Japanese run choice uses the concise short-test label")
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        first = temporary / "inputs" / "first"
        second = temporary / "inputs" / "second"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        image_a = first / "image.png"
        audio = first / "audio.wav"
        image_b = second / "portrait.jpg"
        audio_b = second / "narration.wav"
        image_a.write_bytes(b"png")
        image_b.write_bytes(b"jpg")
        write_input_wav(audio)
        write_input_wav(audio_b)
        os.utime(image_a, (100, 100))
        os.utime(audio, (100, 100))
        os.utime(image_b, (200, 200))
        os.utime(audio_b, (200, 200))
        check(_choose_input_set(temporary, input_fn=lambda _: "1")
              == (str(image_b), str(audio_b)),
              "the newest complete user input is the default selection")
        bundled = temporary / "assets" / "characters" / "sample"
        bundled.mkdir(parents=True)
        bundled_image = bundled / "image.png"
        bundled_audio = bundled / "audio.wav"
        bundled_image.write_bytes(b"png")
        write_input_wav(bundled_audio)
        combined = _input_set_candidates(temporary)
        check(len(combined) == 3
              and [item["source"] for item in combined]
                  == ["user", "user", "bundled"]
              and _choose_input_set(temporary, input_fn=lambda _: "3")
                  == (str(bundled_image), str(bundled_audio)),
              "bundled characters remain selectable beside custom inputs")

        single_root = temporary / "single"
        single = single_root / "inputs" / "only"
        single.mkdir(parents=True)
        single_image = single / "image.png"
        single_audio = single / "audio.wav"
        single_image.write_bytes(b"png")
        write_input_wav(single_audio)
        check(_choose_input_set(single_root) == (str(single_image), str(single_audio)),
              "one complete input set is selected without prompting")

        fallback_root = temporary / "fallback"
        fallback = fallback_root / "assets" / "characters" / "bundled"
        fallback.mkdir(parents=True)
        fallback_image = fallback / "image.png"
        fallback_audio = fallback / "audio.wav"
        fallback_image.write_bytes(b"png")
        write_input_wav(fallback_audio)
        check(_choose_input_set(fallback_root) == (str(fallback_image), str(fallback_audio)),
              "bundled character sets are used when inputs has no sets")
        summary = StringIO()
        _render_generation_summary(
            temporary,
            {"model_family": "wan21-infinitetalk", "resolution": [832, 480]},
            str(image_a), str(audio), 26.7, {"known": True, "minutes": 60},
            output=summary)
        rendered = summary.getvalue()
        check("Image      : inputs/first/image.png" in rendered
              and "Audio      : inputs/first/audio.wav" in rendered
              and "Length     : Full length (26.7 seconds)" in rendered,
              "final confirmation shows both selected paths and measured duration")

        incomplete_root = temporary / "incomplete"
        incomplete = incomplete_root / "inputs" / "scripted"
        incomplete.mkdir(parents=True)
        (incomplete / "portrait.png").write_bytes(b"png")
        (incomplete / "script.txt").write_text("hello", encoding="utf-8")
        message = StringIO()
        check(_choose_input_set(incomplete_root, output=message) is None
              and "narration-video-gen tts" in message.getvalue(),
              "a script-only set points to the local narration workflow")

        source_wav = temporary / "source.wav"
        short_wav = temporary / "short.wav"
        with wave.open(str(source_wav), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\0\0" * (16000 * 10))
        written_seconds = trim_wav(source_wav, short_wav, 5.5625)
        check(abs(written_seconds - 5.5625) < 0.0001
              and frames_for_audio(written_seconds, 16) == 89,
              "short test audio is physically trimmed to the selected frame duration")

    client = ComfyClient()
    client.call = lambda path: {"ready": True}
    try:
        client.wait_ready(timeout=1)
        ready = True
    except Exception:
        ready = False
    check(ready, "guided run can wait for the generation service")

    prompt_id = "long-kernel"
    history_polls = []
    original_progress_socket = runner_mod._ProgressSocket
    runner_mod._ProgressSocket = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("websocket unavailable"))
    client = ComfyClient()

    def delayed_history(path):
        history_polls.append(path)
        if len(history_polls) == 1:
            raise TimeoutError("timed out")
        return {prompt_id: {"status": {"completed": True}, "outputs": {}}}

    client.call = delayed_history
    try:
        completed = client.wait(prompt_id, poll_seconds=0)
    finally:
        runner_mod._ProgressSocket = original_progress_socket
    check(completed["status"]["completed"] and len(history_polls) == 2,
          "a busy ComfyUI history endpoint is retried after a read timeout")


def test_retime_audio_codec_selection():
    section("retime audio codec selection")
    from narration_video_gen import runner as runner_mod

    template = [
        "ffmpeg", "-i", "/output/rife.mp4", "-i", "/input/narration.wav",
        "{audio_codec_args}", "-t", "{audio_duration}", "{output}",
    ]

    def resolved_command(codec):
        responses = iter([
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"streams": [{
                    "codec_name": codec, "duration": "5.562500",
                }]}),
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ])
        commands = []
        original_run = subprocess.run

        def fake_run(command, **_kwargs):
            commands.append(command)
            return next(responses)

        subprocess.run = fake_run
        try:
            result = runner_mod.run_command_stage(
                template, "self", "/input/narration.wav", "/output/final.mp4")
        finally:
            subprocess.run = original_run
        check(result == "/output/final.mp4",
              "%s audio completes the command stage" % codec)
        return commands

    pcm_commands = resolved_command("pcm_s16le")
    pcm_ffmpeg = pcm_commands[1]
    start = pcm_ffmpeg.index("-c:a")
    check(pcm_ffmpeg[start:start + 4] == ["-c:a", "aac", "-b:a", "192k"],
          "PCM narration is encoded once as MP4-compatible AAC")
    check("5.562500" in pcm_ffmpeg and "/output/final.mp4" in pcm_ffmpeg,
          "retime resolves the measured duration and destination")

    aac_commands = resolved_command("aac")
    aac_ffmpeg = aac_commands[1]
    start = aac_ffmpeg.index("-c:a")
    check(aac_ffmpeg[start:start + 2] == ["-c:a", "copy"]
          and "-b:a" not in aac_ffmpeg,
          "existing AAC audio is copied without another lossy encode")


@expected_ui_language("en")
def test_background_run_state_and_errors():
    section("background generation status")
    from narration_video_gen.cli import (_background_command, _length_cli_value,
                                 _launch_background, _live_stage_remaining, _pid_is_run, _record_live_progress,
                                 _release_tts_backend_for_video,
                                 _reference_eta_state, _sampler_node_steps,
                                 _sampler_progress_maxima,
                                 _run_timing, _tracks_run,
                                 cmd_cancel)
    from narration_video_gen import cli
    from narration_video_gen import runner as runner_mod
    from narration_video_gen.runner import _execution_error, _stage_command_prefix

    backend_checks = iter((True, False))
    backend_commands = []
    original_container_running = cli._container_running
    original_run = cli.subprocess.run
    cli._container_running = lambda name: (
        check(name == "narration-video-gen-tts",
              "video run only releases the managed TTS container")
        or next(backend_checks))
    cli.subprocess.run = lambda command, **kwargs: (
        backend_commands.append((command, kwargs))
        or SimpleNamespace(returncode=0, stdout="", stderr=""))
    try:
        backend_released = _release_tts_backend_for_video(ROOT)
    finally:
        cli._container_running = original_container_running
        cli.subprocess.run = original_run
    check(backend_released
          and backend_commands[0][0]
          == [str(ROOT / "scripts" / "tts-backend.sh"), "stop"]
          and backend_commands[0][1].get("timeout") == 30,
          "video run stops the managed TTS engine before claiming GPU memory")

    backend_commands = []
    cli._container_running = lambda _name: False
    cli.subprocess.run = lambda command, **kwargs: backend_commands.append(command)
    try:
        already_released = _release_tts_backend_for_video(ROOT)
    finally:
        cli._container_running = original_container_running
        cli.subprocess.run = original_run
    check(already_released and not backend_commands,
          "video run leaves Docker untouched when the TTS engine is already stopped")

    backend_checks = iter((True, True))
    cli._container_running = lambda _name: next(backend_checks)
    cli.subprocess.run = lambda _command, **_kwargs: SimpleNamespace(
        returncode=0, stdout="", stderr="")
    try:
        release_failed = _release_tts_backend_for_video(ROOT)
    finally:
        cli._container_running = original_container_running
        cli.subprocess.run = original_run
    check(not release_failed,
          "video run is blocked when the TTS engine remains running")

    launch_source = inspect.getsource(cli._launch_background)
    active_check = launch_source.index("active and _pid_is_run(active)")
    guided_release = launch_source.index(
        "not _release_tts_backend_for_video(args.root)")
    worker_launch = launch_source.index("subprocess.Popen(")
    run_source = inspect.getsource(cli._run_pipeline)
    defensive_release = run_source.index(
        "not _release_tts_backend_for_video(args.root)")
    comfy_client = run_source.index("client = runner_mod.ComfyClient")
    check(active_check < guided_release < worker_launch
          and defensive_release < comfy_client,
          "guided, direct, and detached video runs release TTS only before launch")

    error = _execution_error({
        "messages": [["execution_error", {
            "exception_message": "CUDA out of memory. Tried to allocate 442 MiB. "
                                 "GPU 0 has 275.88 MiB is free."
        }]]
    })
    check(error.code == "gpu-out-of-memory"
          and error.context["allocation_requested"] == "442 MiB"
          and error.context["memory_free"] == "275.88 MiB"
          and "execution_error" in error.detail,
          "ComfyUI OOM is reduced to a stable code while preserving technical detail")
    check(_stage_command_prefix("self") == []
          and _stage_command_prefix("narration-video-gen-comfy") == [
              "docker", "exec", "narration-video-gen-comfy"],
          "single-container calibration post-stages bypass Docker exec only explicitly")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = run_state.write(
            root, "test-run", status="failed", error_code=error.code,
            error=str(error), error_context=error.context,
            log="outputs/test-run/run.log")
        loaded = run_state.load(root, "test-run")
        check(loaded == state and run_state.latest(root)["run_id"] == "test-run",
              "run state is written atomically and can be selected as latest")
        status_env = os.environ.copy()
        status_env["LC_ALL"] = "ja_JP.UTF-8"
        status_env["NVG_UI_LANGUAGE"] = "ja"
        status = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "narration-video-gen"),
             "--root", str(root), "status"],
            env=status_env, capture_output=True, text=True, check=False)
        check(status.returncode == 1
              and "状態   : 失敗" in status.stdout
              and "GPUメモリ不足（空き275.88 MiB、追加で442 MiB必要）" in status.stdout
              and "execution_error" not in status.stdout,
              "status shows a concise localized failure instead of the raw ComfyUI dump")

        args = SimpleNamespace(
            root=ROOT, server="127.0.0.1:8188", container="comfy",
            image="portrait.png", audio="narration.wav", source_video=None,
            stages=None, length="short", frames=None, mask_only=False,
            accept_unverified=True, force=False, dry_run=False,
            profile_dir=[Path("/tmp/profile-overlay")])
        command = _background_command(args, "profile-id", "test-run")
        check("--background-worker" in command
              and command[command.index("--length") + 1] == "short"
              and command[command.index("--profile") + 1] == "profile-id",
              "guided run passes its approved inputs to a detached worker")
        check(command[command.index("--profile-dir") + 1]
              == "/tmp/profile-overlay"
              and command.index("--profile-dir") < command.index("run"),
              "guided run preserves run-scoped profile overlays in its worker")
        check(_length_cli_value({"mode": "test"}) == "short"
              and _length_cli_value({"mode": "full"}) == "full",
              "guided length choices use the public short/full worker arguments")

        # The guided path starts a detached worker.  Its profile comes from the
        # catalog dict, so verify that the worker receives its id rather than a
        # stale local variable name.
        launched = []
        original_popen = cli.subprocess.Popen
        original_release = cli._release_tts_backend_for_video
        cli.subprocess.Popen = lambda command, **_kwargs: (
            launched.append(command) or SimpleNamespace(pid=4242))
        cli._release_tts_backend_for_video = lambda _root: True
        try:
            launch_args = SimpleNamespace(**vars(args))
            launch_args.root = root
            launch_result = _launch_background(
                launch_args, {"id": "profile-id", "evidence": {}}, {},
                "launched-run", {"known": False})
        finally:
            cli.subprocess.Popen = original_popen
            cli._release_tts_backend_for_video = original_release
        launched_state = run_state.load(root, "launched-run")
        check(launch_result == 0 and launched
              and launched[0][launched[0].index("--profile") + 1] == "profile-id"
              and launched_state and launched_state["pid"] == 4242,
              "guided run launches a worker with the selected profile id")

        tracked_args = SimpleNamespace(
            run_id="direct-run", dry_run=False, background_worker=False)
        check(_tracks_run(tracked_args)
              and not _tracks_run(SimpleNamespace(
                  run_id=None, dry_run=False, background_worker=False))
              and not _tracks_run(SimpleNamespace(
                  run_id="preview", dry_run=True, background_worker=False))
              and not _tracks_run(SimpleNamespace(run_id="status-query")),
              "explicit run ids enable status tracking without tracking dry runs")

        process = subprocess.Popen([
            sys.executable, "-c", "import time; time.sleep(10)",
            "narration-video-gen", "run", "--run-id", "direct-run",
        ])
        try:
            direct_run_detected = False
            for _ in range(50):
                direct_run_detected = _pid_is_run(
                    {"pid": process.pid, "run_id": "direct-run"})
                if direct_run_detected:
                    break
                time.sleep(0.01)
            run_state.write(root, "direct-run", status="running", pid=process.pid)
            direct_status = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "narration-video-gen"),
                 "--root", str(root), "status", "--run-id", "direct-run"],
                env={**os.environ, "LC_ALL": "C"}, capture_output=True, text=True,
                check=False)
            check(direct_run_detected
                  and not _pid_is_run({"pid": process.pid, "run_id": "other-run"})
                  and direct_status.returncode == 0
                  and "Status : running" in direct_status.stdout,
                  "status recognizes a direct run by its exact run id")

            original_call = runner_mod.ComfyClient.call
            runner_mod.ComfyClient.call = lambda *unused, **ignored: (_ for _ in ()).throw(
                OSError("test server is unavailable"))
            try:
                cancel_result = cmd_cancel(SimpleNamespace(
                    root=root, run_id="direct-run", server="127.0.0.1:9"))
            finally:
                runner_mod.ComfyClient.call = original_call
            process.wait(timeout=5)
            check(cancel_result == 0
                  and run_state.load(root, "direct-run")["status"] == "cancelled",
                  "cancel stops and records a tracked direct run")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        elapsed, remaining = _run_timing(
            {"started_epoch": 1000, "estimated_total_seconds": 720}, now=1180)
        check(elapsed == 180 and remaining == 540,
              "status derives elapsed and estimated remaining time from run state")
        live_remaining = _live_stage_remaining({
            "live_progress": {
                "value": 1, "max": 6, "remaining_seconds": 500,
                "observed_at_epoch": 1180,
            }}, now=1200)
        check(live_remaining == 480,
              "status switches to the current sampler's measured ETA after one step")
        sampler_steps = _sampler_node_steps({
            "15": {"class_type": "WanVideoSampler", "inputs": {"steps": 6}},
            "12": {"class_type": "VAELoader", "inputs": {}},
        })
        framepack_workflow = {
            "11": {"class_type": "WanVideoEmptyEmbeds", "inputs": {
                "num_frames": 457}},
            "12": {"class_type": "WanVideoAddS2VEmbeds", "inputs": {
                "embeds": ["11", 0], "enable_framepack": True,
                "start_from_ref": True, "frame_window_size": 80}},
            "14": {"class_type": "WanVideoSampler", "inputs": {
                "image_embeds": ["12", 0], "steps": 4}},
        }
        framepack_maxima = _sampler_progress_maxima(framepack_workflow)
        check(framepack_maxima == {"14": 24},
              "FramePack progress covers every sampling window")
        run_state.write(root, "progress-filter", status="running")
        progress_args = SimpleNamespace(
            root=root, run_id="progress-filter", background_worker=True)
        _record_live_progress(
            progress_args, "infinitetalk", 1000, sampler_steps,
            {"node": "12", "value": 1633, "max": 1633})
        ignored_progress = run_state.load(root, "progress-filter")
        _record_live_progress(
            progress_args, "infinitetalk", 1000, sampler_steps,
            {"node": "15", "value": 1260, "max": 1260})
        ignored_sampler_internal_progress = run_state.load(root, "progress-filter")
        _record_live_progress(
            progress_args, "infinitetalk", 1000, sampler_steps,
            {"node": 15, "value": 1, "max": 6})
        sampler_progress = run_state.load(root, "progress-filter").get("live_progress")
        check(sampler_steps == {"15": 6}
              and ignored_progress.get("live_progress") is None
              and ignored_sampler_internal_progress.get("live_progress") is None
              and sampler_progress is not None
              and sampler_progress["stage"] == "infinitetalk"
              and sampler_progress["value"] == 1
              and sampler_progress["max"] == 6,
              "status ignores pre-sampler work and accepts only denoising steps")
        run_state.write(root, "framepack-progress", status="running")
        framepack_args = SimpleNamespace(
            root=root, run_id="framepack-progress", background_worker=True)
        _record_live_progress(
            framepack_args, "s2v", 1000, framepack_maxima,
            {"node": 14, "value": 9, "max": 24})
        framepack_progress = run_state.load(
            root, "framepack-progress").get("live_progress")
        _record_live_progress(
            framepack_args, "s2v", 1000, framepack_maxima,
            {"node": 14, "value": 1260, "max": 1260})
        check(framepack_progress is not None
              and framepack_progress["value"] == 9
              and framepack_progress["max"] == 24
              and run_state.load(root, "framepack-progress")["live_progress"]
              == framepack_progress,
              "FramePack ETA tracks the combined sampler and ignores loader progress")
        reference_profile = {"evidence": {"gpu_model": "NVIDIA RTX A4000 (16 GiB)"}}
        reference_estimate = {"known": True, "minutes": 66, "extrapolated": False}
        same_gpu = {"gpus": [{"name": "NVIDIA RTX A4000", "vram_gib": 16}]}
        other_gpu = {"gpus": [{"name": "NVIDIA GeForce RTX 4090", "vram_gib": 24,
                                "compute_capability": "8.9", "power_limit_w": 450}]}
        similar_gpu = {"gpus": [{"name": "NVIDIA RTX A5000", "vram_gib": 24,
                                  "compute_capability": "8.6", "power_limit_w": 230}]}
        check(_reference_eta_state(reference_profile, same_gpu, reference_estimate)
              .get("eta_source") == "reference"
              and _reference_eta_state(reference_profile, similar_gpu, reference_estimate)
              .get("eta_reference_match") == "same-generation"
              and not _reference_eta_state(reference_profile, other_gpu, reference_estimate),
              "pre-step ETA uses the nearest available same-generation benchmark")


def test_public_name_is_consistent():
    section("public name")
    entrypoint = ROOT / "bin" / "narration-video-gen"
    check(entrypoint.is_file(), "narration-video-gen entry point exists")
    check(bool(entrypoint.stat().st_mode & 0o111),
          "narration-video-gen entry point is executable")
    check(not (ROOT / "bin" / "vglab").exists(), "legacy vglab entry point is absent")

    legacy = re.compile(r"video-gen-lab|videogenlab|\bvglab\b|\.vglab-inputs")
    excluded = {ROOT / "tests" / "test_catalog.py"}
    suffixes = {"", ".cmd", ".md", ".ps1", ".py", ".sh", ".yaml", ".yml"}
    stale = []
    for path in ROOT.rglob("*"):
        if (not path.is_file() or path in excluded or path.suffix not in suffixes
                or ".git" in path.parts or "__pycache__" in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if legacy.search(text):
            stale.append(str(path.relative_to(ROOT)))
    check(not stale, "legacy public names are absent%s" %
          (": %s" % ", ".join(stale) if stale else ""))

    compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "narration_video_gen" / "cli.py").read_text(encoding="utf-8")
    expected_container = "narration-video-gen-comfy"
    check("container_name: %s" % expected_container in compose,
          "Compose uses the public container name")
    check('default="%s"' % expected_container in cli,
          "CLI default container matches Compose")


def test_generated_matrix_is_current():
    section("generated matrix is up to date")
    from narration_video_gen.matrix import render_matrix
    expected = render_matrix(Catalog(ROOT))
    actual = (ROOT / "docs" / "hardware-matrix.md").read_text(encoding="utf-8")
    check(expected == actual,
          "docs/hardware-matrix.md matches the catalog (regenerate with "
          "`narration-video-gen matrix --write`)")
    wan21_section = actual.split("## Linux / wan22-s2v", 1)[0]
    check("`linux-wan21-720p-vram24`" in wan21_section
          and "`linux-wan21-720p-vram24-tiled`" not in wan21_section,
          "matrix favours the native 24 GiB 720p profile over the tiled fallback")


def test_failure_remedies_name_the_next_setting(catalog):
    """A failed run has to say what to change, not only what ran out.

    The catalog records what worked on the machine that produced it. That a
    setting can work is not the same as it working here -- a driver holding a
    few hundred MiB more, a desktop session on the same card, or a longer clip
    all move the boundary. So the failure path carries its own advice, resolved
    against the reader's machine.
    """
    section("failure remedies")
    from narration_video_gen import remedies

    profile = catalog.profiles["linux-wan22-720p-vram16"]
    current = profile["settings"]["blocks_to_swap"]
    gpu = remedies.for_failure(
        "gpu-out-of-memory", profile, {},
        {"allocation_requested": "676.00 MiB", "memory_free": "649.88 MiB"})
    swap_it = next((r for r in gpu if r.setting == "blocks_to_swap"), None)
    check(swap_it is not None, "a VRAM failure proposes blocks_to_swap")
    check(swap_it and swap_it.current == current and swap_it.suggested > current,
          "the proposal moves up from the value the run actually used")
    check(any("676.00 MiB" in r.detail for r in gpu),
          "the proposal quotes how much the run was short by")
    with_resolution = remedies.for_failure(
        "gpu-out-of-memory", profile, {}, recipe=catalog.recipe_for(profile))
    check(any(r.action == "Drop to a lower resolution profile" for r in with_resolution),
          "a GPU OOM also offers a lower resolution after 720p remedies")

    face_profile = dict(
        profile,
        settings=dict(profile["settings"], face_detailer_size=384,
                      face_detailer_blocks_to_swap=30))
    face_gpu = remedies.for_failure(
        "gpu-out-of-memory", face_profile, {},
        {"allocation_requested": "2.32 GiB", "memory_free": "1.69 GiB"},
        stage="face-detailer")
    face_swap = next((r for r in face_gpu
                      if r.setting == "face_detailer_blocks_to_swap"), None)
    face_size = next((r for r in face_gpu
                      if r.setting == "face_detailer_size"), None)
    check(face_swap is not None and face_swap.current == 30
          and face_swap.suggested == 32,
          "a Face Detailer OOM changes its independent swap budget")
    check(face_size is not None and face_size.current == 384
          and face_size.suggested == 320,
          "a Face Detailer OOM also offers the next smaller activation plane")
    from narration_video_gen.cli import _remedies_for_state
    saved_failure = _remedies_for_state(
        SimpleNamespace(root=ROOT, profile_dir=[]),
        {"error_code": "gpu-out-of-memory", "stage": "face-detailer",
         "profile": "external-profile-no-longer-loaded",
         "profile_settings": face_profile["settings"],
         "profile_requires": face_profile["requires"],
         "recipe_resolution": [1280, 720],
         "error_context": {"host_at_failure": {"ram_gib": 30, "swap_gib": 32}}})
    check(any(r.setting == "face_detailer_blocks_to_swap"
              and r.current == 30 for r in saved_failure),
          "status retains custom-profile remedies without needing --profile-dir again")

    maxed = dict(profile, settings=dict(profile["settings"],
                                        blocks_to_swap=remedies.BLOCK_SWAP_MAX))
    topped = remedies.for_failure("gpu-out-of-memory", maxed, {}, {})
    check(all(r.setting != "blocks_to_swap" for r in topped),
          "no more block swapping is proposed once it is already at the maximum")

    # Host memory splits in two: swap covers a peak, RAM covers a working set.
    wan21 = catalog.profiles["linux-wan21-720p-vram16"]
    needed = wan21["requires"]["swap_gib_min"]
    thin = remedies.for_failure(
        "host-out-of-memory", wan21, {"memory": {"ram_gib": 19.5, "swap_gib": 8.0}})
    add_swap = next((r for r in thin if r.setting == "swap_gib"), None)
    check(add_swap is not None and add_swap.suggested == needed,
          "too little swap proposes exactly what the profile asks for")

    ample = remedies.for_failure(
        "host-out-of-memory", wan21, {"memory": {"ram_gib": 12.0, "swap_gib": 40.0}})
    check(all(r.setting != "swap_gib" for r in ample),
          "ample swap stops proposing more swap")
    check(any("RAM" in r.action for r in ample),
          "a RAM shortage that swap cannot cover proposes more RAM")

    check(remedies.for_failure("comfy-stage-failed", profile, {}) == [],
          "a failure with no known remedy stays silent rather than guessing")


def test_machine_local_calibration_state(catalog):
    section("machine-local calibration state")
    from narration_video_gen.cli import _local_profile
    env = {
        "platform": "linux",
        "gpus": [{"name": "NVIDIA RTX A4000", "uuid": "GPU-test-a4000",
                  "vram_mib": 16376, "vram_gib": 15.99}],
    }
    other_env = {
        "platform": "windows-wsl2",
        "gpus": [{"name": "NVIDIA RTX A4000", "uuid": "GPU-other-a4000",
                  "vram_mib": 16376, "vram_gib": 15.99}],
    }
    base = json.loads(json.dumps(catalog.profiles["linux-wan22-720p-vram16"]))
    env["platform"] = "windows-wsl2"
    base["platform"] = "windows-wsl2"
    base["settings"]["face_detailer_size"] = 368
    base["settings"].pop("face_detailer_blocks_to_swap", None)
    base["settings"]["wsl_ram_gib"] = 20
    base["settings"]["wsl_swap_gib"] = 16
    base["settings"]["vram_ballast_mib"] = 4096
    base["settings"]["vram_ballast_device_index"] = 0
    options = ["face-detailer-on"]
    recipe = catalog.recipe_for(base, options)
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        run = temporary / "run"
        promoted = run / "profiles" / "recommended" / "candidate.yaml"
        promoted.parent.mkdir(parents=True)
        profile = json.loads(json.dumps(base))
        profile.pop("_path", None)
        profile["id"] = "candidate"
        profile["settings"]["blocks_to_swap"] = 35
        profile["settings"]["face_detailer_size"] = 320
        profile["settings"]["face_detailer_blocks_to_swap"] = 20
        profile["requires"]["host_ram_gib_min"] = 19.53
        profile["requires"]["swap_gib_min"] = 64.0
        raw = (json.dumps(profile, indent=2) + "\n").encode()
        promoted.write_bytes(raw)
        profile_set = {
            "schema_version": 1,
            "calibration_run_id": "test-calibration",
            "source_revision": "test-revision",
            "image_reference": "local:test",
            "gpu": {"name": "NVIDIA RTX A4000", "uuid": "GPU-test-a4000",
                    "memory.total": "16376"},
            "profiles": {"wan22-720p": {
                "id": "candidate", "path": str(promoted.relative_to(run)),
                "sha256": hashlib.sha256(raw).hexdigest(), "blocks_to_swap": 35,
            }},
        }
        profile_set_path = run / "profile-set.json"
        profile_set_path.write_text(json.dumps(profile_set), encoding="utf-8")
        previous = os.environ.get("NVG_CONFIG_HOME")
        os.environ["NVG_CONFIG_HOME"] = str(temporary / "config")
        try:
            record_path, _record = calibration_state.import_profile_set(
                profile_set_path, base, recipe, options, env)
            effective, metadata = calibration_state.resolve(
                base, recipe, options, env)
            wrong_gpu, _ = calibration_state.resolve(
                base, recipe, options, other_env)
            wrong_plan, _ = calibration_state.resolve(
                base, catalog.recipe_for(base, ["face-detailer-off"]),
                ["face-detailer-off"], env)
            record_path.with_suffix(".profile.yaml").write_text(
                "{}\n", encoding="utf-8")
            tampered, _ = calibration_state.resolve(base, recipe, options, env)
            calibration_state.set_enabled(record_path, False)
            disabled, _ = calibration_state.resolve(base, recipe, options, env)
        finally:
            if previous is None:
                os.environ.pop("NVG_CONFIG_HOME", None)
            else:
                os.environ["NVG_CONFIG_HOME"] = previous
        check(effective["settings"]["blocks_to_swap"] == 35 and metadata,
              "an exact GPU and plan automatically use the calibrated profile")
        check(not any(key in effective["settings"]
                      for key in calibration_state.BALLAST_SETTINGS),
              "a local calibration never keeps a capacity-simulation ballast")
        check(effective["settings"]["face_detailer_size"] == 368,
              "calibration changes block swap without resetting custom plan settings")
        check(effective["settings"]["face_detailer_blocks_to_swap"] == 20,
              "calibration retains exercised face settings absent from the base profile")
        check(effective["settings"]["wsl_ram_gib"] == 20
              and effective["settings"]["wsl_swap_gib"] == 64,
              "Windows calibration keeps WSL limits aligned with measured requirements")
        check(wrong_gpu["id"] == base["id"],
              "a different physical GPU cannot inherit the calibration")
        check(wrong_plan["id"] == base["id"],
              "a different plan composition cannot inherit the calibration")
        check(tampered["id"] == base["id"],
              "a modified materialized profile is not applied")
        check(disabled["id"] == base["id"],
              "a calibration can be disabled without deleting its evidence")
        check(stat.S_IMODE(record_path.stat().st_mode) == 0o600,
              "the persistent calibration record is private")

        profile["settings"]["blocks_to_swap"] = 36
        profile["evidence"]["duration_class"] = "full"
        profile["evidence"]["visual_review"] = "pending"
        profile["limitations"] = ["Full-length technical checks passed; visual review remains pending."]
        raw = (json.dumps(profile, indent=2) + "\n").encode()
        promoted.write_bytes(raw)
        profile_set["profiles"]["wan22-720p"]["sha256"] = hashlib.sha256(raw).hexdigest()
        profile_set_path.write_text(json.dumps(profile_set), encoding="utf-8")
        previous = os.environ.get("NVG_CONFIG_HOME")
        os.environ["NVG_CONFIG_HOME"] = str(temporary / "config")
        try:
            calibration_state.import_profile_set(
                profile_set_path, base, recipe, options, env)
            full_effective, _ = calibration_state.resolve(
                base, recipe, options, env)
        finally:
            if previous is None:
                os.environ.pop("NVG_CONFIG_HOME", None)
            else:
                os.environ["NVG_CONFIG_HOME"] = previous
        check(full_effective["settings"]["blocks_to_swap"] == 36
              and not any("short-calibrated" in item
                          for item in full_effective.get("limitations", [])),
              "a full calibration import does not retain the short-only warning")

    unknown_env = {
        "platform": "windows-wsl2",
        "gpus": [{"name": "NVIDIA Unknown 16GB GPU", "uuid": "GPU-unknown",
                  "vram_mib": 16384, "vram_gib": 16}],
    }
    unknown_base = catalog.profiles["windows-wan21-480p-vram16"]
    with tempfile.TemporaryDirectory() as directory:
        previous = os.environ.get("NVG_CONFIG_HOME")
        os.environ["NVG_CONFIG_HOME"] = str(Path(directory) / "config")
        try:
            effective, _recipe, metadata = _local_profile(
                unknown_base, catalog, unknown_env, [], enabled=True)
        finally:
            if previous is None:
                os.environ.pop("NVG_CONFIG_HOME", None)
            else:
                os.environ["NVG_CONFIG_HOME"] = previous
    check(effective is unknown_base
          and effective["settings"]["blocks_to_swap"] == 19
          and metadata is None,
          "plan and run keep catalog settings when no local calibration exists")



def test_measured_capability():
    section("measured capability probe")
    from narration_video_gen import measure as measure_mod

    # What a machine can run is the catalog's answer; the probe must not carry a
    # second, hard-coded copy of it that can drift.
    check(not hasattr(measure_mod, "TIERS") and not hasattr(measure_mod, "tier_for"),
          "the probe does not keep its own capability table")

    for reported, expected in ((15.99, 16), (23.99, 24), (5.66, 6), (11.75, 12)):
        env = {"gpus": [{"name": "card", "vram_gib": reported}]}
        check(measure_mod.vram_gib(env, {}) == expected,
              "%.2f GiB reported reads as %d GiB" % (reported, expected))

    check(measure_mod.decode_throttle("0x0000000000000004") == ["power_cap"],
          "a power-cap mask decodes to power_cap alone")
    check(measure_mod.decode_throttle("0x0000000000000024")
          == ["power_cap", "thermal_sw"],
          "a combined mask decodes to every reason it carries")
    check(measure_mod.decode_throttle("[N/A]") == [],
          "an unsupported throttle field decodes to no reasons")

    # Nine telemetry columns, then the throttle mask.
    hot = {"tflops": 32.6, "elapsed_seconds": 30.0,
           "throttle_field": "clocks_event_reasons.active",
           "samples": [["48", "1560", "1560", "95.0", "140.0", "40", "100", "4", "16"] + ["0x0"],
                       ["98", "520", "1560", "97.0", "140.0", "100", "100", "4", "16"]
                       + ["0x0000000000000020"]]}
    summary = measure_mod._summarise(hot)
    check(summary["thermally_throttled"] is True,
          "a thermal slowdown in any sample is reported")
    check(summary["temperature_rise_c"] == 50.0, "the temperature rise is the span")
    check(summary["clock_ratio_pct"] == 33,
          "the clock ratio compares the last clock against the rated maximum")

    # An idle first and last sample must not be read as the sustained clock.
    idle_row = ["35", "210", "1560", "12.0", "140.0", "30", "0", "4", "16", "0x0"]
    padded = dict(hot, samples=[idle_row] + hot["samples"] + [idle_row])
    padded_summary = measure_mod._summarise(padded)
    check(padded_summary["clocks_sm_mhz"] == 520.0,
          "idle samples around the load are left out of the summary")
    check(padded_summary["samples"] == 2 and padded_summary["samples_observed"] == 4,
          "the summary counts the busy samples and reports how many were taken")
    check(measure_mod.busy_samples([idle_row]) == [idle_row],
          "a measurement with no busy sample still reports what it saw")

    cool = dict(hot, samples=[["60", "1900", "1950", "160", "200", "45", "100", "5", "8"]
                              + ["0x0000000000000004"]])
    check(measure_mod._summarise(cool)["thermally_throttled"] is False,
          "a power cap alone is not reported as thermal throttling")

    a4000 = {"platform": "linux", "memory": {"ram_gib": 64.0},
             "gpus": [{"name": "NVIDIA RTX A4000", "vram_gib": 15.99}]}
    hot_record = dict(measure_mod._summarise(hot), vram_mib=16376)
    warnings = measure_mod.warnings_for(a4000, hot_record)
    check(len(warnings) == 1 and "冷却" in warnings[0][0],
          "thermal throttling produces one cooling warning")
    check(set(measure_mod.assess(a4000, hot_record)) == {"warnings"},
          "the measurement reports warnings only, leaving capability to select")

    wsl = {"platform": "windows-wsl2", "memory": {"ram_gib": 32.0},
           "gpus": [{"name": "NVIDIA GeForce RTX 4070", "vram_gib": 11.75}]}
    cool_record = dict(measure_mod._summarise(cool), vram_mib=12028)
    messages = [ja for ja, _en in measure_mod.warnings_for(wsl, cool_record)]
    check(any("フル尺" in m for m in messages),
          "a 12 GiB WSL2 host with 32 GiB RAM is warned about full-length runs")

    linux = dict(wsl, platform="linux")
    check(measure_mod.warnings_for(linux, cool_record) == [],
          "the same GPU on Linux gets no full-length warning")

    calls = []

    def fake_runner(args, timeout=None):
        calls.append(args)
        return ('noise\n@@PROBE@@' + json.dumps({
            "torch": "2.8.0+cu128", "cuda": "12.8", "gpu_name": "NVIDIA RTX A4000",
            "vram_mib": 16376, "matmuls": 100, "elapsed_seconds": 30.0,
            "tflops": 32.6, "throttle_field": "clocks_event_reasons.active",
            "samples": hot["samples"],
        }) + '\n')

    record = measure_mod.measure("pinned@sha256:test", seconds=30, runner=fake_runner)
    check(record["tflops_fp16"] == 32.6, "measure returns the probe's throughput")
    check(record["thermally_throttled"] is True,
          "measure carries the throttle verdict through")
    check("--gpus" in calls[0] and "pinned@sha256:test" in calls[0],
          "measure runs the pinned image with the GPU attached")

    def empty_runner(args, timeout=None):
        return "nothing useful"

    try:
        measure_mod.measure("pinned@sha256:test", runner=empty_runner)
    except measure_mod.MeasureError:
        check(True, "a probe that prints no result raises MeasureError")
    else:
        check(False, "a probe that prints no result raises MeasureError")



    calls = []

    def pinned_runner(args, timeout=None):
        calls.append(args)
        return '@@PROBE@@' + json.dumps({
            "torch": "2.8.0+cu128", "cuda": "12.8", "gpu_name": "NVIDIA GeForce RTX 3060",
            "gpu_uuid": "GPU-small", "vram_mib": 12288, "matmuls": 10,
            "elapsed_seconds": 30.0, "tflops": 12.0,
            "throttle_field": "clocks_event_reasons.active", "samples": cool["samples"],
        }) + '\n'

    default = measure_mod.measure("pinned@sha256:test", seconds=30,
                                  runner=pinned_runner)
    check("all" in calls[0],
          "measure sees the GPUs the way a generation does")
    check(all("0" != argument for argument in calls[0][-1:]),
          "the probe resolves its own telemetry target instead of being told an index")
    check("get_device_properties" in measure_mod.PROBE_SOURCE
          and 'device_uuid.startswith("GPU-")' in measure_mod.PROBE_SOURCE,
          "telemetry is aimed at the card torch benchmarks, by UUID")
    check("telemetry_skipped" in measure_mod.PROBE_SOURCE
          and "visible != 1" in measure_mod.PROBE_SOURCE,
          "the probe declines telemetry it cannot attribute to the benchmarked card")

    ambiguous = {"platform": "linux", "gpus": [
        {"name": "NVIDIA GeForce RTX 3060", "uuid": "GPU-a", "vram_gib": 11.75},
        {"name": "NVIDIA GeForce RTX 3090", "uuid": "GPU-b", "vram_gib": 23.99},
    ]}
    unidentified = {"vram_mib": 12028, "gpu_uuid": None, "gpu_name": None}
    check(measure_mod.measured_gpu(ambiguous, unidentified) == {},
          "an unidentified card is not silently attributed to another one")
    check(measure_mod.vram_gib(ambiguous, unidentified) == 12,
          "an unidentified card falls back to what the probe itself saw")

    bare = "302c804c-8d90-285e-cd78-31da57368c1d"
    check(measure_mod.normalise_gpu_uuid(bare) == "GPU-" + bare,
          "a bare CUDA UUID is given the prefix nvidia-smi expects")
    check(measure_mod.normalise_gpu_uuid("GPU-" + bare) == "GPU-" + bare,
          "an already prefixed CUDA UUID is not prefixed twice")
    check(measure_mod.normalise_gpu_uuid(None) is None,
          "a missing CUDA UUID stays missing")
    pinned = measure_mod.measure("pinned@sha256:test", seconds=30,
                                 runner=pinned_runner, gpu_uuid="GPU-small")
    check("device=GPU-small" in calls[1],
          "measure can still be pinned to one card on request")
    check(default["gpu_uuid"] == "GPU-small" and pinned["gpu_uuid"] == "GPU-small",
          "measure reports which card it measured")

    mixed = {"platform": "linux", "memory": {"ram_gib": 64.0}, "gpus": [
        {"name": "NVIDIA GeForce RTX 3060", "uuid": "GPU-small", "vram_gib": 11.75},
        {"name": "NVIDIA GeForce RTX 3090", "uuid": "GPU-large", "vram_gib": 23.99},
    ]}
    check(measure_mod.vram_gib(mixed, pinned) == 12,
          "the record describes the measured card, not the largest one present")


    from narration_video_gen import cli as cli_mod

    stderr, sys.stderr = sys.stderr, StringIO()
    try:
        bad = cli_mod.main(["detect", "--measure", "--measure-seconds", "0"])
    finally:
        seconds_message = sys.stderr.getvalue()
        sys.stderr = stderr
    check(bad == cli_mod.EXIT_ERROR and "greater than zero" in seconds_message,
          "a non-positive measurement duration is rejected")

    saved_latest, saved_pid = run_state.latest, cli_mod._pid_is_run
    run_state.latest = lambda root, running_only=False: {"run_id": "busy", "pid": 1}
    cli_mod._pid_is_run = lambda state: True
    stderr, sys.stderr = sys.stderr, StringIO()
    try:
        args = SimpleNamespace(root=ROOT, measure_seconds=30, json=False)
        busy = cli_mod._measure_environment(args, {})
    finally:
        busy_message = sys.stderr.getvalue()
        sys.stderr = stderr
        run_state.latest, cli_mod._pid_is_run = saved_latest, saved_pid
    check(busy is None and ("使用中" in busy_message or "in use" in busy_message),
          "measurement is refused while a generation is running")

    saved_processes = measure_mod.compute_processes
    measure_mod.compute_processes = lambda gpu_uuid=None: [{"pid": 4242, "used_mib": 8000}]
    stderr, sys.stderr = sys.stderr, StringIO()
    try:
        args = SimpleNamespace(root=ROOT, measure_seconds=30, json=False)
        occupied = cli_mod._measure_environment(args, {})
    finally:
        occupied_message = sys.stderr.getvalue()
        sys.stderr = stderr
        measure_mod.compute_processes = saved_processes
    check(occupied is None and ("使用中" in occupied_message
                               or "in use" in occupied_message),
          "measurement is refused when another process already holds the GPU")

    measure_mod.compute_processes = lambda gpu_uuid=None: [{"pid": 1, "used_mib": 1}]
    quiet_env = {}
    stdout, sys.stdout = sys.stdout, StringIO()
    stderr, sys.stderr = sys.stderr, StringIO()
    try:
        cli_mod._measure_environment(SimpleNamespace(root=ROOT, measure_seconds=30, json=False),
                                     quiet_env)
    finally:
        on_stdout = sys.stdout.getvalue()
        sys.stdout, sys.stderr = stdout, stderr
        measure_mod.compute_processes = saved_processes
    check(on_stdout == "" and quiet_env.get("warnings"),
          "a refused measurement leaves stdout clean and records the reason")

    saved_present, saved_measure = measure_mod.image_present, measure_mod.measure

    def raising(*args, **kwargs):
        raise measure_mod.MeasureError("docker was not found on PATH")

    # The measurement must follow the image plan guarantees, not the base tag a
    # docker prune can remove.
    saved_runtime = cli_mod.plan_mod.runtime_image_plan
    cli_mod.plan_mod.runtime_image_plan = lambda root, recipe=None: {
        "images": [{"name": "comfy", "image": "narration-video-gen-comfy:abc",
                    "present": True}]}
    check(cli_mod._measurement_image(ROOT) == "narration-video-gen-comfy:abc",
          "the measurement uses the built runtime image")
    cli_mod.plan_mod.runtime_image_plan = lambda root, recipe=None: {
        "images": [{"name": "comfy", "image": "narration-video-gen-comfy:abc",
                    "present": False}]}
    saved_present_probe = measure_mod.image_present
    measure_mod.image_present = lambda image, runner=None: True
    fallback = cli_mod._measurement_image(ROOT)
    check(fallback and fallback.startswith("pytorch/pytorch:"),
          "an unbuilt runtime image falls back to the pinned base")
    measure_mod.image_present = lambda image, runner=None: False
    check(cli_mod._measurement_image(ROOT) is None,
          "with neither image present there is nothing to measure in")
    measure_mod.image_present = saved_present_probe
    cli_mod.plan_mod.runtime_image_plan = saved_runtime

    with measure_mod.exclusive():
        try:
            with measure_mod.exclusive():
                check(False, "a second measurement cannot take the lock")
        except measure_mod.MeasureError:
            check(True, "a second measurement cannot take the lock")
    with measure_mod.exclusive():
        check(True, "the lock is released when the measurement ends")

    saved_pull, saved_runtime2 = measure_mod.pull, cli_mod.plan_mod.runtime_image_plan
    saved_present2, saved_busy = measure_mod.image_present, cli_mod._gpu_is_busy
    started_during_pull = {"pulled": False}

    def slow_pull(image, runner=None):
        started_during_pull["pulled"] = True

    cli_mod.plan_mod.runtime_image_plan = lambda root, recipe=None: {
        "images": [{"name": "comfy", "image": "x", "present": False}]}
    measure_mod.image_present = lambda image, runner=None: False
    measure_mod.pull = slow_pull
    cli_mod._confirm = lambda *a, **k: True
    cli_mod._gpu_is_busy = lambda args: started_during_pull["pulled"]
    stderr, sys.stderr = sys.stderr, StringIO()
    stdout, sys.stdout = sys.stdout, StringIO()
    try:
        raced_env = {}
        raced = cli_mod._run_measurement(
            SimpleNamespace(root=ROOT, measure_seconds=30, json=False), raced_env)
    finally:
        sys.stderr, sys.stdout = stderr, stdout
        measure_mod.pull, cli_mod.plan_mod.runtime_image_plan = saved_pull, saved_runtime2
        measure_mod.image_present, cli_mod._gpu_is_busy = saved_present2, saved_busy
        cli_mod._confirm = saved_confirm if "saved_confirm" in dir() else cli_mod._confirm
    check(raced is None and raced_env.get("warnings"),
          "a generation that starts during the download stops the measurement")

    saved_confirm = cli_mod._confirm
    cli_mod._confirm = lambda *a, **k: check(False, "--json never prompts for a download")
    cli_mod.plan_mod.runtime_image_plan = lambda root, recipe=None: {
        "images": [{"name": "comfy", "image": "x", "present": False}]}
    measure_mod.image_present = lambda image, runner=None: False
    stdout, sys.stdout = sys.stdout, StringIO()
    stderr, sys.stderr = sys.stderr, StringIO()
    try:
        json_env = {}
        cli_mod._measure_environment(
            SimpleNamespace(root=ROOT, measure_seconds=30, json=True), json_env)
    finally:
        json_stdout = sys.stdout.getvalue()
        sys.stdout, sys.stderr = stdout, stderr
        cli_mod._confirm = saved_confirm
        measure_mod.image_present = saved_present_probe
        cli_mod.plan_mod.runtime_image_plan = saved_runtime
    check(json_stdout == "" and json_env.get("warnings"),
          "--json keeps stdout clean when the image must be fetched")

    saved_image = cli_mod._measurement_image
    saved_error_busy = cli_mod._gpu_is_busy
    # Exercise the measurement exception, independently of a real GPU workload.
    cli_mod._gpu_is_busy = lambda args: False
    cli_mod._measurement_image = lambda root: "prepared@sha256:test"
    measure_mod.measure = raising
    stderr, sys.stderr = sys.stderr, StringIO()
    try:
        code = cli_mod.main(["detect", "--measure"])
    finally:
        message = sys.stderr.getvalue()
        sys.stderr = stderr
        cli_mod._measurement_image = saved_image
        cli_mod._gpu_is_busy = saved_error_busy
        measure_mod.image_present, measure_mod.measure = saved_present, saved_measure
    check(code == cli_mod.EXIT_ERROR and "docker was not found" in message,
          "a measurement failure exits with an error instead of a traceback")


def test_machine_local_timings():
    section("machine-local timings")
    from narration_video_gen import timings

    env = {
        "platform": "linux",
        "gpus": [{"name": "NVIDIA GeForce RTX 5060 Ti", "uuid": "GPU-test-5060ti",
                  "vram_mib": 16376, "vram_gib": 15.99}],
    }
    profile = {"id": "test-profile"}
    recipe = {"id": "test-recipe"}

    # Stand in for a published profile: five seconds of narration costs three
    # minutes, and each further second adds six seconds of work.
    def reference(seconds):
        return {"known": True, "minutes": 3.0 + max(0.0, seconds - 5.0) * 0.1}

    with tempfile.TemporaryDirectory() as home:
        previous_home = os.environ.get("HOME")
        previous_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["HOME"] = home
        os.environ["XDG_CONFIG_HOME"] = str(Path(home) / "config")
        try:
            check(timings.estimate(env, profile, recipe, 30.0,
                                   reference_estimate=reference) is None,
                  "a machine with no history gets no local estimate")

            timings.record(env, profile, recipe, 5.0, 600.0)
            history = timings.observations(env, profile, recipe)
            check(len(history) == 1 and history[0]["wall_clock_seconds"] == 600.0,
                  "a finished run is recorded for this machine")

            estimate = timings.estimate(env, profile, recipe, 30.0,
                                        reference_estimate=reference)
            # The reference says 30 s takes 5.5/3.0 of a 5 s run, so this
            # machine's 600 s becomes 1100 s.
            check(estimate and round(estimate["minutes"], 2) == 18.33,
                  "a short run is scaled to the full length by the published shape")
            check(estimate["extrapolated"] is True,
                  "an estimate for a different length is marked extrapolated")
            check(estimate["source"] == "local", "the estimate is labelled local")

            timings.record(env, profile, recipe, 30.0, 1000.0)
            exact = timings.estimate(env, profile, recipe, 30.0,
                                     reference_estimate=reference)
            check(exact["extrapolated"] is False,
                  "an estimate matching a measured length is not extrapolated")
            check(exact["low_minutes"] < exact["high_minutes"],
                  "two disagreeing observations produce a range")

            without_shape = timings.estimate(env, profile, recipe, 30.0,
                                             reference_estimate=None)
            check(without_shape and without_shape["samples"] == 1,
                  "without a published shape only the same-length run is used")
            check(timings.estimate(env, profile, recipe, 45.0,
                                   reference_estimate=None) is None,
                  "without a published shape no other length is invented")

            # Same recipe id, different work: the face detailer stage removed.
            timings.record(env, profile, recipe, 5.0, 600.0,
                           recipe_options=["face-detailer-off"],
                           pipeline=["s2v", "rife"])
            check(len(timings.observations(env, profile, recipe)) == 2,
                  "a run with different options is not stored under the plain workload")
            variant = timings.observations(env, profile, recipe,
                                           recipe_options=["face-detailer-off"],
                                           pipeline=["s2v", "rife"])
            check(len(variant) == 1,
                  "the variant keeps its own history")

            calibrated = {"id": "test-profile", "settings": {"blocks_to_swap": 20}}
            check(timings.observations(env, calibrated, recipe) == [],
                  "a different block-swap setting does not reuse these timings")

            two_gpus = {
                "platform": "linux",
                "gpus": [
                    {"name": "NVIDIA GeForce RTX 5060 Ti", "uuid": "GPU-test-5060ti",
                     "vram_mib": 16376, "vram_gib": 15.99},
                    {"name": "NVIDIA RTX A4000", "uuid": "GPU-test-a4000",
                     "vram_mib": 16376, "vram_gib": 15.99},
                ],
            }
            check(calibration_state.machine_identity(two_gpus) is None,
                  "calibration still refuses a machine with two GPUs")
            check(timings.machine_identity(two_gpus) is not None,
                  "timings still identify a machine with two GPUs")
            check(timings.record(two_gpus, profile, recipe, 5.0, 700.0) is not None,
                  "a two-GPU machine can record its own timings")
            check(timings.observations(two_gpus, profile, recipe) != []
                  and timings.observations(env, profile, recipe, ) != []
                  and timings.machine_identity(two_gpus)["fingerprint"]
                  != timings.machine_identity(env)["fingerprint"],
                  "adding a second GPU starts a separate timing history")

            check(timings.observations(env, profile, recipe, runtime="rebuilt") == [],
                  "a rebuilt runtime image does not inherit the old timings")

            saved_write = timings._write

            def unwritable(path, payload):
                raise OSError("read-only file system")

            timings._write = unwritable
            try:
                check(timings.record(env, profile, recipe, 5.0, 600.0) is None,
                      "an unwritable config root does not raise from record")
            finally:
                timings._write = saved_write

            other_machine = {
                "platform": "linux",
                "gpus": [{"name": "NVIDIA RTX A4000", "uuid": "GPU-test-a4000",
                          "vram_mib": 16376, "vram_gib": 15.99}],
            }
            check(timings.observations(other_machine, profile, recipe) == [],
                  "another machine does not inherit these timings")

            record_path = timings.timings_root() / (
                timings.machine_identity(env)["fingerprint"] + ".json")
            check(record_path.is_file(), "the timing record is written under the config root")
            check(stat.S_IMODE(record_path.parent.stat().st_mode) == 0o700,
                  "the timing record directory is private")
        finally:
            for name, value in (("HOME", previous_home),
                                ("XDG_CONFIG_HOME", previous_xdg)):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _write_test_wav(path, seconds, value=24):
    frames = round(48000 * seconds)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(48000)
        target.writeframes(int(value).to_bytes(2, "little", signed=True) * frames)


def test_local_tts_planning_and_assembly():
    section("local TTS planning and assembly")
    from narration_video_gen import cli as cli_mod

    web_args = cli_mod.build_parser().parse_args(["tts", "web"])
    lan_args = cli_mod.build_parser().parse_args(["tts", "web", "start", "--lan"])
    status_args = cli_mod.build_parser().parse_args(["tts", "web", "status"])
    stop_args = cli_mod.build_parser().parse_args(["tts", "web", "stop"])
    password_status_args = cli_mod.build_parser().parse_args(
        ["tts", "web", "password-status"])
    check(web_args.host is None and not web_args.lan
          and web_args.web_action == "start" and lan_args.lan
          and status_args.web_action == "status" and stop_args.web_action == "stop"
          and password_status_args.web_action == "password-status",
          "the review UI defaults to a background localhost service with explicit LAN access")
    check(not cli_mod._tts_web_auth_required("127.0.0.1")
          and not cli_mod._tts_web_auth_required("127.8.9.10")
          and not cli_mod._tts_web_auth_required("::1")
          and cli_mod._tts_web_auth_required("0.0.0.0")
          and cli_mod._tts_web_auth_required("::")
          and cli_mod._tts_web_auth_required("192.168.1.20")
          and cli_mod._tts_web_auth_required("narration.internal"),
          "every non-loopback listen address requires TTS authentication")
    cli_source = (ROOT / "src/narration_video_gen/cli.py").read_text(encoding="utf-8")
    check("auth_required=auth_required or args.auth_required" in cli_source,
          "the internal serving path cannot weaken network authentication")
    backend_script = (ROOT / "scripts/tts-backend.sh").read_text(encoding="utf-8")
    check("-p 127.0.0.1:8088:8088" in backend_script,
          "the model API remains loopback-only when the review UI is on the LAN")

    with tempfile.TemporaryDirectory(prefix="nvg-tts-web-service-") as directory:
        service_root = Path(directory)
        shutil.copytree(ROOT / "src", service_root / "src")
        (service_root / "bin").mkdir()
        shutil.copy2(ROOT / "bin" / "narration-video-gen",
                     service_root / "bin" / "narration-video-gen")
        port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port_socket.bind(("127.0.0.1", 0))
        service_port = port_socket.getsockname()[1]
        port_socket.close()
        first = None
        second = None
        try:
            first = cli_mod._start_tts_web(
                service_root, "127.0.0.1", service_port,
                tts_service.DEFAULT_SERVER)
            first_status = cli_mod._tts_web_status(service_root)
            second = cli_mod._start_tts_web(
                service_root, "0.0.0.0", service_port,
                tts_service.DEFAULT_SERVER)
            second_status = cli_mod._tts_web_status(service_root)
            state_mode = stat.S_IMODE(
                cli_mod._tts_web_state_path(service_root).stat().st_mode)
            check(first_status["running"] and second_status["running"]
                  and first["pid"] != second["pid"]
                  and second["host"] == "0.0.0.0" and state_mode == 0o600,
                  "the managed web service starts detached and safely restarts for LAN access")
        finally:
            stopped = cli_mod._stop_tts_web(service_root)
        check(stopped and not cli_mod._tts_web_status(service_root)["running"],
              "the managed web service stops and removes its state")

        narration.write_json_atomic(cli_mod._tts_web_state_path(service_root), {
            "pid": os.getpid(), "token": "not-on-this-command-line",
            "host": "127.0.0.1", "port": service_port,
        })
        check(not cli_mod._stop_tts_web(service_root) and os.getpid() > 0,
              "a stale PID without the private token is never signalled")

    script = ("短い導入です。これは短文を単独生成しないための続きです。\n\n"
              "ここから話題が変わります。最後まで落ち着いて確認しましょう。")
    parts = narration.split_script(script)
    check(len(parts) == 2, "short related sentences are merged within paragraphs")
    check(parts[0].paragraph_end and parts[0].gap_after_ms == 820,
          "a paragraph boundary receives the longest default pause")
    check(parts[-1].gap_after_ms == 0,
          "the final part has no following pause")
    check("。" in parts[0].text and "。" in parts[1].text,
          "script punctuation is preserved")
    explicit = narration.split_script("短い挨拶です。\nここから本題です。\n最後の文です。")
    check(len(explicit) == 3 and explicit[0].text == "短い挨拶です。",
          "one non-empty script line is preserved as an editable TTS part")

    def _pcm(blocks):
        """Build 48 kHz PCM from (seconds, amplitude) blocks."""
        payload = bytearray()
        for seconds, amplitude in blocks:
            for index in range(round(48000 * seconds)):
                value = amplitude if (index // 60) % 2 == 0 else -amplitude
                payload += int(value).to_bytes(2, "little", signed=True)
        return bytes(payload)

    # The v3 extra-utterance shape: a short blob after a long silence. A single
    # window dipping below the speech threshold inside that blob used to split
    # it, so the rule saw a 0.01 s blob after a 0.01 s gap and kept the extra
    # speech in a measured take.
    body = [(1.20, 6000), (0.50, 0), (0.20, 6000), (0.01, 100), (0.04, 6000),
            (0.10, 0)]
    cleaned = narration._clean_pcm(_pcm(body))
    check(1.20 < len(cleaned) / 96000 < 1.45,
          "a dip inside a detached tail blob does not hide the extra speech")
    # A sentence that merely opens with a phrase and a pause is left alone:
    # measured parts do that routinely, so the same rule is not applied there.
    opening = _pcm([(0.55, 6000), (0.70, 0), (1.30, 6000), (0.10, 0)])
    check(len(narration._clean_pcm(opening)) / 96000 > 2.4,
          "a pause after an opening phrase is not treated as extra speech")

    exact = narration.classify_transcript("こんにちは、葵です。", ["こんにちは、葵です。"])
    extra = narration.classify_transcript(
        "こんにちは、葵です。余計な言葉", ["こんにちは、葵です。"])
    check(exact["status"] == "pass", "exact ASR text passes after normalization")
    check(extra["status"] == "fail_extra_speech" and extra["extra_normalized"],
          "ASR prefix plus extra words is flagged as extra speech")

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        first, second = temporary / "one.wav", temporary / "two.wav"
        output = temporary / "joined.wav"
        _write_test_wav(first, 0.5)
        _write_test_wav(second, 0.75)
        info = narration.join_wav_parts(
            [first, second], output, gaps_ms=[500], outro_ms=1000)
        check(output.is_file() and abs(info["duration_seconds"] - 2.75) < 0.001,
              "WAV assembly preserves parts and inserts gap plus outro")
        check(not info["issues"], "assembled WAV satisfies the video audio contract")

        try:
            tts_service._local_server("https://example.com")
        except tts_service.TTSError:
            rejected = True
        else:
            rejected = False
        check(rejected, "TTS refuses a remote backend address")
        check(tts_service._local_server("127.0.0.1:8088")
              == "http://127.0.0.1:8088",
              "a loopback backend address is accepted")

        root = temporary / "repo"
        image = root / "assets/characters/aoi/images/aoi-portrait-angled-01.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"image")
        job = root / "outputs/tts/test-job"
        (job / "parts").mkdir(parents=True)
        _write_test_wav(job / "narration.wav", 1.0)
        manifest = {
            "job_id": "test-job", "status": "review", "character": "aoi",
            "script": "テストです。", "human_review": "pending",
            "output": {"audio": "narration.wav", "sha256": "test"},
        }
        narration.write_json_atomic(job / "manifest.json", manifest)
        try:
            tts_service.adopt_as_input_set(root, "test-job")
        except tts_service.TTSError:
            rejected = True
        else:
            rejected = False
        check(rejected, "a narration cannot be adopted before human listening")
        tts_service.confirm_human_review(root, "test-job")
        adopted = tts_service.adopt_as_input_set(root, "test-job")
        check((adopted / "audio.wav").is_file()
              and (adopted / "image.png").is_file()
              and (adopted / "script.txt").read_text(encoding="utf-8") == "テストです。",
              "an explicitly reviewed narration becomes a complete video input set")

    lock = load_yaml_file(ROOT / "manifests/tts-models.lock.yaml")
    locked = {item["id"]: item for item in lock["models"]}
    check(locked["irodori-tts-v4.1-small"]["revision"]
          == tts_service.MODEL_REVISION
          and locked["semantic-dacvae-japanese-32dim"]["revision"]
          == tts_service.CODEC_REVISION,
          "TTS service and model lock use the same fixed revisions")
    check(all(len(item["sha256"]) == 64 and item["bytes"] > 0
              for item in lock["models"]),
          "every TTS and ASR weight has a byte size and SHA-256")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = tts_service.record_models_prepared(root)
        check(tts_service.models_prepared(root)
              and tts_service.model_state_path(root).is_file()
              and "prepared_at" in state and "accepted_at" not in state,
              "TTS model state records preparation without a licence-consent gate")
        tts_service.model_state_path(root).unlink()
        legacy = root / "outputs/tts/.model-acceptance.json"
        narration.write_json_atomic(legacy, {
            "model": tts_service.MODEL_ID,
            "revision": tts_service.MODEL_REVISION,
        })
        check(tts_service.models_prepared(root),
              "existing TTS preparations remain valid after the state rename")
    dockerfile = (ROOT / "docker/tts.Dockerfile").read_text(encoding="utf-8")
    check("python:3.10-slim@sha256:" in dockerfile
          and "ghcr.io/astral-sh/uv:0.8.15@sha256:" in dockerfile,
          "TTS container base and uv helper are digest-pinned")

    result = subprocess.run(
        [str(ROOT / "bin/narration-video-gen"), "--json", "tts", "plan",
         "--text", "短い文です。続きの短い文です。"],
        cwd=ROOT, text=True, capture_output=True)
    check(result.returncode == 0 and json.loads(result.stdout)["parts"],
          "TTS script planning has machine-readable CLI output")

    requests = []
    audio_buffer = BytesIO()
    with wave.open(audio_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\x18\x00" * 24000)
    audio_bytes = audio_buffer.getvalue()

    class FakeIrodori(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append(request)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("x-irodori-seed", str(request["irodori"]["seed"]))
            self.end_headers()
            self.wfile.write(audio_bytes)

    server = HTTPServer(("127.0.0.1", 0), FakeIrodori)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tts_service.record_models_prepared(root)
            manifest, job_dir = tts_service.generate_narration(
                root,
                "これは最初の説明文です。内容が短すぎないように続けます。\n\n"
                "ここから次の説明です。最後まで確認して終わります。",
                "aoi", server="http://127.0.0.1:%d" % server.server_port,
                run_asr=False)
            check(manifest["status"] == "review"
                  and (job_dir / "narration.wav").is_file()
                  and all((job_dir / item["audio"]).is_file()
                          for item in manifest["parts"]),
                  "the local API client retains parts, manifest, and joined WAV")
            check(len(requests) == len(manifest["parts"])
                  and all(item["irodori"]["chunking_enabled"] is False
                          and item["irodori"]["seed"] == 1315520242
                          and item["voice"] == "aoi" for item in requests),
                  "each part fixes voice and seed and disables upstream auto-chunking")
            changed = tts_service.regenerate_part(
                root, manifest["job_id"], 1,
                server="http://127.0.0.1:%d" % server.server_port,
                seed=1315520243, run_asr=False)
            check(requests[-1]["irodori"]["seed"] == 1315520243
                  and changed["parts"][0]["seed"] == 1315520243
                  and changed["parts"][0]["initial_seed"] == 1315520242,
                  "one TTS part can change seed while retaining its initial seed")
            restored = tts_service.regenerate_part(
                root, manifest["job_id"], 1,
                server="http://127.0.0.1:%d" % server.server_port,
                seed=changed["parts"][0]["initial_seed"], run_asr=False)
            check(restored["parts"][0]["seed"] == 1315520242,
                  "one TTS part can return to its initial seed")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_local_tts_web_ui():
    """The beginner review page must stay self-contained and job-scoped."""
    section("local TTS review UI")
    page = tts_web._HTML

    for element in ("id=\"script\"", "id=\"generate\"", "id=\"character\"",
                    "id=\"listened\"", "id=\"adopt\"", "id=\"result\""):
        check(element in page, "the review page keeps %s" % element)
    check("prefers-color-scheme:dark" in page,
          "the review page follows the viewer's light or dark theme")
    check("width=device-width" in page,
          "the review page is usable from a phone on the same LAN")
    check("alert(" not in page and "prompt(" not in page,
          "errors and adjustments stay in the page instead of browser dialogs")
    check("const attempts=body?1:3" in page
          and "一時的に接続できませんでした" in page,
          "read-only Web UI requests retry a transient local connection failure")
    check("__CSRF__" in page and "X-NVG-CSRF" in page,
          "every write from the page carries the per-start request token")
    check('name="password"' in tts_web._LOGIN_HTML
          and 'name="username"' not in tts_web._LOGIN_HTML
          and '<strong>tts</strong>' in tts_web._LOGIN_HTML,
          "LAN login fixes the username to tts and only asks for a password")
    check("ensureReady" in page,
          "one button generates audio, preparing the model and engine if needed")
    check("最初のseedに戻す" in page and "この長さで作り直す" in page,
          "a single part can return to its first seed or change length in place")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        job = root / "outputs/tts/20260101-000000-abcdef"
        (job / "parts").mkdir(parents=True)
        _write_test_wav(job / "narration.wav", 0.5)
        narration.write_json_atomic(job / "manifest.json", {
            "job_id": "20260101-000000-abcdef", "status": "review",
            "character": "aoi", "script": "テストです。", "human_review": "pending",
            "parts": [{"index": 1, "text": "テストです。", "audio": "narration.wav"}],
        })
        (root / "outputs/tts/broken").mkdir(parents=True)
        listed = tts_web.recent_jobs(root)
        check(len(listed) == 1 and listed[0]["job_id"] == "20260101-000000-abcdef"
              and "葵" in listed[0]["label"],
              "a closed tab can reopen a job, and an unreadable directory is skipped")

        app = tts_web.NarrationWebApp(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_port

        def fetch(path, headers=None, body=None):
            request = urllib.request.Request(
                base + path, data=body, headers=headers or {})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, response.read(), dict(response.headers)
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read(), dict(exc.headers)

        try:
            status, _body, _headers = fetch(
                "/api/segment",
                headers={"Content-Type": "application/json"},
                body=b'{"script": "\u30c6\u30b9\u30c8\u3067\u3059\u3002"}')
            check(status == 403, "a write without the request token is refused")

            audio = "/media/20260101-000000-abcdef/narration.wav"
            status, body, headers = fetch(audio)
            check(status == 200 and body.startswith(b"RIFF")
                  and headers.get("Accept-Ranges") == "bytes",
                  "job audio is served with range support for the seek bar")
            total = len(body)
            status, body, headers = fetch(audio, headers={"Range": "bytes=10-59"})
            check(status == 206 and len(body) == 50
                  and headers["Content-Range"] == "bytes 10-59/%d" % total,
                  "a partial request returns exactly the requested bytes")
            for escape in ("/media/20260101-000000-abcdef/../../../../etc/passwd.wav",
                           "/media/../../../etc/passwd.wav",
                           "/media/20260101-000000-abcdef/manifest.json"):
                status, _body, _headers = fetch(escape)
                check(status == 400, "media refuses %s" % escape)
        finally:
            server.shutdown()
            server.server_close()

        try:
            tts_web.set_password(root, "123456789")
            short_password_rejected = False
        except ValueError:
            short_password_rejected = True
        tts_web.set_password(root, "1234567890")
        check(short_password_rejected
              and tts_web.password_matches(root, "tts", "1234567890"),
              "the LAN password accepts 10 characters but rejects 9")

        auth_app = tts_web.NarrationWebApp(root, auth_required=True)
        old_record = tts_web.password_configured(root)
        tts_web.set_password(root, "abcdefghij")
        new_record = tts_web.password_configured(root)
        with patch.object(tts_web, "password_configured",
                          side_effect=(old_record, new_record)):
            raced_token, _throttled = auth_app.login("1234567890", "race-test")
        check(raced_token is None,
              "a password reset racing a login cannot authorize the old password")
        tts_web.set_password(root, "1234567890")
        auth_server = ThreadingHTTPServer(("127.0.0.1", 0), auth_app.handler())
        threading.Thread(target=auth_server.serve_forever, daemon=True).start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", auth_server.server_port, timeout=10)
        try:
            connection.request("GET", "/api/jobs")
            response = connection.getresponse()
            response.read()
            unauthenticated = response.status

            body = urllib.parse.urlencode({"password": "wrong-password"})
            connection.request("POST", "/login", body, {
                "Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            response.read()
            wrong_password = response.status

            body = urllib.parse.urlencode({"password": "1234567890"})
            connection.request("POST", "/login", body, {
                "Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            response.read()
            cookie = response.getheader("Set-Cookie", "").split(";", 1)[0]
            logged_in = response.status == 303 and cookie.startswith(
                tts_web.AUTH_COOKIE + "=")

            connection.request("GET", "/api/jobs", headers={"Cookie": cookie})
            response = connection.getresponse()
            response.read()
            authenticated = response.status

            tts_web.set_password(root, "abcdefghij")
            connection.request("GET", "/api/jobs", headers={"Cookie": cookie})
            response = connection.getresponse()
            response.read()
            invalidated = response.status
        finally:
            connection.close()
            auth_server.shutdown()
            auth_server.server_close()
        check(unauthenticated == 401 and wrong_password == 401
              and logged_in and authenticated == 200 and invalidated == 401,
              "LAN data needs a password session and password resets invalidate it")


def _fake_irodori_server(audio_bytes, requests):
    class FakeIrodori(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append(request)
            time.sleep(0.15)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.send_header("x-irodori-seed", str(request["irodori"]["seed"]))
            self.end_headers()
            self.wfile.write(audio_bytes)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIrodori)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_local_tts_robustness():
    """Splitting, transcript grading and concurrent job writes must hold."""
    section("local TTS robustness")

    def texts(value):
        return [item.text for item in narration.split_script(value)]

    url = "詳しくは https://example.com/docs?lang=ja を見てください。ありがとうございました。"
    check(texts(url) == [url], "a URL's query mark does not split a sentence")
    long_url = ("今回の詳細な手順と補足資料、そして参考にした一次情報のすべては、次のページに"
                "まとめて掲載していますので、"
                "https://example.com/very/long/path?lang=ja&mode=full "
                "こちらをご確認ください。")
    check(all("https://example.com/very/long/path?lang=ja&mode=full" in item
              for item in texts(long_url)
              if "https://" in item),
          "an over-long line never cuts a URL in half")
    check(texts("連絡は support@example.com へどうぞ。よろしくお願いします。")
          == ["連絡は support@example.com へどうぞ。よろしくお願いします。"],
          "a mail address is not treated as sentence punctuation")
    mixed = "Wow! That is great. 日本語もまざる文章のテストです。もう少し続けます。"
    check("".join(texts(mixed)) == mixed,
          "merging sentences keeps every character, including spaces")

    orthography = narration.classify_transcript(
        "この動画、実は全部AIで作りました。", ["この動画、実はぜんぶAIで作りました。"])
    check(orthography["status"] == "review_asr_mismatch",
          "a homophone spelling is a review flag, not a failure")
    dropped = narration.classify_transcript(
        "こんにちは。", ["こんにちは、葵です。今日はよろしくお願いします。"])
    check(dropped["status"] == "fail_missing_speech",
          "a transcript far shorter than the script fails as dropped speech")
    trailing = narration.classify_transcript(
        "こんにちは、葵です。ところで今日はとてもいい天気ですね、そう思いませんか",
        ["こんにちは、葵です。"])
    check(trailing["status"] == "fail_extra_speech",
          "the whole script plus more speech still fails as extra speech")
    unrelated = narration.classify_transcript(
        "まったく違う内容の長い文章になっています", ["こんにちは、葵です。"])
    check(unrelated["status"] == "fail_mismatch",
          "a different sentence is still a mismatch failure")
    try:
        narration.classify_transcript("なにか", [])
        rejected = False
    except ValueError:
        rejected = True
    check(rejected, "grading without an expected transcript is refused")

    natural = tts_service.natural_seconds(8)
    good = tts_service._part_quality("こんにちは、葵です。", {
        "duration_seconds": 3.12, "cleaned_duration_seconds": 1.76, "issues": []})
    extra = tts_service._part_quality("こんにちは、葵です。", {
        "duration_seconds": 3.12, "cleaned_duration_seconds": 3.12, "issues": []})
    tiny = tts_service._part_quality("はい。", {
        "duration_seconds": 0.7, "cleaned_duration_seconds": 0.7, "issues": []})
    kanji_floor = tts_service._part_quality("人工知能技術検証結果公開音声品質評価項目", {
        "duration_seconds": 5.0, "cleaned_duration_seconds": 5.0, "issues": []})
    check(abs(natural - 1.46) < 1e-9, "natural length uses the measured v3 fit")
    check(good["status"] == "pass" and not good["issues"],
          "a measured good short take is not flagged")
    check(any("extra speech" in issue for issue in extra["issues"]),
          "the v3 short-text extra-utterance shape is still flagged")
    check(tiny["status"] == "pass",
          "a one-word part is no longer flagged by a flat per-character limit")
    check(kanji_floor["status"] == "pass",
          "the hybrid duration limit does not become stricter on kanji-heavy text")
    check("p.asr.status!=='pass'&&p.asr.status!=='not_run'" in tts_web._HTML,
          "every ASR review or failure contributes to the page warning count")

    audio_buffer = BytesIO()
    with wave.open(audio_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\x18\x00" * 24000)
    audio_bytes = audio_buffer.getvalue()
    requests = []
    server = _fake_irodori_server(audio_bytes, requests)
    backend = "http://127.0.0.1:%d" % server.server_port
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tts_service.record_models_prepared(root)
            manifest, job_dir = tts_service.generate_narration(
                root,
                "これは最初の説明文です。内容が短すぎないように続けます。\n"
                "ここから次の説明です。最後まで確認して終わります。",
                "aoi", server=backend, run_asr=False)
            job_id = manifest["job_id"]

            results = {}

            def regenerate(index, seed):
                try:
                    results[index] = tts_service.regenerate_part(
                        root, job_id, index, server=backend, seed=seed,
                        run_asr=False)
                except Exception as exc:  # noqa: BLE001 - reported by the check
                    results[index] = exc

            workers = [threading.Thread(target=regenerate, args=(1, 111)),
                       threading.Thread(target=regenerate, args=(2, 222))]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(120)
            final = tts_service.load_job(root, job_id)
            check(all(not isinstance(item, Exception) for item in results.values()),
                  "concurrent part regeneration does not raise")
            check(final["parts"][0]["seed"] == 111 and final["parts"][1]["seed"] == 222,
                  "concurrent part regeneration keeps both seeds in the manifest")
            check((job_dir / "narration.wav").is_file()
                  and final["output"]["sha256"] == tts_service._sha256(
                      job_dir / "narration.wav"),
                  "the joined WAV still matches the hash recorded for it")

            (job_dir / final["parts"][0]["audio"]).unlink()
            for call, label in (
                    (lambda: tts_service.rejoin_job(root, job_id, [500]),
                     "rejoin"),
                    (lambda: tts_service.regenerate_part(
                        root, job_id, 2, server=backend, run_asr=False),
                     "regenerate")):
                try:
                    call()
                    reported = "no error"
                except tts_service.TTSError as exc:
                    reported = str(exc)
                except Exception as exc:  # noqa: BLE001 - the bug being fixed
                    reported = "%s: %s" % (type(exc).__name__, exc)
                check("audio is missing" in reported,
                      "%s reports a missing part instead of crashing" % label)
    finally:
        server.shutdown()
        server.server_close()

    check(tts_web.host_allowed("192.168.1.50:7861")
          and tts_web.host_allowed("127.0.0.1:7861")
          and not tts_web.host_allowed("rebind.example.com:7861"),
          "the review page answers addresses and its own name, not a foreign name")
    check(tts_web.host_allowed("narration.internal", ("narration.internal",)),
          "an operator can allow an extra name explicitly")

    tasks = tts_web.TaskStore()
    tasks.values = {
        str(index): {"status": "running"} for index in range(tts_web.MAX_TASKS)}
    try:
        tasks.start("overflow", lambda _update: None)
        bounded = False
    except tts_service.TTSError:
        bounded = True
    check(bounded and len(tasks.values) == tts_web.MAX_TASKS,
          "the web task store enforces its hard upper bound")

    from narration_video_gen import cli as cli_mod
    generate_args = cli_mod.build_parser().parse_args(
        ["tts", "generate", "--text", "テストです。"])
    check(generate_args.ui_host == "127.0.0.1",
          "a generate command starts the review page on loopback only")


def test_local_tts_user_characters():
    """A locally-made character must reach the video input set intact."""
    section("local TTS user characters")
    page = tts_web._HTML
    check("キャラクターを作る" in page, "the page offers character creation")
    check("maker={mode:'design'" in page and 'data-mode="design"' in page
          and 'data-mode="inherit"' in page,
          "making a new voice is the default and inheriting is an option")
    check("16:9" in page,
          "the portrait step states the crop the video stage applies")
    check('id="m-consent"' in page,
          "bringing your own recording asks for an explicit confirmation")
    check("声を文章で作る" in page and "音声を指定する" in page,
          "a voice can be described in words as well as supplied as a file")
    check('id="m-seed"' in page and "別の声にする" in page,
          "a described voice can be rerolled into a different person")
    check('id="a-delete"' in page and 'id="a-voice"' in page
          and 'id="a-image"' in page,
          "a character made here can be redesigned, repictured or deleted later")
    check('id="delete-job"' in page and "/delete'" in page,
          "a completed narration can be deleted before its character")
    check("このキャラクターにする" in page and "/commit'" in page,
          "an auditioned character is committed only after the user keeps it")
    check("rerollMade" in page and "createCharacterAgain" not in page,
          "rejecting a take redraws the voice instead of remaking the character")

    check(tts_service.REFERENCE_MINIMUM_SECONDS <= 2.0
          and tts_service.REFERENCE_MAXIMUM_SECONDS == 120.0,
          "reference length limits follow the checkpoint, not a legacy fallback")
    passage_seconds = tts_service.natural_seconds(
        narration._visible_length(tts_service.VOICE_DESIGN_PASSAGE))
    check(4.0 < passage_seconds < 20.0,
          "the voice-design passage stays short enough to generate in one go")

    # A described voice can be drawn narrowband, and narration copies the
    # reference's spectrum, so a dull reference stays dull for every part.
    check(not tts_service.bandwidth_advice({"cutoff_hz": 13242.2})
          and not tts_service.bandwidth_advice({"cutoff_hz": 17273.4}),
          "a reference as wide as the bundled voices draws no complaint")
    dull = tts_service.bandwidth_advice({"cutoff_hz": 8191.4})
    check(len(dull) == 1 and "8.2kHz" in dull[0] and "別の声にする" in dull[0],
          "a narrowband reference says so and points at drawing another voice")
    check(tts_service.bandwidth_advice(None) == []
          and tts_service.bandwidth_advice({"cutoff_hz": 0.0}) == [],
          "an unavailable measurement never blocks making a character")
    check(tts_service.measure_reference_bandwidth(ROOT, "/etc/passwd") is None,
          "the bandwidth check refuses a path outside the repository")
    checker = (ROOT / "scripts/tts-audio-check.sh").read_text(encoding="utf-8")
    check("TORCHINDUCTOR_CACHE_DIR" in checker and "-e USER=" in checker,
          "the bandwidth check runs as the calling user without a home")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, portrait in tts_service.BUNDLED_PORTRAITS.items():
            images = root / "assets/characters" / name / "images"
            images.mkdir(parents=True)
            (images / portrait).write_bytes(b"png")
            audio = root / "assets/characters" / name / "audio"
            audio.mkdir(parents=True)
        _write_test_wav(
            root / "assets/characters/aoi/audio/aoi-narration-take3.wav", 26.0)
        _write_test_wav(
            root / "assets/characters/sakura/audio/sakura-narration-blog.wav", 26.0)
        (root / "config").mkdir()
        shutil.copy2(ROOT / "config/tts-voices.json", root / "config/tts-voices.json")

        made = tts_service.create_user_character(
            root, "ギャル葵", voice_from="aoi",
            caption="明るく軽やかに、語尾をのばして話す。")
        characters = tts_service.list_characters(root)
        derived = characters[made["id"]]
        check(derived["reference"] == characters["aoi"]["reference"]
              and derived["portrait"] == characters["aoi"]["portrait"]
              and derived["seed"] == characters["aoi"]["seed"],
              "an inherited character reuses the voice, portrait and seed")
        check(derived["caption"] != characters["aoi"]["caption"]
              and derived["derived_from"] == "aoi",
              "an inherited character keeps its own speaking style and origin")

        record = json.loads(
            (tts_service.character_root(root, made["id"]) / "character.json")
            .read_text(encoding="utf-8"))
        check("consent" not in record,
              "inheriting a cleared bundled voice asks for no consent record")
        for section_name in ("reference", "portrait"):
            check((root / record[section_name]["path"]).is_file(),
                  "the recorded %s path resolves after the character is staged"
                  % section_name)

        aliases = json.loads(
            (root / "outputs/tts/.voices/aliases.json").read_text(encoding="utf-8"))
        check(aliases[made["id"]]["ref_wav"] == aliases["aoi"]["ref_wav"],
              "the generated alias file points the new voice at its reference")
        runtime = root / "outputs/tts/.voices"
        check((runtime / "characters").is_dir() and (runtime / "user").is_dir(),
              "the runtime voices directory carries both container mount points")

        portrait = root / "new-portrait.jpg"
        portrait.write_bytes(b"jpeg")
        tts_service.replace_character_portrait(root, made["id"], portrait)
        characters = tts_service.list_characters(root)
        check(characters[made["id"]]["portrait"] != characters["aoi"]["portrait"]
              and (root / characters["aoi"]["portrait"]).is_file(),
              "replacing an inherited portrait leaves the bundled asset alone")

        try:
            tts_service.create_user_character(
                root, "太郎", portrait_path=portrait, audio_path=portrait)
            refused = "no error"
        except tts_service.TTSError as exc:
            refused = str(exc)
        check("チェック" in refused,
              "an uploaded voice without the confirmation is refused")
        try:
            tts_service.delete_user_character(root, "aoi")
            refused = "no error"
        except tts_service.TTSError as exc:
            refused = str(exc)
        check("同梱" in refused, "a bundled character cannot be deleted")

        try:
            tts_service.redesign_character_voice(root, made["id"])
            refused = "no error"
        except tts_service.TTSError as exc:
            refused = str(exc)
        check("音声ファイルから作られています" in refused,
              "a voice copied from a recording is not redesigned in place")

        job = root / "outputs/tts/20260101-000000-aaaaaa"
        job.mkdir(parents=True)
        _write_test_wav(job / "narration.wav", 1.0)
        narration.write_json_atomic(job / "manifest.json", {
            "job_id": "20260101-000000-aaaaaa", "status": "review",
            "character": made["id"], "script": "テストです。",
            "human_review": "passed",
            "output": {"audio": "narration.wav", "sha256": "test"},
        })
        try:
            tts_service.delete_user_character(root, made["id"])
            refused = "no error"
        except tts_service.TTSError as exc:
            refused = str(exc)
        check("削除できません" in refused,
              "a character a narration still cites cannot be deleted")

        # A described voice is the only kind that can be redesigned in place,
        # so build one without reaching for the container's converter.
        reference = root / "designed-source.wav"
        _write_test_wav(reference, 16.0, value=4096)
        convert = tts_service.convert_to_reference_wav
        tts_service.convert_to_reference_wav = (
            lambda _root, source, target: (
                Path(target).parent.mkdir(parents=True, exist_ok=True),
                shutil.copy2(source, target), Path(target))[-1])
        try:
            designed = tts_service.create_user_character(
                root, "設計した声", portrait_path=portrait,
                audio_path=reference,
                designed_from={"caption": "落ち着いた声。", "seed": 4242})
        finally:
            tts_service.convert_to_reference_wav = convert
        record = json.loads(
            (tts_service.character_root(root, designed["id"]) / "character.json")
            .read_text(encoding="utf-8"))
        check("consent" not in record
              and record["reference"]["designed_from"]["seed"] == 4242,
              "a described voice records its description instead of a consent tick")

        narration.write_json_atomic(job / "manifest.json", {
            "job_id": "20260101-000000-aaaaaa", "status": "review",
            "character": designed["id"], "script": "テストです。",
            "human_review": "passed",
            "output": {"audio": "narration.wav", "sha256": "test"},
        })
        try:
            tts_service.redesign_character_voice(root, designed["id"])
            refused = "no error"
        except tts_service.TTSError as exc:
            refused = str(exc)
        check("新しいキャラクターを作ってください" in refused,
              "a character a narration cites keeps the voice that made it")

        adopted = tts_service.adopt_as_input_set(root, "20260101-000000-aaaaaa")
        images = sorted(path.name for path in adopted.iterdir()
                        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
        check(images == ["image.jpg"] and (adopted / "audio.wav").is_file(),
              "adopting a user character yields one image in its real format")
        check(json.loads((adopted / "tts-manifest.json").read_text(
            encoding="utf-8"))["voice_source"] == "user",
            "the adopted input set records that a user voice made it")

        shutil.rmtree(job)
        tts_service.delete_user_character(root, made["id"])
        check(not tts_service.character_root(root, made["id"]).exists()
              and (root / "assets/characters/aoi/images"
                   / tts_service.BUNDLED_PORTRAITS["aoi"]).is_file(),
              "deleting a character removes only what it owned")

    backend_script = (ROOT / "scripts/tts-backend.sh").read_text(encoding="utf-8")
    check('-v "$USER_VOICES:/voices/user:ro"' in backend_script
          and '-v "$RUNTIME_VOICES:/voices:ro"' in backend_script,
          "user voices reach the engine as a directory, not a single-file mount")
    check("--network none" in backend_script and "--user" in backend_script,
          "audio conversion runs offline and never writes root-owned files")
    # The SilentCipher digest hashes a sorted file list, and en_US.UTF-8
    # collates it differently from C. Without a fixed collation the integrity
    # check fails on intact weights and tells the operator to re-download.
    check(re.search(r"find -L \. -type f -print0 \| LC_ALL=C sort -z",
                    backend_script),
          "the pinned model digest sorts in a fixed collation, not the caller's")


def main():
    test_ui_language_fixture_restores_environment()
    test_bundled_yaml_parser_matches_pyyaml()
    catalog = test_catalog_loads()
    test_timing_reference_drives_runtime_estimate(catalog)
    test_external_profile_overlay(catalog)
    test_composed_stage_runtime_estimate(catalog)
    test_every_recipe_model_is_locked(catalog)
    test_lock_entries_are_complete()
    test_evidence_is_never_stronger_than_it_should_be(catalog)
    test_selection_prefers_the_right_profile(catalog)
    test_interactive_selection_and_temporary_state(catalog)
    test_frame_rounding()
    test_cli_inputs_are_container_addressable()
    test_wsl2_allocator_workaround_defaults_on_with_opt_out()
    test_container_wrappers_reconstruct_compose_environment()
    test_linux_setup_is_safe_and_pinned()
    test_windows_setup_is_guided_and_safe()
    test_passwordless_sudo_helper_is_guarded()
    test_stage_workflows_are_well_formed(catalog)
    test_rife_only_on_a_wan21_recipe(catalog)
    test_cli_json_is_parseable()
    test_guided_plan_and_run_helpers()
    test_retime_audio_codec_selection()
    test_background_run_state_and_errors()
    test_public_name_is_consistent()
    test_failure_remedies_name_the_next_setting(catalog)
    test_machine_local_calibration_state(catalog)
    test_generated_matrix_is_current()
    test_measured_capability()
    test_machine_local_timings()
    test_local_tts_planning_and_assembly()
    test_local_tts_web_ui()
    test_local_tts_robustness()
    test_local_tts_user_characters()

    print("\n%d failure(s)" % len(FAILURES))
    for failure in FAILURES:
        print("  - %s" % failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
