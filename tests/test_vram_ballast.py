"""Read-only ballast configuration tests: no torch import or CUDA allocation."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vram_ballast", ROOT / "scripts/hold-vram-ballast.py")
BALLAST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BALLAST)


class BallastConfigurationTests(unittest.TestCase):
    def read(self, settings):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.yaml"
            path.write_text(yaml.safe_dump({"id": "test", "settings": settings}), encoding="utf-8")
            return BALLAST.read_settings(path)

    def test_exact_uint8_byte_budgets(self):
        for mib in (4096, 8192):
            config = self.read({"vram_ballast_mib": mib})
            self.assertEqual(config["bytes"], mib * 1024 * 1024)
            self.assertEqual(config["device_index"], 0)

    def test_invalid_ballast_is_refused(self):
        for value in (None, True, "8192", 0, 4095, 16384):
            with self.assertRaises(ValueError):
                self.read({"vram_ballast_mib": value})

    def test_invalid_device_is_refused(self):
        for value in (-1, True, "0"):
            with self.assertRaises(ValueError):
                self.read({"vram_ballast_mib": 4096, "vram_ballast_device_index": value})

    def test_published_ballast_profiles_are_readable_and_simulated(self):
        paths = sorted((ROOT / "profiles").rglob("*.yaml"))
        ballasted = []
        for path in paths:
            profile = yaml.safe_load(path.read_text(encoding="utf-8"))
            if "vram_ballast_mib" not in (profile.get("settings") or {}):
                continue
            ballasted.append(path.name)
            config = BALLAST.read_settings(path)
            self.assertEqual(config["bytes"],
                             profile["settings"]["vram_ballast_mib"] * 1024 * 1024)
            self.assertEqual(profile["evidence"]["gpu_evidence"], "capacity-simulated")
            self.assertIn("simulated", profile["id"])
        self.assertTrue(ballasted)

if __name__ == "__main__":
    unittest.main()
