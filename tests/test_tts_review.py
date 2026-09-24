"""TTS regressions; set TTS_DOM_MODULES to a jsdom installation for UI tests."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from narration_video_gen import cli, narration, tts_service, tts_web


def write_audio(path, silent=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = [0 if silent else round(4000 * math.sin(2 * math.pi * n / 120))
               for n in range(narration.SAMPLE_RATE * 2)]
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, narration.SAMPLE_RATE, 0, "NONE", "not compressed"))
        target.writeframes(struct.pack("<%dh" % len(samples), *samples))


def character_fixture(root, name="source", seed=0):
    directory = root / "voices" / name
    write_audio(directory / "reference.wav")
    (directory / "portrait.png").write_bytes(b"portrait")
    narration.write_json_atomic(directory / "character.json", {
        "id": name, "label": name, "seed": seed, "caption": "落ち着いた声",
        "reference": {"path": "voices/%s/reference.wav" % name,
                      "designed_from": {"caption": "落ち着いた声", "seed": 0}},
        "portrait": {"path": "voices/%s/portrait.png" % name},
    })
    return directory


@contextlib.contextmanager
def web_server(root):
    app = tts_web.NarrationWebApp(root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield app, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class NarrationReviewTests(unittest.TestCase):
    def test_web_start_json_never_exposes_the_instance_token(self):
        args = cli.build_parser().parse_args([
            "--root", "/tmp/test-root", "tts", "web", "start",
            "--no-open", "--json"])
        output = io.StringIO()
        with patch.object(cli, "_start_tts_web", return_value={
                "running": True, "token": "private-instance-token",
                "urls": {"local": "http://127.0.0.1:7861", "lan": []},
                }), patch.object(tts_service, "models_prepared", return_value=True), \
                patch.object(tts_service, "backend_health",
                             return_value={"reachable": True}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.cmd_tts(args), cli.EXIT_OK)
        self.assertNotIn("private-instance-token", output.getvalue())
        self.assertNotIn("token", json.loads(output.getvalue()))

    def test_character_draft_is_hidden_until_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            character_fixture(root)
            draft = tts_service.create_user_character(
                root, "散歩葵", voice_from="source", draft=True)
            self.assertNotIn(draft["id"], tts_service.list_characters(root))
            self.assertIn(
                draft["id"], tts_service.list_characters(root, include_drafts=True))
            tts_service.commit_user_character(root, draft["id"])
            self.assertIn(draft["id"], tts_service.list_characters(root))
            # Retrying after a lost HTTP response is safe.
            self.assertEqual(
                tts_service.commit_user_character(root, draft["id"]), draft["id"])

    def test_video_input_library_tracks_real_adopted_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            character_fixture(root)
            job = root / "outputs" / "tts" / "saved-job"
            write_audio(job / "narration.wav")
            narration.write_json_atomic(job / "manifest.json", {
                "job_id": "saved-job", "status": "review",
                "created_at": "2026-09-17T10:00:00+0900",
                "script": "最初の行です。\n次の行です。", "character": "source",
                "character_label": "かんな", "purpose": "narration",
                "human_review": "passed", "parts": [{"text": "最初の行です。"}],
                "output": {"audio": "narration.wav", "sha256": "audio-hash",
                           "wav": {"duration_seconds": 12.5}},
            })
            adopted = tts_service.adopt_as_input_set(root, "saved-job")
            manifest = json.loads(
                (adopted / "tts-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["character_label"], "source")
            self.assertEqual(manifest["title"], "最初の行です。")
            self.assertEqual(manifest["duration_seconds"], 12.5)
            self.assertIn("adopted_at", manifest)

            inputs = tts_web.video_input_sets(root)
            self.assertEqual(len(inputs), 1)
            self.assertEqual(inputs[0]["input_set"], "tts-saved-job")
            self.assertEqual(inputs[0]["source_job"], "saved-job")
            self.assertTrue(inputs[0]["source_job_exists"])
            self.assertEqual(inputs[0]["script"], "最初の行です。\n次の行です。")
            recent = tts_web.recent_jobs(root)[0]
            self.assertEqual(recent["character"], "source")
            self.assertIn("動画用に保存済み", recent["state"])

            with patch.object(tts_service, "_video_generation_active",
                              return_value=True), self.assertRaises(
                                  tts_service.TTSError):
                tts_service.archive_adopted_input_set(root, "tts-saved-job")
            self.assertTrue(adopted.is_dir())
            archived = tts_service.archive_adopted_input_set(
                root, "tts-saved-job")
            self.assertTrue((archived / "audio.wav").is_file())
            self.assertFalse(adopted.exists())
            self.assertEqual(tts_web.recent_jobs(root)[0]["state"],
                             "確認済み・動画用には未保存")
            adopted = tts_service.adopt_as_input_set(root, "saved-job")
            self.assertTrue(adopted.is_dir())

            shutil.rmtree(job)
            inputs = tts_web.video_input_sets(root)
            self.assertEqual(len(inputs), 1)
            self.assertFalse(inputs[0]["source_job_exists"])

    def test_reviewed_job_is_not_called_adopted_without_an_input_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            character_fixture(root)
            job = root / "outputs" / "tts" / "reviewed-only"
            job.mkdir(parents=True)
            narration.write_json_atomic(job / "manifest.json", {
                "job_id": "reviewed-only", "status": "review",
                "script": "確認済みの台本", "character": "source",
                "character_label": "テスト", "purpose": "narration",
                "human_review": "passed", "parts": [{"text": "確認済みの台本"}],
            })
            for index in range(9):
                other = root / "outputs" / "tts" / ("z-aoi-%02d" % index)
                other.mkdir(parents=True)
                narration.write_json_atomic(other / "manifest.json", {
                    "job_id": other.name, "status": "review",
                    "script": "葵の台本%d" % index, "character": "aoi",
                    "purpose": "narration", "human_review": "pending",
                    "parts": [{"text": "葵の台本%d" % index}],
                })
            recent = tts_web.recent_jobs(root)
            item = next(entry for entry in recent
                        if entry["character"] == "source")
            self.assertEqual(item["state"], "確認済み・動画用には未保存")
            self.assertNotIn("採用ずみ", item["label"])
            self.assertEqual(item["script_preview"], "確認済みの台本")
            self.assertEqual(sum(entry["character"] == "aoi" for entry in recent), 8)

    def test_character_creation_copy_requires_confirmation_before_script(self):
        self.assertNotIn("そのまま台本を書いてください", tts_web._HTML)
        self.assertIn("キャラクターを確定すると、台本を書ける", tts_web._HTML)
        self.assertIn("$('card-script').hidden=true", tts_web._HTML)

    def test_config_exposes_drafts_only_as_pending_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            character_fixture(root)
            draft = tts_service.create_user_character(
                root, "作成途中", voice_from="source", draft=True)
            with patch.object(tts_service, "models_prepared", return_value=True), \
                    patch.object(tts_service, "backend_health",
                                 return_value={"reachable": True}), \
                    web_server(root) as (app, port):
                with app.character_draft_lock:
                    app.active_character_drafts.add(draft["id"])
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("GET", "/api/config")
                response = client.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertNotIn(draft["id"], body["characters"])
                self.assertIn(draft["id"], body["pending_characters"])
                self.assertTrue(body["pending_characters"][draft["id"]]["busy"])
                client.close()

                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("GET", "/character/%s/reference" % draft["id"])
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read().startswith(b"RIFF"))
                client.close()

                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("POST", "/api/characters/%s/commit" % draft["id"],
                               body=b"{}", headers={"X-NVG-CSRF": app.csrf})
                response = client.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                client.close()

    def test_adopted_input_audio_is_served_without_exposing_other_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "inputs" / "tts-ready"
            write_audio(directory / "audio.wav")
            (directory / "image.png").write_bytes(b"image")
            (directory / "script.txt").write_text("台本", encoding="utf-8")
            narration.write_json_atomic(directory / "tts-manifest.json", {
                "source_job": "gone", "character": "aoi",
            })
            linked = root / "inputs" / "linked-input"
            linked.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(tts_service.TTSError):
                tts_service.archive_adopted_input_set(root, "../outside")
            with self.assertRaises(tts_service.TTSError):
                tts_service.archive_adopted_input_set(root, "linked-input")
            secret = root / "secret.txt"
            secret.write_text("must not be exposed", encoding="utf-8")
            bad_script = root / "inputs" / "bad-script"
            write_audio(bad_script / "audio.wav")
            (bad_script / "image.png").write_bytes(b"image")
            (bad_script / "script.txt").symlink_to(secret)
            narration.write_json_atomic(bad_script / "tts-manifest.json", {
                "source_job": "gone", "character": "aoi",
            })
            bad_audio = root / "inputs" / "bad-audio"
            bad_audio.mkdir(parents=True)
            (bad_audio / "audio.wav").symlink_to(directory / "audio.wav")
            (bad_audio / "image.png").write_bytes(b"image")
            (bad_audio / "script.txt").write_text("台本", encoding="utf-8")
            narration.write_json_atomic(bad_audio / "tts-manifest.json", {
                "source_job": "gone", "character": "aoi",
            })
            malformed = root / "inputs" / "malformed"
            malformed.mkdir(parents=True)
            (malformed / "tts-manifest.json").write_text("[]", encoding="utf-8")
            (malformed / "script.txt").write_text("台本", encoding="utf-8")
            write_audio(malformed / "audio.wav")
            (malformed / "image.png").write_bytes(b"image")
            self.assertEqual(
                [item["input_set"] for item in tts_web.video_input_sets(root)],
                ["tts-ready"])
            with web_server(root) as (_app, port):
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("GET", "/input-media/tts-ready/audio",
                               headers={"Range": "bytes=0-3"})
                response = client.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"RIFF")
                client.close()

                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("GET", "/input-media/%2e%2e/audio")
                response = client.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                client.close()

                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("GET", "/input-media/bad-audio/audio")
                response = client.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                client.close()

                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("POST", "/api/input-sets/tts-ready/delete",
                               body=b"{}", headers={"X-NVG-CSRF": _app.csrf})
                response = client.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(body["deleted"], "tts-ready")
                self.assertTrue(body["recoverable"])
                client.close()
            self.assertFalse(directory.exists())
            archived = list((root / "outputs" / "tts" / "deleted-inputs").iterdir())
            self.assertEqual(len(archived), 1)
            self.assertTrue((archived[0] / "audio.wav").is_file())

    def test_deleting_narration_releases_its_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            character_fixture(root)
            job = root / "outputs" / "tts" / "made-audio"
            job.mkdir(parents=True)
            narration.write_json_atomic(job / "manifest.json", {
                "job_id": "made-audio", "status": "review",
                "character": "source", "purpose": "narration",
            })
            with self.assertRaises(tts_service.TTSError):
                tts_service.delete_user_character(root, "source")
            deleted = tts_service.delete_narration_job(root, "made-audio")
            self.assertEqual(deleted["character"], "source")
            self.assertFalse(job.exists())
            tts_service.delete_user_character(root, "source")

    def test_prepare_is_single_flight(self):
        tasks = tts_web.TaskStore()
        started = threading.Event()
        release = threading.Event()

        def prepare(_update):
            started.set()
            release.wait(3)
            return {"prepared": True}

        first = tasks.start("prepare", prepare, key="prepare-models")
        self.assertTrue(started.wait(1))
        second = tasks.start("prepare", prepare, key="prepare-models")
        self.assertEqual(first, second)
        release.set()
        for _attempt in range(100):
            if tasks.get(first).get("status") == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(tasks.get(first).get("status"), "completed")

    def fake_backend_script(self, root, body):
        script = root / "scripts" / "tts-backend.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        script.chmod(0o755)

    def test_backend_script_output_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fake_backend_script(
                root, "echo 'download failed: network unavailable' >&2\nexit 1\n")
            app = tts_web.NarrationWebApp(root)
            with self.assertRaises(tts_service.TTSError) as raised:
                app._script("prepare", "--device", "auto")
            log_path = root / "outputs" / "tts" / "prepare.log"
            self.assertIn("network unavailable", log_path.read_text(encoding="utf-8"))
            self.assertIn("outputs/tts/prepare.log", str(raised.exception))

    def test_backend_script_output_is_streamed_to_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fake_backend_script(
                root, "echo 'nvg-phase: build'\nsleep 1.3\necho 'nvg-phase: verify'\n")
            app = tts_web.NarrationWebApp(root)
            seen = []
            app._script("prepare", "--device", "auto", watch=seen.append)
            self.assertGreaterEqual(len(seen), 2)
            self.assertEqual("".join(seen), "nvg-phase: build\nnvg-phase: verify\n")
            log = (root / "outputs" / "tts" / "prepare.log").read_text(encoding="utf-8")
            self.assertIn("nvg-phase: verify", log)
            self.assertIn("completed", log)

    def test_broken_watch_does_not_stop_the_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fake_backend_script(root, "sleep 1.2\necho done\n")
            app = tts_web.NarrationWebApp(root)

            def broken(_output):
                raise RuntimeError("status failed")

            with contextlib.redirect_stderr(io.StringIO()):
                app._script("prepare", "--device", "auto", watch=broken)
            log = (root / "outputs" / "tts" / "prepare.log").read_text(encoding="utf-8")
            self.assertIn("done", log)

    def test_prepare_progress_reports_each_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifests").mkdir()
            (root / "manifests" / "tts-models.lock.yaml").write_text(
                "schema_version: 1\nmodels:\n"
                "  - id: a\n    bytes: 3000000\n"
                "  - id: b\n    bytes: 1000000\n", encoding="utf-8")
            now = [100.0]
            updates = []
            progress = tts_web.PrepareProgress(
                root, lambda message, fraction=None: updates.append((message, fraction)),
                clock=lambda: now[0])

            progress("nvg-phase: build\n#1 [internal] load build definition\n")
            self.assertIn("確認中", updates[-1][0])
            now[0] = 175.0
            progress("#7 [3/6] RUN git init .\n#8 [4/")
            self.assertIn("手順 3/6", updates[-1][0])
            self.assertIn("1分15秒", updates[-1][0])
            self.assertIsNone(updates[-1][1])
            progress("6] RUN uv sync\n")
            self.assertIn("手順 4/6", updates[-1][0])

            model_dir = root / tts_web.TTS_MODEL_DIR
            partial = model_dir / "checkpoint" / ".cache" / "model.safetensors.incomplete"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"x" * 1000000)
            xet = model_dir / "huggingface" / "xet" / "chunk"
            xet.parent.mkdir(parents=True)
            xet.write_bytes(b"x" * 1000000)
            progress("nvg-phase: download\n")
            message, fraction = updates[-1]
            self.assertIn("モデルをダウンロード中", message)
            self.assertIn("（25%）", message)
            self.assertAlmostEqual(fraction, 0.25, places=2)
            now[0] = 177.0
            partial.write_bytes(b"x" * 3000000)
            progress("\r 50%|####")
            message, fraction = updates[-1]
            self.assertAlmostEqual(fraction, 0.75, places=2)
            self.assertIn("1.0 MB/s", message)
            self.assertIn("残り1分未満", message)

            (model_dir / "codec").mkdir()
            (model_dir / "codec" / "weights.pth").write_bytes(b"x" * 2000000)
            progress("")
            self.assertEqual(updates[-1][1], 0.99)

            progress("\nnvg-phase: verify\n")
            self.assertIn("検証中", updates[-1][0])
            self.assertIsNone(updates[-1][1])

    def test_task_progress_is_reported_only_when_known(self):
        tasks = tts_web.TaskStore()
        release = threading.Event()

        def work(update):
            update("downloading", 0.5)
            release.wait(3)
            return {}

        task_id = tasks.start("work", work, key="prepare-models")
        for _attempt in range(100):
            if tasks.get(task_id).get("progress") == 0.5:
                break
            time.sleep(0.01)
        self.assertEqual(tasks.get(task_id)["progress"], 0.5)
        self.assertTrue(tasks.running("prepare-models"))
        tasks.update(task_id, "verifying")
        self.assertNotIn("progress", tasks.get(task_id))
        release.set()
        for _attempt in range(100):
            if not tasks.running("prepare-models"):
                break
            time.sleep(0.01)
        self.assertFalse(tasks.running("prepare-models"))

    def test_unknown_error_keeps_a_location_in_the_log(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            try:
                raise RuntimeError("private request text")
            except RuntimeError as exc:
                message = tts_web._public_error(exc)
        self.assertIn("RuntimeError at test_tts_review.py:", output.getvalue())
        self.assertNotIn("private request text", output.getvalue() + message)

    def test_connective_does_not_shorten_paragraph_pause(self):
        parts = narration.split_script("最初の段落です。\n\nそして、次の段落です。")
        self.assertTrue(parts[0].paragraph_end)
        self.assertEqual(parts[0].gap_after_ms, 820)

    def test_silent_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "silent.wav"
            write_audio(wav, silent=True)
            self.assertTrue(tts_service.inspect_reference_audio(wav)["problems"])

    def test_inherited_user_voice_survives_parent_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = character_fixture(root)
            child = tts_service.create_user_character(root, "child", voice_from="source")
            entry = tts_service.list_characters(root)[child["id"]]
            self.assertEqual(entry["seed"], 0)
            self.assertNotEqual(entry["reference"], "voices/source/reference.wav")
            self.assertNotEqual(entry["portrait"], "voices/source/portrait.png")
            original_audio = (root / entry["reference"]).read_bytes()
            (source / "reference.wav").write_bytes(b"changed")
            replacement = root / "new.png"
            replacement.write_bytes(b"new portrait")
            tts_service.replace_character_portrait(root, "source", replacement)
            tts_service.delete_user_character(root, "source")
            self.assertIn(child["id"], tts_service.list_characters(root))
            self.assertEqual((root / entry["reference"]).read_bytes(), original_audio)
            self.assertEqual((root / entry["portrait"]).read_bytes(), b"portrait")

    def test_legacy_shared_files_cannot_be_removed_or_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = character_fixture(root)
            child = character_fixture(root, "legacy")
            record = json.loads((child / "character.json").read_text())
            record["reference"]["path"] = "voices/source/reference.wav"
            record["portrait"]["path"] = "voices/source/portrait.png"
            narration.write_json_atomic(child / "character.json", record)
            with self.assertRaises(tts_service.TTSError):
                tts_service.delete_user_character(root, "source")
            with self.assertRaises(tts_service.TTSError), patch.object(
                    tts_service, "design_reference_wav") as generate:
                tts_service.redesign_character_voice(root, "source")
            generate.assert_not_called()
            with self.assertRaises(tts_service.TTSError):
                tts_service.replace_character_portrait(root, "source", source / "portrait.png")
            self.assertEqual((source / "portrait.png").read_bytes(), b"portrait")

    def test_design_seed_is_exposed_separately_from_narration_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            character_fixture(root, seed=123)
            entry = tts_service.list_characters(root)["source"]
            self.assertEqual(entry["seed"], 123)
            self.assertEqual(entry["design_seed"], 0)

    def test_design_seed_zero_reaches_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = tts_web.NarrationWebApp(tmp)
            with patch.object(tts_service, "models_prepared", return_value=True), \
                    patch.object(tts_service, "backend_health", return_value={"reachable": True}), \
                    patch.object(tts_service, "design_reference_wav") as design, \
                    patch.object(tts_service, "create_user_character", return_value={"id": "test"}), \
                    patch.object(tts_service, "generate_narration", return_value=({"job_id": "test"}, None)):
                app._create_character({"mode": "design", "design_seed": 0,
                                       "caption": "自然な声"}, lambda _text: None)
            self.assertEqual(design.call_args.args[2], 0)

    def test_invalid_outro_does_not_start_generation(self):
        app = tts_web.NarrationWebApp("/tmp")
        for value in (-1, 5001, 3.5, None):
            with self.subTest(value=value), patch.object(
                    tts_service, "generate_narration") as generate:
                with self.assertRaises(ValueError):
                    app._generate({"script": "テスト", "outro_ms": value}, lambda _: None)
                generate.assert_not_called()

    def test_user_label_cannot_close_page_script(self):
        label = '</script><script>window.injected=true</script>'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = character_fixture(root)
            record = json.loads((directory / "character.json").read_text())
            record["label"] = label
            narration.write_json_atomic(directory / "character.json", record)
            with web_server(root) as (_app, port):
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                client.request("GET", "/")
                response = client.getresponse()
                html = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertEqual(html.count("</script>"), 1)
                self.assertNotIn(label, html)
                self.assertEqual(json.loads(tts_web._script_json(label)), label)
                client.close()

    def test_negative_request_lengths_are_rejected_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp, web_server(tmp) as (app, port):
            for path in ("/api/segment", "/api/uploads"):
                with self.subTest(path=path):
                    client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    client.request("POST", path, headers={
                        "Content-Length": "-1", "X-NVG-CSRF": app.csrf})
                    response = client.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertIn("error", json.loads(response.read()))
                    client.close()

    def test_json_request_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as tmp, web_server(tmp) as (app, port):
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            client.request("POST", "/api/segment", body=b"[]", headers={"X-NVG-CSRF": app.csrf})
            response = client.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn("error", json.loads(response.read()))
            client.close()

    @unittest.skipUnless(os.environ.get("TTS_DOM_MODULES") and shutil.which("node"),
                         "set TTS_DOM_MODULES to a jsdom installation for UI tests")
    def test_browser_workflows(self):
        html = tts_web._HTML.replace("__CSRF__", '"test"').replace(
            "__DEFAULTS__", tts_web._script_json({
                "aoi": {"label_ja": "葵", "caption": "落ち着いた声", "seed": 1},
                "designed": {"label_ja": "自作", "caption": "自然な声", "seed": 99,
                             "source": "user", "designed": True, "design_seed": 0},
            })).replace("__CAPTION__", '"自然な声"').replace("__DESIGN__", '"自然な声"')
        result = subprocess.run(["node", "-e", DOM_TESTS], input=html, text=True,
                                capture_output=True, timeout=30,
                                env={**os.environ, "NODE_PATH": os.environ["TTS_DOM_MODULES"]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


DOM_TESTS = r"""
const {JSDOM} = require('jsdom');
const assert = require('node:assert/strict');
const html = require('node:fs').readFileSync(0, 'utf8');
let characters, healthCalls = 0, starts = 0;
const job = {job_id:'saved',status:'review',script:'保存した台本',character:'designed',
  caption:'保存した話し方',outro_ms:2700,seed:99,human_review:'pending',
  output:{sha256:'test',wav:{duration_seconds:4}},
  parts:[{index:1,text:'保存した台本',audio:'parts/part_001.wav',seed:99,initial_seed:99,
    gap_after_ms:0,asr:{status:'pass'}}]};
