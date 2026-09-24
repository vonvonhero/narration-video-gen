"""CPU-only checks against the helper in an applied, pinned wrapper patch.

Run in the existing image with the patched sampler source path as argument.
This does not import the sampler, load weights, or execute GPU generation.
"""
import ast
import logging
from pathlib import Path
import sys
import unittest

try:
    import torch
except ModuleNotFoundError:
    torch = None

SAMPLER_PATH = None


def load_check(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    helpers = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)
               and node.name == "check_framepack_tensor"]
    if len(helpers) != 1:
        raise AssertionError("expected one applied FramePack check helper")
    module = ast.Module(body=helpers, type_ignores=[])
    namespace = {"torch": torch, "log": logging.getLogger(__name__)}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["check_framepack_tensor"]


class FramePackDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if torch is None or SAMPLER_PATH is None:
            raise unittest.SkipTest("needs CPU PyTorch and an applied sampler source")
        cls.check_tensor = staticmethod(load_check(SAMPLER_PATH))

    def test_six_finite_windows(self):
        for window in range(6):
            for phase in ("motion latent", "sampled latent", "decoded video"):
                self.check_tensor(torch.tensor([-0.25, 0.5]), phase, window)

    def test_nan_and_inf_identify_window_and_phase(self):
        for invalid in (float("nan"), float("inf"), -float("inf")):
            for phase in ("motion latent", "sampled latent", "decoded video"):
                with self.assertRaisesRegex(RuntimeError,
                                            "window 5: non-finite " + phase):
                    self.check_tensor(torch.tensor([0.0, invalid]), phase, 4)

    def test_constant_decoded_window_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError,
                                    "window 5: spatially and temporally constant"):
            self.check_tensor(torch.zeros(2, 3, 4), "decoded video", 4)

    def test_constant_motion_latent_is_not_rejected_as_video(self):
        self.check_tensor(torch.zeros(2), "motion latent", 0)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        SAMPLER_PATH = sys.argv.pop(1)
    unittest.main()
