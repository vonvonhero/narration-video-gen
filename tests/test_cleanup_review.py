"""Cleanup scope and safety regressions."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cleanup_models", ROOT / "scripts" / "cleanup-models.py")
CLEANUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLEANUP)


class CleanupReviewTests(unittest.TestCase):
    def make_root(self, directory):
        root = Path(directory)
        (root / "src/narration_video_gen").mkdir(parents=True)
        (root / "manifests").mkdir()
        (root / "src/narration_video_gen/__init__.py").write_text("")
        (root / "src/narration_video_gen/compat.py").write_text(
            "def load_yaml_file(path):\n"
            " import json\n"
            " return json.loads(path.read_text())\n")
        (root / "manifests/models.lock.yaml").write_text(json.dumps({
            "models": [
                {"path": "diffusion/model.bin"},
                {"path": "rife/model.pth"},
            ]
        }))
        return root

    def test_only_managed_models_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            managed = root / "models/diffusion/model.bin"
            managed.parent.mkdir(parents=True)
            managed.write_bytes(b"managed")
            tts = root / "models/irodori-tts-v4.1-small/cache/blob"
            tts.parent.mkdir(parents=True)
            tts.write_bytes(b"tts")
            custom = root / "models/custom/user-model.bin"
            custom.parent.mkdir(parents=True)
            custom.write_bytes(b"keep")
            with patch.object(CLEANUP, "active_operations", return_value=[]):
                result = CLEANUP.remove_models(root)
            self.assertGreater(result["bytes"], 0)
            self.assertFalse(managed.exists())
            self.assertFalse(tts.exists())
            self.assertEqual(custom.read_bytes(), b"keep")

    def test_active_generation_blocks_model_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            with patch.object(CLEANUP, "active_operations", return_value=[{"pid": 42}]):
                with self.assertRaises(RuntimeError):
                    CLEANUP.remove_models(root)

    def test_manifest_cannot_escape_models_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "manifests/models.lock.yaml").write_text(json.dumps({
                "models": [{"path": "../outside.bin"}]
            }))
            with self.assertRaises(ValueError):
                CLEANUP.model_targets(root)


if __name__ == "__main__":
    unittest.main()