const dom = new JSDOM(html,{runScripts:'dangerously',url:'http://localhost/',
  beforeParse(w){
    w.HTMLElement.prototype.scrollIntoView=function(){};
    w.fetch=async(path,request={})=>{
      let value={};
      if(path==='/api/config'){
        healthCalls++;
        characters=characters||JSON.parse(JSON.stringify(w.eval('defaults')));
        value={model_prepared:true,backend:{reachable:true},characters};
      }else if(path==='/api/jobs')value={jobs:[
        {job_id:'aoi-history',character:'aoi',title:'葵履歴',script_preview:'葵の台本',label:'葵 / 未確認'},
        {job_id:'designed-history',character:'designed',title:'自作履歴',script_preview:'自作の台本',label:'自作 / 未確認'}],input_sets:[{
        input_set:'tts-saved',source_job:'saved',source_job_exists:true,
        character:'aoi',character_label:'葵',title:'保存した台本',
        script:'保存した台本',duration_seconds:4,
        adopted_at:'2026-09-17T10:00:00+09:00'}]};
      else if(path==='/api/segment')value={parts:[]};
      else if(path==='/api/jobs/saved')value=job;
      else if(path==='/api/characters/designed/voice')value={task_id:'voice'};
      else if(path==='/api/tasks/voice')value={status:'completed',result:{
        id:'designed',label:'自作',seed:0,test:job,advice:[]}};
      else throw Error('Unexpected API '+path);
      return {ok:true,json:async()=>value};
    };
  }});
