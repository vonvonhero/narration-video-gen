"""Offline tests for the local block-swap calibration harness.

Run with: python3 tests/test_calibration.py
No GPU, model download, Docker build, or network access is required.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "calibration_harness", ROOT / "calibration" / "calibrate.py")
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


GIB = 1024 ** 3
FAKE_GPU = {
    "name": "NVIDIA Test GPU",
    "uuid": "GPU-test",
    "driver_version": "999.0",
    "memory.total": "24576",
}


class FakeMonitor:
    def __init__(self, *unused, **ignored):
        pass

    def start(self):
        pass

    def close(self):
        pass

    def set_attempt(self, _attempt_id):
        pass

    def set_comfy_process(self, _process):
        pass

    def summary(self, _attempt_id=None):
        return {}


def gpu_args(workspace, run_id, scenario="wan21-480p"):
    return SimpleNamespace(
        root=ROOT,
        workspace=workspace,
        run_id=run_id,
        scenario=[scenario],
        telemetry_seconds=1,
        comfy_timeout_seconds=1,
        attempt_timeout_seconds=1,
        allow_unknown_image=True,
        search_mode="fast",
        dry_run=False,
    )


def synthetic_attempt(threshold=None, failure="gpu-oom", elapsed_seconds=1.0,
                      forced_oom_blocks=(), telemetry_peak_mib=None):
    def run(_root, _run_dir, _scenario_set, _scenario, profile_entry,
            attempt_id, length, replicate, phase, reason, _monitor, _args):
        blocks = profile_entry["blocks_to_swap"]
        passed = (threshold is not None and blocks >= threshold
                  and blocks not in set(forced_oom_blocks))
        elapsed = (elapsed_seconds(blocks, passed)
                   if callable(elapsed_seconds) else elapsed_seconds)
        return {
            "attempt_id": attempt_id,
            "scenario": _scenario["id"],
            "profile": profile_entry["id"],
            "profile_sha256": profile_entry["sha256"],
            "blocks_to_swap": blocks,
            "length": length,
            "replicate": replicate,
            "phase": phase,
            "selection_reason": reason,
            "status": "passed" if passed else "failed",
            "failure_category": None if passed else failure,
            "elapsed_seconds": elapsed,
            "telemetry_summary": ({
                "gpu_max_memory_used_mib": {"GPU-test": telemetry_peak_mib},
            } if telemetry_peak_mib is not None else {}),
            "host_before": {},
        }
    return run


def model_preflight_patch():
    """Bypass physical model and disk checks in synthetic calibration tests."""
    return mock.patch.multiple(
        BENCHMARK, verify_selected_models=mock.DEFAULT,
        verify_runtime_disk=mock.DEFAULT)


class CalibrationHarnessTests(unittest.TestCase):
    def test_empty_comfy_output_marker_is_safe_to_remove(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            marker = output / "_output_images_will_be_put_here"
            marker.touch()
            BENCHMARK.remove_bundled_output_marker(output)
            self.assertFalse(marker.exists())

            marker.write_text("unexpected", encoding="utf-8")
            BENCHMARK.remove_bundled_output_marker(output)
            self.assertTrue(marker.exists())

    def test_benchmark_attempt_bypasses_host_cli_checks_after_its_own_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            profile_dir = run_dir / "profiles"
            profile_dir.mkdir()
            profile = {
                "id": "linux-wan22-720p-vram16",
                "settings": {"blocks_to_swap": 32},
            }
            entry = {
                "id": profile["id"], "sha256": "a" * 64,
                "profile": profile, "directory": profile_dir,
            }
            scenario_set = {
                "input": {"image": "portrait.png", "audio": "audio.wav"},
            }
            scenario = {"id": "wan22-720p"}
            with mock.patch.object(
                    BENCHMARK, "captured_command", return_value=(1, "", "stopped", False)), \
                    mock.patch.object(BENCHMARK, "host_snapshot", return_value={}), \
                    mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={}):
                record = BENCHMARK.execute_attempt(
                    ROOT, run_dir, scenario_set, scenario, entry, "attempt-1",
                    "full", 1, "speed", "test", FakeMonitor(), 30,
                    "cold-process")
            self.assertIn("--force", record["command"])
            self.assertGreater(
                record["command"].index("--force"),
                record["command"].index("run"))

    def test_main_only_attempt_runs_and_verifies_generation_stage_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "results" / "main-attempt"
            profile_dir = run_dir / "profiles"
            profile_dir.mkdir(parents=True)
            audio = root / "audio.wav"
            audio.write_bytes(b"placeholder")
            profile = {
                "id": "main-profile", "recipe": "wan21-infinitetalk-480p",
                "settings": {"blocks_to_swap": 0},
            }
            entry = {
                "id": profile["id"], "sha256": "a" * 64,
                "profile": profile, "directory": profile_dir,
            }
            scenario_set = {
                "input": {"image": "portrait.png", "audio": "audio.wav"},
            }
            scenario = {"id": "wan21-480p"}
            fake_catalog = SimpleNamespace(
                recipe_for=lambda _profile: {
                    "id": "wan21-infinitetalk-480p",
                    "pipeline_stages": [
                        "infinitetalk", "face-detailer", "rife", "retime"],
                })

            def command(command, _timeout):
                if "run" in command:
                    output = root / "outputs" / "attempt-main" / "main-audio.mp4"
                    output.parent.mkdir(parents=True)
                    output.write_bytes(b"video")
                    BENCHMARK.write_json(output.parent / "run-state.json", {
                        "status": "completed",
                        "output": str(output.relative_to(root)),
                    })
                    return 0, "", "", False
                return 0, json.dumps({"passed": True}), "", False

            with mock.patch.object(
                    BENCHMARK, "load_catalog",
                    return_value=(fake_catalog, {}, scenario_set)), \
                    mock.patch.object(BENCHMARK, "captured_command", side_effect=command), \
                    mock.patch.object(BENCHMARK, "host_snapshot", return_value={}), \
                    mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={}), \
                    mock.patch.object(BENCHMARK, "telemetry_errors", return_value=[]), \
                    mock.patch.object(BENCHMARK, "main_verify_expectation",
                                      return_value={"fps": 16}):
                record = BENCHMARK.execute_attempt(
                    root, run_dir, scenario_set, scenario, entry, "attempt-main",
                    "full", 1, "speed", "test", FakeMonitor(), 30,
                    "cold-process", main_only=True)

            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["pipeline_scope"], "main")
            self.assertEqual(record["stages"], ["infinitetalk"])
            self.assertEqual(
                record["command"][record["command"].index("--stages") + 1],
                "infinitetalk")
            self.assertIn("--expect", record["verify_command"])
            self.assertNotIn("--profile", record["verify_command"])

    def test_verify_expectation_checks_main_stage_frame_rate(self):
        sys.path.insert(0, str(ROOT / "src"))
        from narration_video_gen import verify
        probe = {
            "format": {"duration": "1.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 832,
                 "height": 480, "nb_frames": "16", "avg_frame_rate": "16/1"},
                {"codec_type": "audio", "codec_name": "aac", "duration": "1.0"},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "main.mp4"
            path.write_bytes(b"video")
            with mock.patch.object(verify, "ffprobe", return_value=probe):
                passed = verify.verify_output(path, {"fps": 16})
                failed = verify.verify_output(path, {"fps": 60})
        self.assertTrue(passed["passed"])
        self.assertFalse(failed["passed"])
        self.assertEqual(
            next(item for item in failed["checks"]
                 if item["name"] == "frame_rate")["ok"], False)

    def test_gpu_benchmark_refuses_fragmenting_allocator_configuration(self):
        with mock.patch.object(
                BENCHMARK, "nvidia_rows", return_value=([FAKE_GPU], {})), \
                mock.patch.dict(BENCHMARK.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "PYTORCH_CUDA_ALLOC_CONF"):
                BENCHMARK.require_gpu_context(
                    {"image_reference": "unknown"}, allow_unknown_image=True)
        with mock.patch.object(
                BENCHMARK, "nvidia_rows", return_value=([FAKE_GPU], {})), \
                mock.patch.dict(BENCHMARK.os.environ, {
                    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                }, clear=True):
            self.assertEqual(BENCHMARK.require_gpu_context(
                {"image_reference": "unknown"}, allow_unknown_image=True),
                [FAKE_GPU])

    def test_optional_system_command_timeout_is_recorded_not_raised(self):
        timeout = BENCHMARK.subprocess.TimeoutExpired(
            ["probe"], 1, output=b"partial", stderr=b"stalled")
        with mock.patch.object(BENCHMARK.subprocess, "run", side_effect=timeout):
            result = BENCHMARK.command_output(["probe"], timeout=1)
        self.assertIn("timed out", result["error"])
        self.assertEqual(result["stdout"], "partial")
        self.assertEqual(result["stderr"], "stalled")

    def test_begin_run_records_host_system_without_blocking_on_probe_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            with mock.patch.object(
                    BENCHMARK, "collect_host_system",
                    side_effect=RuntimeError("probe unavailable")):
                run_dir, _metadata = BENCHMARK.begin_run(
                    ROOT, workspace, "prepare", "host-system-fallback")
            evidence = json.loads(
                (run_dir / "host-system.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["status"], "unavailable")
            self.assertIn("probe unavailable", evidence["reason"])

    def test_comfy_process_lifecycle_preserves_spontaneous_sigkill(self):
        class DeadProcess:
            pid = 4242

            def poll(self):
                return -9

        class Handle:
            closed = False

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary:
            process = DeadProcess()
            process._nvg_started_at = "2026-08-11T00:00:00+00:00"
            process._nvg_started_epoch = time.time() - 1
            process._nvg_lifecycle_path = Path(temporary) / "process.json"
            process._nvg_kernel_before = {"events": []}
            process._nvg_nvidia_before = {"returncode": 0}
            process._nvg_cgroup_before = {"cgroup_oom_kill": 0}
            handle = Handle()
            with mock.patch.object(
                    BENCHMARK, "process_memory_snapshot",
                    return_value={"available": False, "pid": 4242}), \
                    mock.patch.object(
                        BENCHMARK, "kernel_gpu_events",
                        return_value={"events": ["NVRM: Xid 79"]}), \
                    mock.patch.object(
                        BENCHMARK, "nvidia_diagnostics",
                        return_value={"returncode": 0}), \
                    mock.patch.object(
                        BENCHMARK, "_cgroup_memory_snapshot",
                        return_value={"cgroup_oom_kill": 0}):
                evidence = BENCHMARK.stop_comfy(process, handle)
            persisted = json.loads(process._nvg_lifecycle_path.read_text(
                encoding="utf-8"))
        self.assertTrue(handle.closed)
        self.assertTrue(evidence["exit_observed_before_cleanup"])
        self.assertEqual(evidence["controller_action"], "none")
        self.assertEqual(evidence["before_cleanup"]["signal_name"], "SIGKILL")
        self.assertEqual(persisted["new_kernel_gpu_events"], ["NVRM: Xid 79"])

    def test_start_comfy_enables_python_faulthandler(self):
        captured = {}

        class LiveProcess:
            pid = 4242
            returncode = None

            def poll(self):
                return None

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return LiveProcess()

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(BENCHMARK.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(BENCHMARK, "wait_for_comfy"), \
                mock.patch.object(BENCHMARK, "kernel_gpu_events", return_value={"events": []}), \
                mock.patch.object(BENCHMARK, "nvidia_diagnostics", return_value={}), \
                mock.patch.object(BENCHMARK, "_cgroup_memory_snapshot", return_value={}):
            process, handle, _log = BENCHMARK.start_comfy(
                Path(temporary), "faulthandler", 1)
            handle.close()
        self.assertEqual(captured["env"]["PYTHONFAULTHANDLER"], "1")
        self.assertEqual(process.pid, 4242)

    def test_fresh_attempt_links_spontaneous_comfy_exit_to_attempt(self):
        class DeadProcess:
            pid = 4242
            _nvg_lifecycle_path = None

            def poll(self):
                return -11

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "comfy").mkdir()
            (run_dir / "attempts").mkdir()
            process = DeadProcess()
            process._nvg_lifecycle_path = run_dir / "comfy" / "attempt-process.json"
            record = {
                "attempt_id": "attempt", "status": "failed",
                "failure_category": "execution",
            }
            with mock.patch.object(
                    BENCHMARK, "start_comfy",
                    return_value=(process, mock.Mock(), run_dir / "comfy" / "attempt.log")), \
                    mock.patch.object(BENCHMARK, "execute_attempt", return_value=record), \
                    mock.patch.object(
                        BENCHMARK, "process_memory_snapshot", return_value={}), \
                    mock.patch.object(BENCHMARK, "stop_comfy", return_value={
                        "exit_observed_before_cleanup": True,
                    }):
                result = BENCHMARK.fresh_attempt(
                    ROOT, run_dir, {}, {}, {"profile": {"settings": {}}},
                    "attempt", "short", 1,
                    "calibration", "test", FakeMonitor(),
                    SimpleNamespace(comfy_timeout_seconds=1,
                                    attempt_timeout_seconds=1))
            persisted = json.loads((run_dir / "attempts" / "attempt.json").read_text(
                encoding="utf-8"))
        self.assertEqual(result["failure_category"], "comfy-process-exit")
        self.assertEqual(persisted["comfy_returncode"], -11)
        self.assertEqual(persisted["comfy_signal"], "SIGSEGV")
        self.assertEqual(persisted["comfy_process_evidence"],
                         "comfy/attempt-process.json")

    def test_host_system_uses_an_explicit_non_secret_environment_whitelist(self):
        sections = {
            "status": "available",
        }
        command_result = {"returncode": 1, "stderr": "not installed"}
        environment = {
            "CUDA_VISIBLE_DEVICES": "0",
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
            "HF_HOME": "/workspace/cache/huggingface",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "API_KEY": "must-not-appear",
            "HF_TOKEN": "must-not-appear",
            "HOSTNAME": "must-not-appear",
        }
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(BENCHMARK.os.environ, environment, clear=True), \
                mock.patch.object(BENCHMARK, "_os_release", return_value=sections), \
                mock.patch.object(BENCHMARK, "_cpu_details", return_value=sections), \
                mock.patch.object(BENCHMARK, "_parse_meminfo", return_value=({}, "missing")), \
                mock.patch.object(BENCHMARK, "_cgroup_details", return_value=sections), \
                mock.patch.object(BENCHMARK, "_workspace_filesystem", return_value=sections), \
                mock.patch.object(BENCHMARK, "_runtime_details", return_value=sections), \
                mock.patch.object(BENCHMARK, "_resource_limits", return_value=sections), \
                mock.patch.object(BENCHMARK, "nvidia_rows",
                                  return_value=([], command_result)):
            evidence = BENCHMARK.collect_host_system(ROOT, Path(temporary))
        self.assertTrue({
            "kernel", "container_os", "container", "cpu", "memory", "cgroup",
            "workspace_filesystem", "runtime", "gpu", "resource_limits",
            "environment",
        }.issubset(evidence))
        self.assertEqual(evidence["environment"], {
            "CUDA_VISIBLE_DEVICES": "0",
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
            "HF_HOME": "/workspace/cache/huggingface",
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
        })
        encoded = json.dumps(evidence)
        self.assertNotIn("API_KEY", encoded)
        self.assertNotIn("HF_TOKEN", encoded)
        self.assertNotIn("HOSTNAME", encoded)
        self.assertNotIn("must-not-appear", encoded)

    def test_scenarios_do_not_embed_hardware_values(self):
        _catalog, _lock, scenarios = BENCHMARK.load_catalog(ROOT)
        forbidden = {
            "blocks_to_swap", "host_ram_gib_min", "swap_gib_min",
            "gpu_vram_gib_min", "use_non_blocking",
        }

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        self.assertFalse(forbidden.intersection(keys(scenarios)))
        _candidate_id, hypotheses = BENCHMARK.load_profile_hypotheses(ROOT, scenarios)
        blocks = [item["settings"]["blocks_to_swap"] for item in hypotheses]
        self.assertEqual(blocks, sorted(set(blocks)))
        self.assertEqual(blocks[0], 0)
        self.assertGreaterEqual(blocks[-1], 40)

    def test_search_start_table_covers_supported_vram_tiers(self):
        _catalog, _lock, scenarios = BENCHMARK.load_catalog(ROOT)
        table = BENCHMARK.load_search_start_table(ROOT, scenarios)
        self.assertEqual(
            [item["vram_gib"] for item in table["tiers"]],
            [6, 8, 10, 12, 16, 20, 24, 32])
        grid = list(range(41))
        a4000 = BENCHMARK.select_search_start(
            table, "wan22-720p", {"memory.total": "16376"}, grid)
        a4000_wan21_480p = BENCHMARK.select_search_start(
            table, "wan21-480p", {"memory.total": "16376"}, grid)
        between_tiers = BENCHMARK.select_search_start(
            table, "wan22-720p", {"memory.total": str(18 * 1024)}, grid)
        just_above_tier = BENCHMARK.select_search_start(
            table, "wan22-720p", {"memory.total": str(16.4 * 1024)}, grid)
        above_table = BENCHMARK.select_search_start(
            table, "wan22-720p", {"memory.total": str(48 * 1024)}, grid)
        self.assertEqual(a4000["matched_vram_tier_gib"], 16)
        self.assertEqual(a4000["blocks_to_swap"], 28)
        self.assertEqual(a4000_wan21_480p["matched_vram_tier_gib"], 16)
        self.assertEqual(a4000_wan21_480p["blocks_to_swap"], 12)
        self.assertEqual(between_tiers["matched_vram_tier_gib"], 20)
        self.assertEqual(between_tiers["blocks_to_swap"], 22)
        self.assertEqual(just_above_tier["nominal_vram_gib"], 17)
        self.assertEqual(just_above_tier["matched_vram_tier_gib"], 20)
        self.assertEqual(just_above_tier["blocks_to_swap"], 22)
        self.assertIsNone(above_table["matched_vram_tier_gib"])
        self.assertEqual(above_table["blocks_to_swap"], 0)

    def test_candidates_preserve_named_native_vae_setting(self):
        catalog, _lock, scenario_set = BENCHMARK.load_catalog(ROOT)
        scenarios = BENCHMARK.selected_scenarios(scenario_set, ["wan21-720p"])
        base = copy.deepcopy(catalog.profiles[scenarios[0]["profile"]])
        base["settings"]["vae_native_upsample"] = True
        base["settings"]["offload_transformer_before_vae_decode"] = True
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            result = BENCHMARK.materialize_candidate_profiles(
                ROOT, run_dir, scenarios, FAKE_GPU, scenario_set, base_profile=base)
            entries = result["wan21-720p"]
            self.assertTrue(all(entry["profile"]["settings"]["vae_native_upsample"]
                                is True for entry in entries))
            self.assertTrue(all(entry["profile"]["settings"]["offload_transformer_before_vae_decode"]
                                is True for entry in entries))
            self.assertTrue(all(entry["profile"]["settings"]["blocks_to_swap"]
                                == entry["blocks_to_swap"] for entry in entries))
            plan = json.loads((run_dir / "calibration-plan.json").read_text())
            self.assertEqual(plan["base_profile_snapshot"], base)
            base["recipe"] = "wan22-s2v-720p"
            with self.assertRaisesRegex(ValueError, "match the selected scenario"):
                BENCHMARK.materialize_candidate_profiles(
                    ROOT, run_dir, scenarios, FAKE_GPU, scenario_set, base_profile=base)

    def test_materialized_profiles_are_named_catalog_overlays(self):
        _catalog, _lock, scenario_set = BENCHMARK.load_catalog(ROOT)
        scenario = BENCHMARK.selected_scenarios(scenario_set, ["wan21-480p"])
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            result = BENCHMARK.materialize_candidate_profiles(
                ROOT, run_dir, scenario, FAKE_GPU, scenario_set)
            entries = result["wan21-480p"]
            self.assertEqual(len(entries), len({item["id"] for item in entries}))
            self.assertTrue(all("host_ram_gib_min" not in item["profile"]["requires"]
                                for item in entries))
            self.assertTrue(all("free_disk_gib_min" not in item["profile"]["requires"]
                                for item in entries))
            self.assertTrue(all("timing" not in item["profile"] for item in entries))
            catalog = BENCHMARK.load_catalog(ROOT)[0].__class__(
                ROOT, profile_dirs=[entries[0]["directory"]])
            loaded = catalog.profiles[entries[0]["id"]]
            self.assertEqual(
                loaded["settings"]["blocks_to_swap"], entries[0]["blocks_to_swap"])
            plan = json.loads(
                (run_dir / "calibration-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], 2)
            self.assertEqual(plan["search_start_table"],
                             "block-swap-search-starts-v1")
            self.assertEqual(plan["selection_safety_margin_blocks"], 3)
            self.assertEqual(plan["search_starts"]["wan21-480p"][
                "blocks_to_swap"], 0)

    def test_runtime_disk_preflight_uses_generation_headroom(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            destination = workspace / "runtime-disk-preflight.json"
            gib = 1024 ** 3
            with mock.patch.object(
                    BENCHMARK.shutil, "disk_usage",
                    return_value=BENCHMARK.shutil._ntuple_diskusage(
                        100 * gib, 58 * gib, 42 * gib)):
                record = BENCHMARK.verify_runtime_disk(workspace, destination)
            self.assertEqual(record["status"], "ok")
            self.assertEqual(record["required_free_gib"], 20)
            self.assertEqual(record["requirement_kind"], "generation-headroom")

            with mock.patch.object(
                    BENCHMARK.shutil, "disk_usage",
                    return_value=BENCHMARK.shutil._ntuple_diskusage(
                        100 * gib, 81 * gib, 19 * gib)):
                with self.assertRaisesRegex(
                        RuntimeError, "requires at least 20 GiB"):
                    BENCHMARK.verify_runtime_disk(workspace, destination)
            failed = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "insufficient")

            with mock.patch.dict(BENCHMARK.os.environ, {
                    "NVG_RUNTIME_DISK_HEADROOM_GIB": "4"}), mock.patch.object(
                    BENCHMARK.shutil, "disk_usage",
                    return_value=BENCHMARK.shutil._ntuple_diskusage(
                        6 * gib, 1 * gib, 5 * gib)):
                record = BENCHMARK.verify_runtime_disk(workspace, destination)
            self.assertEqual(record["status"], "ok")
            self.assertEqual(record["required_free_gib"], 4)

    def test_framepack_decode_offload_uses_resolution_and_physical_vram(self):
        wan22_720 = {"model_family": "wan22-s2v", "resolution": [1280, 720]}
        wan22_480 = {"model_family": "wan22-s2v", "resolution": [832, 480]}
        wan21_720 = {"model_family": "wan21-infinitetalk", "resolution": [1280, 720]}

        self.assertTrue(BENCHMARK.framepack_decode_offload_required(
            wan22_720, {"memory.total": "16376"}))
        self.assertTrue(BENCHMARK.framepack_decode_offload_required(
            wan22_720, {"memory.total": str(16 * 1024)}))
        self.assertFalse(BENCHMARK.framepack_decode_offload_required(
            wan22_720, {"memory.total": "24576"}))
        self.assertFalse(BENCHMARK.framepack_decode_offload_required(
            wan22_480, {"memory.total": "12288"}))
        self.assertFalse(BENCHMARK.framepack_decode_offload_required(
            wan21_720, {"memory.total": "12288"}))

    def test_materialized_s2v_profiles_override_the_a4000_default(self):
        _catalog, _lock, scenario_set = BENCHMARK.load_catalog(ROOT)
        scenarios = BENCHMARK.selected_scenarios(
            scenario_set, ["wan22-480p", "wan22-720p"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            on_16gib = BENCHMARK.materialize_candidate_profiles(
                ROOT, root / "16gib", scenarios,
                {**FAKE_GPU, "memory.total": "16376"}, scenario_set)
            on_24gib = BENCHMARK.materialize_candidate_profiles(
                ROOT, root / "24gib", scenarios, FAKE_GPU, scenario_set)

            self.assertTrue(all(
                entry["profile"]["settings"][
                    "offload_transformer_before_vae_decode"]
                for entry in on_16gib["wan22-720p"]))
            self.assertFalse(any(
                entry["profile"]["settings"][
                    "offload_transformer_before_vae_decode"]
                for entry in on_16gib["wan22-480p"]))
            self.assertFalse(any(
                entry["profile"]["settings"][
                    "offload_transformer_before_vae_decode"]
                for entry in on_24gib["wan22-720p"]))
            self.assertTrue(all(
                entry["profile"]["settings"].get("face_detailer_size") == 384
                and entry["profile"]["settings"].get(
                    "face_detailer_blocks_to_swap") == 30
                for entry in on_16gib["wan22-720p"]))
            self.assertTrue(all(
                entry["profile"]["settings"].get("face_detailer_size") == 512
                and entry["profile"]["settings"].get(
                    "face_detailer_blocks_to_swap") == 20
                and entry["profile"]["calibration_candidate"].get(
                    "face_detailer_profile")
                == "wan22-720p-face-detailer-vram24"
                for entry in on_24gib["wan22-720p"]))
            self.assertTrue(all(
                entry["profile"]["calibration_candidate"].get(
                    "face_detailer_profile") is None
                for entry in on_24gib["wan22-480p"]))

    def test_24gib_face_detailer_profiles_cover_both_720p_models_only(self):
        _catalog, _lock, scenario_set = BENCHMARK.load_catalog(ROOT)
        scenarios = BENCHMARK.selected_scenarios(scenario_set, [
            "wan21-480p", "wan21-720p", "wan22-480p", "wan22-720p"])
        with tempfile.TemporaryDirectory() as temporary:
            materialized = BENCHMARK.materialize_candidate_profiles(
                ROOT, Path(temporary), scenarios, FAKE_GPU, scenario_set)
        expected_blocks = {"wan21-720p": 20, "wan22-720p": 20}
        for scenario_id in ("wan21-720p", "wan22-720p"):
            for entry in materialized[scenario_id]:
                settings = entry["profile"]["settings"]
                self.assertEqual(settings["face_detailer_size"], 512)
                self.assertEqual(
                    settings["face_detailer_blocks_to_swap"],
                    expected_blocks[scenario_id])
                self.assertEqual(
                    entry["profile"]["calibration_candidate"][
                        "face_detailer_profile"],
                    "%s-face-detailer-vram24" % scenario_id)
        for scenario_id in ("wan21-480p", "wan22-480p"):
            self.assertTrue(all(
                entry["profile"]["calibration_candidate"].get(
                    "face_detailer_profile") is None
                for entry in materialized[scenario_id]))

        with tempfile.TemporaryDirectory() as temporary:
            on_16gib = BENCHMARK.materialize_candidate_profiles(
                ROOT, Path(temporary), scenarios,
                {**FAKE_GPU, "memory.total": "16376"}, scenario_set)
        self.assertTrue(all(
            entry["profile"]["settings"].get("face_detailer_size") == 384
            and entry["profile"]["settings"].get(
                "face_detailer_blocks_to_swap") == 30
            for entry in on_16gib["wan21-720p"]))

    def test_32gib_720p_face_detailer_profiles_are_provisional_block_four(self):
        _catalog, _lock, scenario_set = BENCHMARK.load_catalog(ROOT)
        scenarios = BENCHMARK.selected_scenarios(scenario_set, [
            "wan21-480p", "wan21-720p", "wan22-480p", "wan22-720p"])
        gpu = {**FAKE_GPU, "memory.total": "32768"}
        with tempfile.TemporaryDirectory() as temporary:
            materialized = BENCHMARK.materialize_candidate_profiles(
                ROOT, Path(temporary), scenarios, gpu, scenario_set)

        for scenario_id in ("wan21-720p", "wan22-720p"):
            for entry in materialized[scenario_id]:
                settings = entry["profile"]["settings"]
                self.assertEqual(settings["face_detailer_size"], 512)
                self.assertEqual(settings["face_detailer_blocks_to_swap"], 4)
                self.assertEqual(
                    entry["profile"]["calibration_candidate"][
                        "face_detailer_profile"],
                    "%s-face-detailer-vram32-provisional" % scenario_id)
        for scenario_id in ("wan21-480p", "wan22-480p"):
            self.assertTrue(all(
                entry["profile"]["calibration_candidate"].get(
                    "face_detailer_profile") is None
                for entry in materialized[scenario_id]))

    def test_capacity_observation_separates_shape_from_peak(self):
        record = {
            "status": "passed",
            "host_before": {
                "mem_total_bytes": 64 * GIB,
                "swap_total_bytes": 32 * GIB,
                "cgroup_memory_max_bytes": 48 * GIB,
                "cgroup_swap_max_bytes": 8 * GIB,
            },
            "telemetry_summary": {
                "max_host_memory_used_bytes": 30 * GIB,
                "max_host_swap_used_bytes": 3 * GIB,
            },
        }
        result = BENCHMARK.capacity_observation([record])
        self.assertEqual(result["configured_host_ram_gib"], 48.0)
        self.assertEqual(result["configured_host_swap_gib"], 8.0)
        self.assertEqual(result["observed_host_ram_used_peak_gib"], 30.0)
        self.assertEqual(result["observed_host_swap_used_peak_gib"], 3.0)
        self.assertEqual(result["basis"], "observed-capacity-not-minimum")
        swap_disabled = BENCHMARK.capacity_observation([{
            "status": "passed",
            "host_before": {
                "mem_total_bytes": 64 * GIB,
                "swap_total_bytes": 32 * GIB,
                "cgroup_memory_max_bytes": 48 * GIB,
                "cgroup_swap_max_bytes": 0,
            },
            "telemetry_summary": {},
        }])
        self.assertEqual(swap_disabled["configured_host_swap_gib"], 0.0)
        self.assertEqual(swap_disabled["cgroup_swap_max_gib"], 0.0)
        gpu_result = BENCHMARK.gpu_capacity_observation(FAKE_GPU, [{
            "telemetry_summary": {
                "gpu_max_memory_used_mib": {"GPU-test": 23000.0}}}])
        self.assertEqual(gpu_result["memory_total_mib"], 24576.0)
        self.assertEqual(gpu_result["headroom_mib"], 1576.0)

    def test_passed_attempt_requires_complete_gpu_host_and_cgroup_telemetry(self):
        host = {
            "mem_total_bytes": 64 * GIB,
            "mem_available_bytes": 32 * GIB,
            "swap_total_bytes": 8 * GIB,
            "swap_free_bytes": 7 * GIB,
            "cgroup_memory_current_bytes": 30 * GIB,
            "cgroup_swap_current_bytes": 1 * GIB,
            "cgroup_oom": 0,
            "cgroup_oom_kill": 0,
            "cgroup_version": 2,
            "cgroup_v2_available": True,
            "cgroup_memory_pressure_available": True,
            "cgroup_pressure_some_avg10": 0.0,
            "cgroup_pressure_full_avg10": 0.0,
        }
        complete = {
            "host_before": host,
            "host_after": host,
            "telemetry_summary": {
                "sample_count": 1,
                "gpu_max_memory_used_mib": {"GPU-test": 23000},
                "host_memory_total_bytes": 64 * GIB,
                "max_host_memory_used_bytes": 32 * GIB,
                "host_swap_total_bytes": 8 * GIB,
                "max_host_swap_used_bytes": 1 * GIB,
                "max_cgroup_memory_current_bytes": 30 * GIB,
                "max_cgroup_swap_current_bytes": 1 * GIB,
                "max_cgroup_oom": 0,
                "max_cgroup_oom_kill": 0,
                "max_cgroup_pressure_some_avg10": 0.0,
                "max_cgroup_pressure_full_avg10": 0.0,
                "max_system_pressure_some_avg10": 0.0,
                "max_system_pressure_full_avg10": 0.0,
            },
        }
        self.assertEqual(BENCHMARK.telemetry_errors(complete), [])
        incomplete = {"host_before": complete["host_before"],
                      "host_after": complete["host_after"],
                      "telemetry_summary": {}}
        errors = BENCHMARK.telemetry_errors(incomplete)
        self.assertIn("no attempt-labelled resource sample", errors)
        self.assertIn("no GPU memory sample", errors)

    def test_passed_attempt_accepts_cgroup_v1_with_system_pressure(self):
        host = {
            "mem_total_bytes": 64 * GIB,
            "mem_available_bytes": 32 * GIB,
            "swap_total_bytes": 0,
            "swap_free_bytes": 0,
            "cgroup_version": 1,
            "cgroup_memory_current_bytes": 30 * GIB,
            "cgroup_swap_current_bytes": 0,
            "cgroup_oom": 0,
            "cgroup_oom_kill": 0,
            "cgroup_v2_available": False,
            "cgroup_memory_pressure_available": False,
        }
        record = {
            "host_before": host,
            "host_after": host,
            "telemetry_summary": {
                "sample_count": 1,
                "gpu_max_memory_used_mib": {"GPU-test": 15000},
                "host_memory_total_bytes": 64 * GIB,
                "max_host_memory_used_bytes": 32 * GIB,
                "host_swap_total_bytes": 0,
                "max_host_swap_used_bytes": 0,
                "max_cgroup_memory_current_bytes": 30 * GIB,
                "max_cgroup_swap_current_bytes": 0,
                "max_cgroup_oom": 0,
                "max_cgroup_oom_kill": 0,
                "max_system_pressure_some_avg10": 0.0,
                "max_system_pressure_full_avg10": 0.0,
            },
        }
        self.assertEqual(BENCHMARK.telemetry_errors(record), [])

    def test_failure_classification_only_uses_gpu_oom_as_capacity_signal(self):
        self.assertEqual(BENCHMARK.classify_failure(
            1, "", "CUDA out of memory"), "gpu-oom")
        self.assertEqual(BENCHMARK.classify_failure(
            -9, "", "", host_oom=True), "host-oom")
        self.assertEqual(BENCHMARK.classify_failure(1, "", "network"), "execution")
        self.assertEqual(BENCHMARK.classify_failure(None, "", "", timed_out=True),
                         "timeout")

    def test_saturated_wsl_device_loss_is_a_gpu_capacity_signal(self):
        gpu = {"gpus": [{"memory.total": "16311"}]}
        saturated = {"gpu_max_memory_used_mib": {"gpu-0": 15994}}
        below_threshold = {"gpu_max_memory_used_mib": {"gpu-0": 15000}}
        message = "CUDA driver error: device not ready"
        self.assertEqual(BENCHMARK.classify_saturated_gpu_device_loss(
            "execution", "", message, saturated, gpu), "gpu-oom")
        self.assertEqual(BENCHMARK.classify_saturated_gpu_device_loss(
            "execution", "", message, below_threshold, gpu), "execution")
        self.assertEqual(BENCHMARK.classify_saturated_gpu_device_loss(
            "execution", "", "network", saturated, gpu), "execution")

        observed_wsl_peak = {"gpu_max_memory_used_mib": {"gpu-0": 13860}}
        observed_wsl_gpu = {"gpus": [{"memory.total": "16376"}]}
        self.assertEqual(BENCHMARK.classify_saturated_gpu_device_loss(
            "execution", "", message, observed_wsl_peak, observed_wsl_gpu,
            wsl_allocator_shim_active=True), "gpu-oom")
        self.assertEqual(BENCHMARK.classify_saturated_gpu_device_loss(
            "execution", "", message, observed_wsl_peak, observed_wsl_gpu,
            wsl_allocator_shim_active=False), "execution")

    def test_attempt_timeout_reaps_the_process_group(self):
        started = time.monotonic()
        returncode, _stdout, _stderr, timed_out = BENCHMARK.captured_command(
            [sys.executable, "-c", "import time; time.sleep(60)"], 0.05)
        self.assertTrue(timed_out)
        self.assertNotEqual(returncode, 0)
        self.assertLess(time.monotonic() - started, 5)

    def test_calibration_selects_and_records_safety_adjusted_named_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context",
                                  return_value=[FAKE_GPU]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [FAKE_GPU]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(24)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "calibration-test")), 0)
            run_dir = workspace / "results" / "calibration-test"
            profile_set = json.loads(
                (run_dir / "profile-set.json").read_text(encoding="utf-8"))
            selected = profile_set["profiles"]["wan21-480p"]
            self.assertEqual(selected["blocks_to_swap"], 24)
            self.assertIsNone(selected["raw_short_pass_boundary_blocks_to_swap"])
            self.assertIsNone(selected["raw_short_failure_boundary_blocks_to_swap"])
            self.assertEqual(selected["raw_short_lower_boundary_blocks_to_swap"], 20)
            self.assertEqual(selected["selection_safety_margin_blocks"], 3)
            self.assertEqual(selected["effective_safety_margin_blocks"], 3)
            self.assertEqual(
                selected["qualification"], "short-stable-safety-margin")
            attempts = json.loads((run_dir / "runs.json").read_text(encoding="utf-8"))
            self.assertEqual(attempts[0]["blocks_to_swap"], 0)
            self.assertEqual(sum(item["blocks_to_swap"] == 20
                                 and item["failure_category"] == "gpu-oom"
                                 for item in attempts), 1)
            self.assertEqual(sum(item["blocks_to_swap"] == 24
                                 and item["status"] == "passed"
                                 for item in attempts), 3)

    def test_calibration_starts_from_the_vram_scenario_hint(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            gpu = {**FAKE_GPU, "memory.total": "16376"}
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[gpu]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [gpu]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(32)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "seeded-test", "wan22-720p")), 0)
            attempts = json.loads(
                (workspace / "results" / "seeded-test" / "runs.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(
                [item["blocks_to_swap"] for item in attempts[:5]],
                [28, 32, 32, 32])
            self.assertFalse(any(item["blocks_to_swap"] < 28 for item in attempts))
            self.assertEqual(sum(item["blocks_to_swap"] == 32
                                 and item["status"] == "passed"
                                 for item in attempts), 3)

    def test_grid_minimum_zero_waives_margin_after_three_fresh_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context",
                                  return_value=[FAKE_GPU]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot",
                                  return_value={"gpus": [FAKE_GPU]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(
                    0, telemetry_peak_mib=22000)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "zero-grid-minimum-test")), 0)
            run_dir = workspace / "results" / "zero-grid-minimum-test"
            attempts = json.loads((run_dir / "runs.json").read_text(encoding="utf-8"))
            selected = json.loads((run_dir / "profile-set.json").read_text(
                encoding="utf-8"))["profiles"]["wan21-480p"]
        self.assertEqual([item["blocks_to_swap"] for item in attempts], [0, 0, 0])
        self.assertTrue(all(item["status"] == "passed" for item in attempts))
        self.assertEqual(selected["blocks_to_swap"], 0)
        self.assertEqual(selected["raw_short_pass_boundary_blocks_to_swap"], 0)
        self.assertEqual(selected["selection_safety_margin_blocks"], 3)
        self.assertEqual(selected["effective_safety_margin_blocks"], 0)
        self.assertTrue(selected["selection_safety_margin_waived_at_grid_minimum"])
        self.assertEqual(selected["qualification"], "short-stable-grid-minimum")
        self.assertEqual(selected["zero_swap_full_headroom_policy"]["short_headroom_mib"], 2576)
        self.assertEqual(selected["zero_swap_full_headroom_policy"]["vram_per_block_mib"], 350)
        self.assertTrue(selected["zero_swap_full_headroom_policy"]["waived_margin"])

    def test_grid_minimum_retains_margin_when_short_headroom_is_too_small(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[FAKE_GPU]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [FAKE_GPU]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(
                    0, telemetry_peak_mib=23700)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "zero-grid-headroom-test")), 0)
            run_dir = workspace / "results" / "zero-grid-headroom-test"
            attempts = json.loads((run_dir / "runs.json").read_text(encoding="utf-8"))
            selected = json.loads((run_dir / "profile-set.json").read_text(
                encoding="utf-8"))["profiles"]["wan21-480p"]
        self.assertEqual([item["blocks_to_swap"] for item in attempts], [0, 0, 0])
        self.assertEqual(selected["blocks_to_swap"], 1)
        self.assertFalse(selected["selection_safety_margin_waived_at_grid_minimum"])
        self.assertFalse(selected["zero_swap_full_headroom_policy"]["waived_margin"])
        self.assertEqual(selected["zero_swap_full_headroom_policy"]["available_headroom_blocks"], 2)

    def test_grid_minimum_oom_restores_the_full_safety_jump(self):
        zero_calls = 0
        pass_all = synthetic_attempt(0, telemetry_peak_mib=22000)
        pass_at_four = synthetic_attempt(4)

        def unstable_zero(*args, **kwargs):
            nonlocal zero_calls
            blocks = args[4]["blocks_to_swap"]
            if blocks == 0:
                zero_calls += 1
                if zero_calls == 1:
                    return pass_all(*args, **kwargs)
            return pass_at_four(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context",
                                  return_value=[FAKE_GPU]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot",
                                  return_value={"gpus": [FAKE_GPU]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", unstable_zero),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "zero-grid-oom-test")), 0)
            run_dir = workspace / "results" / "zero-grid-oom-test"
            attempts = json.loads((run_dir / "runs.json").read_text(encoding="utf-8"))
            selected = json.loads((run_dir / "profile-set.json").read_text(
                encoding="utf-8"))["profiles"]["wan21-480p"]
        self.assertEqual(
            [(item["blocks_to_swap"], item["status"]) for item in attempts],
            [(0, "passed"), (0, "failed"),
             (4, "passed"), (4, "passed"), (4, "passed")])
        self.assertEqual(selected["blocks_to_swap"], 4)
        self.assertFalse(selected["selection_safety_margin_waived_at_grid_minimum"])
        self.assertEqual(selected["effective_safety_margin_blocks"], 3)
        self.assertEqual(selected["raw_short_lower_boundary_blocks_to_swap"], 0)
        self.assertEqual(selected["raw_short_lower_boundary_outcome"],
                         "confirmed-oom-safety-jump")

    def test_a4000_confirmed_oom_12_selects_margin_16(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            gpu = {**FAKE_GPU, "memory.total": "16376"}
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[gpu]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [gpu]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(13)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "a4000-boundary-16-test")), 0)
            attempts = json.loads(
                (workspace / "results" / "a4000-boundary-16-test" /
                 "runs.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [(item["blocks_to_swap"], item["status"]) for item in attempts],
                [(12, "failed"), (16, "passed"),
                 (16, "passed"), (16, "passed")])
            profile = json.loads(
                (workspace / "results" / "a4000-boundary-16-test" /
                 "profile-set.json").read_text(encoding="utf-8"))[
                     "profiles"]["wan21-480p"]
            self.assertIsNone(profile["raw_short_pass_boundary_blocks_to_swap"])
            self.assertIsNone(profile["raw_short_failure_boundary_blocks_to_swap"])
            self.assertEqual(profile["raw_short_lower_boundary_blocks_to_swap"], 12)
            self.assertEqual(profile["blocks_to_swap"], 16)

    def test_boundary_mode_ascends_one_block_from_a_failing_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            gpu = {**FAKE_GPU, "memory.total": "16376"}
            args = gpu_args(workspace, "boundary-ascending-test")
            args.search_mode = "boundary"
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[gpu]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [gpu]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(15)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(args), 0)
            run_dir = workspace / "results" / "boundary-ascending-test"
            attempts = json.loads((run_dir / "runs.json").read_text(encoding="utf-8"))
            profile_set = json.loads(
                (run_dir / "profile-set.json").read_text(encoding="utf-8"))
            selected = profile_set["profiles"]["wan21-480p"]
        self.assertEqual(
            [(item["blocks_to_swap"], item["status"]) for item in attempts],
            [(12, "failed"), (13, "failed"), (14, "failed"),
             (15, "passed"), (14, "failed"),
             (18, "passed"), (18, "passed"), (18, "passed")])
        self.assertEqual(profile_set["search_mode"], "boundary")
        self.assertEqual(selected["raw_short_pass_boundary_blocks_to_swap"], 15)
        self.assertEqual(selected["raw_short_failure_boundary_blocks_to_swap"], 14)
        self.assertEqual(selected["blocks_to_swap"], 18)

    def test_boundary_mode_brackets_down_then_fills_up_from_a_passing_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            gpu = {**FAKE_GPU, "memory.total": "16376"}
            args = gpu_args(workspace, "boundary-downward-test")
            args.search_mode = "boundary"
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[gpu]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [gpu]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(10)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(args), 0)
            run_dir = workspace / "results" / "boundary-downward-test"
            attempts = json.loads((run_dir / "runs.json").read_text(encoding="utf-8"))
            selected = json.loads(
                (run_dir / "profile-set.json").read_text(encoding="utf-8"))[
                    "profiles"]["wan21-480p"]
        self.assertEqual(
            [(item["blocks_to_swap"], item["status"]) for item in attempts],
            [(12, "passed"), (11, "passed"), (9, "failed"),
             (10, "passed"), (9, "failed"),
             (13, "passed"), (13, "passed"), (13, "passed")])
        self.assertEqual(selected["raw_short_pass_boundary_blocks_to_swap"], 10)
        self.assertEqual(selected["raw_short_failure_boundary_blocks_to_swap"], 9)
        self.assertEqual(selected["blocks_to_swap"], 13)

    def test_boundary_search_is_default_and_fast_remains_selectable(self):
        default = BENCHMARK.parser().parse_args(["calibrate"])
        fast = BENCHMARK.parser().parse_args(
            ["calibrate", "--search-mode", "fast"])
        self.assertEqual(default.search_mode, "boundary")
        self.assertEqual(fast.search_mode, "fast")

    def test_mixed_lower_boundary_uses_margin_and_stable_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            gpu = {**FAKE_GPU, "memory.total": "16376"}
            base_attempt = synthetic_attempt(8)
            calls = {}

            def mixed_boundary(*args, **kwargs):
                record = base_attempt(*args, **kwargs)
                blocks = record["blocks_to_swap"]
                calls[blocks] = calls.get(blocks, 0) + 1
                if blocks == 7 and calls[blocks] == 2:
                    record["status"] = "passed"
                    record["failure_category"] = None
                return record

            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[gpu]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [gpu]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", mixed_boundary),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "mixed-boundary-test")), 0)
            profile = json.loads(
                (workspace / "results" / "mixed-boundary-test" /
                 "profile-set.json").read_text(encoding="utf-8"))[
                     "profiles"]["wan21-480p"]
            self.assertEqual(profile["raw_short_pass_boundary_blocks_to_swap"], 8)
            self.assertIsNone(profile["raw_short_failure_boundary_blocks_to_swap"])
            self.assertEqual(profile["raw_short_lower_boundary_blocks_to_swap"], 7)
            self.assertEqual(
                profile["raw_short_lower_boundary_outcome"],
                "mixed-pass-gpu-oom")
            self.assertEqual(profile["blocks_to_swap"], 11)
            attempts = json.loads(
                (workspace / "results" / "mixed-boundary-test" /
                 "runs.json").read_text(encoding="utf-8"))
            self.assertEqual(sum(
                item["blocks_to_swap"] == 11 and item["status"] == "passed"
                for item in attempts), 3)

    def test_safety_adjusted_candidate_oom_moves_selection_upward(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            gpu = {**FAKE_GPU, "memory.total": "16376"}
            base_attempt = synthetic_attempt(13)
            calls = {}

            def unstable_margin(*args, **kwargs):
                record = base_attempt(*args, **kwargs)
                blocks = record["blocks_to_swap"]
                calls[blocks] = calls.get(blocks, 0) + 1
                if blocks == 16 and calls[blocks] == 2:
                    record["status"] = "failed"
                    record["failure_category"] = "gpu-oom"
                return record

            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[gpu]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [gpu]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(
                    BENCHMARK, "fresh_attempt",
                    unstable_margin),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "margin-moves-up-test")), 0)
            profile = json.loads(
                (workspace / "results" / "margin-moves-up-test" /
                 "profile-set.json").read_text(encoding="utf-8"))[
                     "profiles"]["wan21-480p"]
            self.assertIsNone(profile["raw_short_pass_boundary_blocks_to_swap"])
            self.assertEqual(profile["selection_safety_margin_blocks"], 3)
            self.assertEqual(profile["effective_safety_margin_blocks"], 3)
            self.assertEqual(profile["blocks_to_swap"], 20)

    def test_passing_seed_brackets_downward_before_ascending(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            gpu = {**FAKE_GPU, "memory.total": "16376"}
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context", return_value=[gpu]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [gpu]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(8)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "downward-test")), 0)
            run_dir = workspace / "results" / "downward-test"
            selected = json.loads(
                (run_dir / "profile-set.json").read_text(encoding="utf-8"))[
                    "profiles"]["wan21-480p"]
            attempts = json.loads((run_dir / "runs.json").read_text(encoding="utf-8"))
            self.assertEqual(selected["raw_short_pass_boundary_blocks_to_swap"], 8)
            self.assertEqual(selected["blocks_to_swap"], 11)
            self.assertEqual(
                [item["blocks_to_swap"] for item in attempts[:3]], [12, 8, 0])
            self.assertTrue(any(item["blocks_to_swap"] == 8
                                and item["status"] == "passed"
                                for item in attempts))

    def test_late_oom_jumps_to_margin_and_restarts_stable_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            attempts = synthetic_attempt(
                24, elapsed_seconds=lambda _blocks, passed: 1.0 if passed else 600.0)
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context",
                                  return_value=[FAKE_GPU]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot",
                                  return_value={"gpus": [FAKE_GPU]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", attempts),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                self.assertEqual(BENCHMARK.cmd_calibrate(
                    gpu_args(workspace, "late-oom-test")), 0)
            records = json.loads(
                (workspace / "results" / "late-oom-test" / "runs.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(
                [item["blocks_to_swap"] for item in records],
                [0, 4, 8, 12, 16, 20, 24, 24, 24])
            self.assertTrue(all(
                item["selection_reason"] == "confirmed-oom-safety-jump-confirmation"
                for item in records[1:]))
            profile = json.loads(
                (workspace / "results" / "late-oom-test" /
                 "profile-set.json").read_text(encoding="utf-8"))[
                     "profiles"]["wan21-480p"]
            self.assertEqual(profile["blocks_to_swap"], 24)
            self.assertIsNone(profile["raw_short_pass_boundary_blocks_to_swap"])
            self.assertEqual(profile["raw_short_lower_boundary_blocks_to_swap"], 20)
            self.assertEqual(
                profile["raw_short_lower_boundary_outcome"],
                "confirmed-oom-safety-jump")
            self.assertEqual(profile["effective_safety_margin_blocks"], 3)

    def test_all_candidates_gpu_oom_is_no_fit_not_inconclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context",
                                  return_value=[FAKE_GPU]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot", return_value={"gpus": [FAKE_GPU]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(None)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaisesRegex(RuntimeError, "no-fit"):
                    BENCHMARK.cmd_calibrate(gpu_args(workspace, "no-fit-test"))
            metadata = json.loads((workspace / "results" / "no-fit-test" /
                                   "benchmark.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "no-fit")

    def test_safety_margin_that_exceeds_candidate_grid_is_no_fit(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            patches = (
                mock.patch.object(BENCHMARK, "require_gpu_context",
                                  return_value=[FAKE_GPU]),
                model_preflight_patch(),
                mock.patch.object(BENCHMARK, "activate_output", return_value=None),
                mock.patch.object(BENCHMARK, "gpu_snapshot",
                                  return_value={"gpus": [FAKE_GPU]}),
                mock.patch.object(BENCHMARK, "ResourceMonitor", FakeMonitor),
                mock.patch.object(BENCHMARK, "fresh_attempt", synthetic_attempt(39)),
                mock.patch.object(BENCHMARK, "select_search_start", return_value={
                    "blocks_to_swap": 40, "matched_vram_tier_gib": 24,
                    "nominal_vram_gib": 24, "physical_vram_mib": 24576,
                }),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6]:
                with self.assertRaisesRegex(RuntimeError, "cannot apply safety margin"):
                    BENCHMARK.cmd_calibrate(
                        gpu_args(workspace, "margin-no-fit-test"))
            metadata = json.loads(
                (workspace / "results" / "margin-no-fit-test" /
                 "benchmark.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "no-fit")

    def test_length_modes_are_mutually_exclusive(self):
        parser = BENCHMARK.parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "run", "--reference-profiles", "--short-only", "--full-only"])
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "benchmark", "--scenario", "wan21-480p",
                "--short-only", "--full-only"])
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "queue", "add", "--scenario", "wan21-480p",
                "--full-only", "--short-and-full"])
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "queue", "add", "--scenario", "wan22-720p",
                "--profile", "linux-wan22-720p-vram16",
                "--profile-file", "/tmp/custom.yaml"])

    def test_bounded_child_run_ids_do_not_collide_on_long_parent_ids(self):
        first = BENCHMARK.bounded_run_id("a" * 128 + "-one")
        second = BENCHMARK.bounded_run_id("a" * 128 + "-two")
        self.assertLessEqual(len(first), 128)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
