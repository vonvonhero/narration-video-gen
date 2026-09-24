"""CPU-only checks for named Q4 recipe selection and ballast preservation."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from narration_video_gen.catalog import Catalog
from narration_video_gen.compat import load_yaml_file
from narration_video_gen.stages import build_s2v

spec = importlib.util.spec_from_file_location("q4_calibration", ROOT / "calibration/calibrate.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class Q4CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_yaml_file(ROOT / "profiles/linux/linux-wan22-720p-vram12-simulated.yaml")
        self.catalog = Catalog(ROOT)

    def test_graph_uses_gguf_without_fp8_requantization(self):
        recipe = self.catalog.recipe_for(self.profile)
        models = dict(s2v="Q4.gguf", vae="vae", audio_encoder="audio", text_encoder="text", lora="lora")
        graph, _ = build_s2v(recipe, self.profile, models, "portrait.png", "speech.wav", 89, "q4")
        self.assertEqual(graph["2"]["inputs"]["quantization"], "disabled")
        self.assertEqual(graph["2"]["inputs"]["model"], "Q4.gguf")
        self.assertEqual(graph["19"]["inputs"]["length"], 89)

    def test_override_preflight_and_candidates_keep_q4_and_ballast(self):
        token = bench._CALIBRATION_PROFILE.set(("linux-wan22-720p-vram16", self.profile))
        try:
            _, _, scenario_set = bench.load_catalog(ROOT)
            scenarios = bench.selected_scenarios(scenario_set, ["wan22-720p"])
            models, _ = bench.selected_models(ROOT, False, scenarios=scenarios, scope="main")
            self.assertIn("wan22-s2v-14b-q4ks", [m["id"] for m in models])
            self.assertNotIn("wan22-s2v-14b-fp8", [m["id"] for m in models])
            with tempfile.TemporaryDirectory() as temp:
                candidates = bench.materialize_candidate_profiles(ROOT, Path(temp), scenarios,
                    {"name": "test", "memory.total": "12288", "uuid": "test"}, scenario_set,
                    base_profile=self.profile)
                for entry in candidates["wan22-720p"]:
                    profile = entry["profile"]
                    self.assertEqual(profile["recipe"], self.profile["recipe"])
                    self.assertEqual(profile["settings"]["vram_ballast_mib"], 4096)
                    self.assertEqual(profile["evidence"]["gpu_evidence"], "capacity-simulated")
        finally:
            bench._CALIBRATION_PROFILE.reset(token)
        catalog, _, _ = bench.load_catalog(ROOT)
        self.assertEqual(catalog.profiles["linux-wan22-720p-vram16"]["recipe"], "wan22-s2v-720p")

    def test_holder_readiness_and_release(self):
        class Process:
            returncode = None
            def poll(self): return self.returncode
            def send_signal(self, value): self.returncode = 0
            def wait(self, timeout=None): return self.returncode
            def kill(self): self.returncode = -9
        process = Process()
        def launch(command, stdout, **kwargs):
            stdout.write(json.dumps({"event": "ready", "allocated_bytes": 4096 * 1024**2}) + "\n")
            stdout.flush()
            return process
        entry = {"profile": self.profile, "path": "candidate.yaml"}
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(bench.subprocess, "Popen", side_effect=launch):
            with bench.attempt_ballast(ROOT, Path(temp), entry, "test"):
                self.assertIsNone(process.poll())
            self.assertEqual(process.poll(), 0)

    def test_holder_death_cannot_be_accepted(self):
        class Process:
            returncode = None
            def poll(self): return self.returncode
            def send_signal(self, value): self.returncode = 0
            def wait(self, timeout=None): return self.returncode
        process = Process()
        def launch(command, stdout, **kwargs):
            stdout.write(json.dumps({"event": "ready", "allocated_bytes": 4096 * 1024**2}) + "\n")
            stdout.flush()
            return process
        entry = {"profile": self.profile, "path": "candidate.yaml"}
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(bench.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, "exited during"):
                with bench.attempt_ballast(ROOT, Path(temp), entry, "test"):
                    process.returncode = 1

    def test_controller_dry_run_keeps_selected_recipe_and_vram_tier(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(ROOT / "calibration/calibrate.py"),
                "calibrate", "--root", str(ROOT), "--workspace", temp,
                "--scenario", "wan22-720p", "--run-id", "q4-plan-test", "--dry-run",
                "--base-profile-json", json.dumps(self.profile)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = Path(temp) / "results" / "q4-plan-test"
            plan = json.loads((run_dir / "calibration-plan.json").read_text())
            self.assertEqual(plan["base_profile_snapshot"]["recipe"], self.profile["recipe"])
            self.assertEqual(plan["base_profile_snapshot"]["settings"]["vram_ballast_mib"], 4096)
            metadata = json.loads((run_dir / "benchmark.json").read_text())
            self.assertEqual(metadata["pipeline_scope"], "main")


if __name__ == "__main__":
    unittest.main()