const w=dom.window, $=id=>w.document.getElementById(id);
(async()=>{
  await new Promise(resolve=>w.setTimeout(resolve,0));
  assert(!$('card-library').hidden,'adopted input library is visible');
  assert($('library').textContent.includes('inputs/tts-saved'),'run input name is visible');
  w.document.querySelector('[data-delete-input="tts-saved"]').click();
  assert($('library').textContent.includes('動画生成の選択肢から外します'),
    'deleting a video input requires inline confirmation');
  w.document.querySelector('.input-delete-no').click();
  assert(!$('library').textContent.includes('動画生成の選択肢から外します'),
    'cancel keeps the input card without confirmation');
  assert($('recent').textContent.includes('葵の台本'),'selected character history preview is visible');
  assert(!$('recent').textContent.includes('自作の台本'),'another character history is hidden');
  w.eval("character='designed';renderCharacters()");
  assert($('library').textContent.includes('まだ動画に使える音声・台本は作られていません'),
    'selected character with no input has an explicit empty state');
  assert(!$('library').textContent.includes('inputs/tts-saved'),
    'another character input is hidden');
  assert($('recent').textContent.includes('自作の台本'),'history follows selected character');
  assert(!$('recent').textContent.includes('葵の台本'),'previous character history is hidden');
  w.eval("character='aoi';renderCharacters()");
  w.eval('renderResult('+JSON.stringify(job)+')');
  w.eval('setBusy(true)');
  assert($('script').disabled,'script cannot change during generation');
  assert(w.document.querySelector('[data-char="designed"]').disabled,'character switch locked');
  assert($('new-character').disabled,'new character locked');
  w.eval('renderCharacters()');
  assert($('new-character').disabled,'redrawn controls remain locked');
  w.eval('setBusy(false)');
  assert(!$('script').disabled,'script unlocked after generation');
  assert(w.document.querySelector('.p-seed-reset').disabled,'unchanged seed reset stays disabled');
  await w.openJob('saved');
  assert.equal($('outro').value,'2700','saved ending pause restored');
  assert.equal($('script').value,'保存した台本','saved script restored');
  assert.equal($('caption').value,'保存した話し方','saved speaking style restored');
  w.openRedesign();
  assert.equal($('a-seed').value,'0','design seed shown instead of narration seed');
  await w.redesign();
  assert($('a-test').querySelector('audio'),'audition survives character refresh');
  assert.equal($('a-seed').value,'0');
  w.openMaker();
  assert($('card-script').hidden,'script step hides while character creation is open');
  w.renderMade({id:'designed',label:'自作',seed:0,test:job});
  assert($('card-script').hidden,'script stays hidden before character confirmation');
  assert($('m-create').closest('.maker').hidden,'creation form hides after a character is made');
  w.makerStatus('err','retry failed');
  assert.equal($('m-preview-status').textContent,'retry failed','audition errors remain visible');
  await w.eval(`work('準備中',async say=>{
    say('モデルをダウンロード中: 25%',0.25);
    const bar=$('setup-action').querySelector('progress');
    if(!bar||bar.value!==0.25)throw Error('download progress bar is missing');
    say('ダウンロードしたモデルを検証中');
    if($('setup-action').querySelector('progress'))throw Error('stale progress bar')})`);
  const previousCalls=healthCalls;
  await w.ensureReady(()=>{});
  assert(healthCalls>previousCalls,'engine health is refreshed before reuse');
  w.renderResult({...job,asr:{status:'error'}});
  assert($('result').textContent.includes('照合に失敗'),'failed transcript check visible');
  w.renderResult({...job,status:'failed',output:undefined});
  assert(!$('adopt'),'failed generation cannot be adopted');
  assert(!$('result').querySelector('audio'),'failed generation has no missing audio player');
  dom.window.close();
  console.log('Browser workflow regressions passed');
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1});
"""


if __name__ == "__main__":
    unittest.main()
