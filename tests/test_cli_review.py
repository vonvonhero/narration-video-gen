"""User-facing CLI regressions; run with python3 tests/test_cli_review.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from narration_video_gen import cli, run_state, runner
from narration_video_gen.catalog import Catalog


def write_wav(path, seconds=1):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * (16000 * seconds))


class Terminal(io.StringIO):
    def isatty(self):
        return True


class CliReviewTests(unittest.TestCase):
    def test_completed_wsl_video_is_exported_to_windows_videos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            source = root / "outputs" / "run-1" / "05-retime-audio.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"finished video")
            videos = Path(directory) / "windows-videos"
            with patch.object(cli, "_windows_videos_paths", return_value=(
                    videos, cli.PureWindowsPath(r"C:\Users\example\Videos"))):
                exported = cli._export_windows_video(
                    root, "run-1", "outputs/run-1/05-retime-audio.mp4",
                    "windows-wsl2")
            destination = videos / "Narration Video Gen" / "run-1" / "video.mp4"
            self.assertEqual(destination.read_bytes(), b"finished video")
            self.assertEqual(
                exported["windows_path"],
                r"C:\Users\example\Videos\Narration Video Gen\run-1\video.mp4")
            self.assertEqual(exported["bytes"], len(b"finished video"))
            self.assertEqual(list(destination.parent.glob("*.partial")), [])

    def test_completed_video_is_not_exported_on_native_linux(self):
        with patch.object(cli, "_windows_videos_paths") as videos:
            self.assertIsNone(cli._export_windows_video(
                Path("/repo"), "run-1", "outputs/run-1/video.mp4", "linux"))
            videos.assert_not_called()

    def test_guided_dry_run_never_launches_generation(self):
        catalog = Catalog(ROOT)
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            portrait, audio = root / "portrait.png", root / "audio.wav"
            portrait.write_bytes(b"portrait fixture")
            write_wav(audio, 10)
            args = cli.build_parser().parse_args([
                "--root", str(root), "run", "--profile", "linux-wan21-480p-vram16",
                "--dry-run", "--length", "short"])
            stack.enter_context(patch.object(cli, "Catalog", return_value=catalog))
            stack.enter_context(patch.object(cli, "_env_for", return_value={}))
            stack.enter_context(patch.object(cli, "evaluate", return_value=(True, [], [])))
            stack.enter_context(patch.object(cli.plan_mod, "build_plan", return_value={
                "downloads": {"missing_gib": 42}, "runtime_image": {"present": False}}))
            stack.enter_context(patch.object(cli, "_choose_input_set", return_value=(str(portrait), str(audio))))
            stack.enter_context(patch.object(runner, "resolve_models", return_value={}))
            stack.enter_context(patch.object(runner, "pipeline_for", return_value=["infinitetalk"]))
            stack.enter_context(patch.object(runner, "build_stage", return_value=({"preview": True}, "output")))
            background = stack.enter_context(patch.object(cli, "_launch_background"))
            release = stack.enter_context(patch.object(cli, "_release_tts_backend_for_video"))
            confirm = stack.enter_context(patch.object(cli, "_confirm"))
            client = stack.enter_context(patch.object(runner, "ComfyClient"))
            output = stack.enter_context(patch.object(cli.sys, "stdout", Terminal()))
            stack.enter_context(patch.object(cli.sys, "stdin", Terminal()))
            self.assertEqual(cli.cmd_run(args), cli.EXIT_OK)
            self.assertIn('"preview": true', output.getvalue())
            background.assert_not_called()
            release.assert_not_called()
            confirm.assert_not_called()
            client.return_value.submit.assert_not_called()
            self.assertIsNone(run_state.latest(root))

    def test_invalid_audio_is_not_the_default_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("good", "broken", "empty"):
                target = root / "inputs" / name
                target.mkdir(parents=True)
                (target / "portrait.png").write_bytes(b"portrait fixture")
                if name == "broken":
                    (target / "audio.wav").write_bytes(b"RIFF")
                else:
                    write_wav(target / "audio.wav", 0 if name == "empty" else 1)
            candidates = cli._input_set_candidates(root)
            self.assertEqual(candidates[0]["directory"].name, "good")
            self.assertTrue(candidates[0]["latest"])
            self.assertEqual({c["issues"][0] for c in candidates[1:]},
                             {"invalid WAV audio", "empty WAV audio"})
            for seconds in (0, -1, float("nan"), float("inf")):
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    cli._choose_run_length({"fps": 16}, seconds, requested="full")

    def test_mask_only_records_completion_and_skips_later_stages(self):
        catalog = Catalog(ROOT)
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            args = cli.build_parser().parse_args([
                "--root", str(root), "run", "--profile", "linux-wan21-480p-vram16",
                "--mask-only", "--stages", "face-detailer,rife,retime",
                "--source-video", "source.mp4", "--run-id", "mask-test"])
            stack.enter_context(patch.object(cli, "Catalog", return_value=catalog))
            stack.enter_context(patch.object(cli, "_env_for", return_value={}))
            stack.enter_context(patch.object(cli, "evaluate", return_value=(True, [], [])))
            stack.enter_context(patch.object(cli, "_stage_input", return_value="/input/source.mp4"))
            stack.enter_context(patch.object(cli.verify_mod, "verify_output", return_value={"frames": 89}))
            stack.enter_context(patch.object(cli, "_release_tts_backend_for_video", return_value=True))
            stack.enter_context(patch.object(cli, "_local_eta_state", return_value={}))
            stack.enter_context(patch.object(runner, "resolve_models", return_value={}))
            build = stack.enter_context(patch.object(runner, "build_stage", return_value=({}, "output")))
            stack.enter_context(patch.object(runner, "ComfyClient"))
            stack.enter_context(patch.object(runner, "outputs_of", return_value=["mask-test/mask.mp4", "mask-test/mask-audio.mp4"]))
            stack.enter_context(patch.object(runner, "ensure_audio_companion", return_value={}))
            stack.enter_context(patch.object(runner, "probe_container_video", return_value=(89, 16)))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(cli.cmd_run(args), cli.EXIT_OK)
            build.assert_called_once()
            state = run_state.load(root, "mask-test")
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["stages"], ["face-detailer"])
            self.assertEqual(state["output"], "outputs/mask-test/mask-audio.mp4")

    def test_invalid_ids_and_numeric_arguments_fail_before_work(self):
        for command in (["plan", "--seconds", "nan"], ["plan", "--seconds", "-1"],
                        ["run", "--frames", "0"], ["verify", "video.mp4", "--frames", "-5"],
                        ["run", "--run-id", "../other"], ["report", "--run-id", "/tmp/out"]):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.build_parser().parse_args(command)
                self.assertEqual(raised.exception.code, 2)

    def test_run_history_ignores_malformed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = run_state.write(root, "good", status="completed")
            for name, content in (("array", "[]"), ("null", "null"),
                                  ("broken", "{"), ("mismatch", '{"run_id":"../other"}')):
                path = root / "outputs" / name / "run-state.json"
                path.parent.mkdir(parents=True)
                path.write_text(content)
            self.assertEqual(run_state.latest(root), good)
            for invalid in ("../other", "/tmp/out", "", "nested/run", "..", "x" * 129):
                with self.subTest(run_id=invalid), self.assertRaises(ValueError):
                    run_state.write(root, invalid, status="starting")

    def test_json_options_and_errors_are_parseable(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = (["--json", "list"], ["list", "--json"],
                        ["tts", "plan", "--text", "Hello.", "--json"],
                        ["matrix", "--json"],
                        ["--root", directory, "status", "--json"],
                        ["--root", directory, "cancel", "--json"],
                        ["tts", "plan", "--json"], ["plan", "--seconds", "nan", "--json"])
            for command in commands:
                with self.subTest(command=command):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                        code = cli.main(command)
                    parsed = json.loads(output.getvalue())
                    self.assertIsInstance(parsed, (dict, list))
                    if "cancel" in command or command[-3:-1] == ["--seconds", "nan"]:
                        self.assertNotEqual(code, 0)

    def test_calibration_searches_the_selected_plan_on_the_real_gpu(self):
        env = {"platform": "linux",
               "gpus": [{"name": "NVIDIA Test GPU", "uuid": "GPU-test",
                         "vram_mib": 8188, "vram_gib": 8.0}]}
        catalog = Catalog(ROOT)
        for profile_id in ("linux-wan22-720p-vram8-simulated",
                           "linux-wan22-480p-vram8-simulated"):
            with self.subTest(profile=profile_id), \
                    patch.object(cli, "_env_for", return_value=env), \
                    patch.object(cli.plan_mod, "runtime_image_plan",
                                 return_value={"present": True, "image": "test-image"}), \
                    patch.object(cli, "_calibration_container_environment", return_value=[]), \
                    patch.object(cli.run_state, "latest", return_value=None):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    code = cli.main(["--json", "calibrate", "--profile", profile_id, "--dry-run"])
                self.assertEqual(code, 0)
                command = json.loads(output.getvalue())["command"]
                snapshot = json.loads(command[command.index("--base-profile-json") + 1])
                self.assertEqual(snapshot["recipe"], catalog.profiles[profile_id]["recipe"])
                self.assertNotIn("vram_ballast_mib", snapshot["settings"])
                self.assertNotIn("vram_ballast_device_index", snapshot["settings"])

    def test_json_run_keeps_progress_out_of_the_result(self):
        args = cli.build_parser().parse_args(["run", "--json", "--dry-run"])
        def pipeline(_args, result):
            print("progress message")
            result["workflows"] = [{"stage": "infinitetalk", "workflow": {"sample": True}}]
            return cli.EXIT_OK
        output, log = io.StringIO(), io.StringIO()
        with patch.object(cli, "_run_pipeline", side_effect=pipeline), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(log):
            self.assertEqual(cli.cmd_run(args), cli.EXIT_OK)
        self.assertEqual(json.loads(output.getvalue())["status"], "planned")
        self.assertEqual(log.getvalue(), "progress message\n")

    def test_standard_plan_uses_defaults_without_extra_prompts(self):
        catalog = Catalog(ROOT)
        profile = catalog.profiles["linux-wan21-480p-vram16"]
        with tempfile.TemporaryDirectory() as directory:
            args = cli.build_parser().parse_args(["--root", directory, "plan"])
            with patch.object(cli, "_prompt_choice", side_effect=AssertionError("unexpected prompt")):
                choices = cli._choose_plan_recipe_options(catalog, profile, args, interactive=True)
            self.assertEqual(choices, ["face-detailer-on"])
            args.advanced = True
            with patch.object(cli, "_prompt_choice", side_effect=[1, 1]) as prompt:
                choices = cli._choose_plan_recipe_options(catalog, profile, args, interactive=True)
            self.assertEqual(prompt.call_count, 2)
            self.assertEqual(choices, ["musetalk", "face-detailer-off"])

    def test_plan_downloads_a_small_missing_model(self):
        catalog = Catalog(ROOT)
        profile = catalog.profiles["linux-wan21-480p-vram16"]
        recipe = catalog.recipe_for(profile)
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            args = cli.build_parser().parse_args([
                "--root", directory, "plan", "--profile", profile["id"]])
            stack.enter_context(patch.object(cli, "Catalog", return_value=catalog))
            stack.enter_context(patch.object(cli, "_env_for", return_value={}))
            stack.enter_context(patch.object(cli, "evaluate", return_value=(True, [], [])))
            payload = {"pipeline_stages": recipe["pipeline_stages"],
                       "downloads": {"missing_bytes": 1024, "missing_gib": 0.0},
                       "runtime_image": {"present": True}, "runtime_estimate": {},
                       "disk": {"sufficient": True, "free_gib": 30}}
            prepared = dict(payload, downloads={"missing_bytes": 0, "missing_gib": 0.0})
            stack.enter_context(patch.object(cli.plan_mod, "build_plan", side_effect=[payload, prepared]))
            stack.enter_context(patch.object(cli, "_confirm", return_value=True))
            command = stack.enter_context(patch.object(cli.subprocess, "run"))
            stack.enter_context(patch.object(cli.sys, "stdout", Terminal()))
            stack.enter_context(patch.object(cli.sys, "stdin", Terminal()))
            self.assertEqual(cli.cmd_plan(args), cli.EXIT_OK)
            command.assert_called_once_with([
                str(root / "scripts/download-models.sh"), "--profile", profile["id"]], check=True)

    def test_live_disk_shortage_makes_plan_check_fail(self):
        catalog = Catalog(ROOT)
        profile = catalog.profiles["linux-wan21-480p-vram16"]
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            args = cli.build_parser().parse_args([
                "--root", directory, "plan", "--profile", profile["id"], "--json"])
            stack.enter_context(patch.object(cli, "Catalog", return_value=catalog))
            stack.enter_context(patch.object(cli, "_env_for", return_value={}))
            stack.enter_context(patch.object(cli, "evaluate", return_value=(True, [], [])))
            stack.enter_context(patch.object(cli.plan_mod, "build_plan", return_value={
                "disk": {"sufficient": False, "required_free_gib": 60, "free_gib": 59}}))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(cli.cmd_plan(args), cli.EXIT_NO_MATCH)
            result = json.loads(output.getvalue())
            self.assertFalse(result["eligible"])
            self.assertTrue(result["blockers"])


if __name__ == "__main__":
    unittest.main()
