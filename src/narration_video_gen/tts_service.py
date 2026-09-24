"""Orchestration client for the pinned, localhost-only Irodori-TTS backend."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import wave
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import narration
from . import run_state

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no advisory file locks
    fcntl = None


MODEL_ID = "Aratako/Irodori-TTS-v4.1-Small"
MODEL_REVISION = "2b28324dc263ed5e6638b3cf3dd94c82ead07b4b"
CODEC_ID = "Aratako/Semantic-DACVAE-Japanese-32dim"
CODEC_REVISION = "47376ee24834d7a05a48ebabfe3cde29b3c5e214"
SERVER_COMMIT = "841fb7c6ec57729c56b9b75c0ef2562249b13a10"
IRODORI_COMMIT = "8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71"
DEFAULT_SERVER = "http://127.0.0.1:8088"


class TTSError(RuntimeError):
    pass


def _local_server(value):
    value = value if "://" in value else "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1", "localhost", "::1"}:
        raise TTSError("TTS backend must be a local HTTP address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TTSError("invalid TTS backend address")
    return value.rstrip("/")


def _request(server, method, path, payload=None, timeout=30):
    url = _local_server(server) + path
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise TTSError("Irodori-TTS returned HTTP %d: %s" %
                       (exc.code, detail[:500])) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TTSError("cannot reach the local Irodori-TTS backend at %s: %s" %
                       (server, exc)) from exc


def backend_health(server=DEFAULT_SERVER):
    try:
        body, _headers = _request(server, "GET", "/health", timeout=3)
        return {"reachable": True, "details": json.loads(body.decode("utf-8"))}
    except (TTSError, ValueError) as exc:
        return {"reachable": False, "error": str(exc)}


def model_state_path(root):
    return Path(root) / "outputs" / "tts" / ".model-preparation.json"


def models_prepared(root):
    paths = [
        model_state_path(root),
        # Preserve installations prepared before the state was renamed.
        Path(root) / "outputs" / "tts" / ".model-acceptance.json",
    ]
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            continue
        if data.get("model") == MODEL_ID and data.get("revision") == MODEL_REVISION:
            return True
    return False


def record_models_prepared(root):
    payload = {
        "schema_version": 1,
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "license": "MIT",
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    narration.write_json_atomic(model_state_path(root), payload)
    return payload


def job_root(root, job_id):
    if not job_id or not all(ch.isalnum() or ch in "-_" for ch in job_id):
        raise TTSError("invalid narration job id")
    return Path(root) / "outputs" / "tts" / job_id


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _thread_lock(key):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def job_lock(root, job_id, timeout=600):
    """Serialise every write to one narration job.

    The review UI runs each generation, regeneration and rejoin on its own
    thread, and the CLI can be pointed at the same job at the same time.
    Without this, two writers each read the manifest, each rewrite the whole
    file, and one part's new seed, hashes and audio silently disappear while
    the joined WAV is assembled from a mixture of both.
    """
    directory = job_root(root, job_id)
    guard = _thread_lock(str(directory))
    deadline = time.monotonic() + timeout
    if not guard.acquire(timeout=timeout):
        raise TTSError("narration job %s is busy; try again in a moment" % job_id)
    handle = None
    try:
        if fcntl is not None and directory.is_dir():
            handle = (directory / ".lock").open("a+")
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TTSError(
                            "narration job %s is busy; try again in a moment"
                            % job_id)
                    time.sleep(0.2)
        yield directory
    finally:
        if handle is not None:
            handle.close()
        guard.release()


def _part_audio_paths(directory, parts):
    """Resolve every part WAV, refusing a job that never finished generating."""
    paths = []
    for index, item in enumerate(parts, start=1):
        name = item.get("audio")
        if not name:
            raise TTSError(
                "narration part %d has no audio; generate the narration again"
                % index)
        path = directory / name
        if not path.is_file():
            raise TTSError("narration part %d audio is missing: %s" % (index, path))
        paths.append(path)
    return paths


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# The recorded v3 study fitted natural narration length to
# `mora_count * 0.145 + 0.3` seconds.  Visible characters under-count mora for
# kanji, so the estimate is a floor and only a generous overshoot is reported.
NATURAL_SECONDS_PER_CHARACTER = 0.145
NATURAL_SECONDS_OFFSET = 0.3
LONG_AUDIO_RATIO = 1.5
LEGACY_SECONDS_PER_CHARACTER_LIMIT = 0.28


def natural_seconds(visible_characters):
    return visible_characters * NATURAL_SECONDS_PER_CHARACTER + NATURAL_SECONDS_OFFSET


def _part_quality(text, wav_info):
    units = max(1, narration._visible_length(text))
    seconds = wav_info.get(
        "cleaned_duration_seconds", wav_info["duration_seconds"])
    expected = natural_seconds(units)
    ratio = seconds / expected if expected else 0.0
    warning_threshold = max(
        units * LEGACY_SECONDS_PER_CHARACTER_LIMIT,
        expected * LONG_AUDIO_RATIO,
    )
    issues = list(wav_info["issues"])
    # The natural-duration intercept prevents false warnings on one- or two-word
    # parts.  Keeping the previous 0.28 s/character floor avoids making longer,
    # kanji-heavy parts stricter before there is enough measured evidence.
    if units <= 40 and seconds > warning_threshold:
        issues.append("short text has unusually long audio; check for extra speech")
    return {
        "status": "review" if issues else "pass",
        "visible_characters": units,
        "seconds_per_visible_character": seconds / units,
        "expected_seconds": expected,
        "duration_ratio": ratio,
        "warning_threshold_seconds": warning_threshold,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Characters the user creates locally: one portrait plus one reference WAV.
# Bundled characters stay in assets/ and are never modified from the UI.
# ---------------------------------------------------------------------------

USER_CHARACTER_DIRNAME = "voices"
RUNTIME_VOICES_DIRNAME = ".voices"
DEFAULT_USER_CAPTION = "自然なテンポで、明瞭に落ち着いて話す。"
PORTRAIT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
# Measured against this checkpoint, references from 2 s to 28 s all produced
# script-accurate speech, so length is not a correctness gate. The checkpoint
# declares ref_max_seconds 120.0 and the runtime truncates beyond it; the
# training range starts at 1 s. Refuse only what carries no speaker at all,
# and advise rather than block below the length of the bundled references.
REFERENCE_MINIMUM_SECONDS = 2.0
REFERENCE_ADVISED_SECONDS = 10.0
REFERENCE_MAXIMUM_SECONDS = 120.0
# The two bundled references reach about 13.2 kHz. Reference-free synthesis
# draws speakers ranging from roughly 8 kHz to 17 kHz, and the narrow ones are
# audibly dull -- and stay dull, because narration copies the reference's
# spectral character. Report it so the voice can simply be drawn again.
REFERENCE_DULL_CUTOFF_HZ = 11000.0
VOICE_TEST_SCRIPT = "こんにちは、はじめまして。今日はよろしくお願いします。"


# One fixed passage, read in the described voice, becomes the reference for a
# character designed from words alone. It is kept short on purpose: measured
# against this checkpoint a 2 s reference already produces script-accurate
# speech, so length buys little, while a long single generation is where
# wording drifts. Narration itself never generates a block this long -- it
# still splits into parts, each generated against this file.
VOICE_DESIGN_PASSAGE = (
    "こんにちは。今日は、この声で話す練習をしています。"
    "いち、に、さん、し、ご。経験、状況、必要、確認。"
    "最後まで聞いてくれて、ありがとうございました。")
DEFAULT_DESIGN_CAPTION = "若く元気な女性の声。カフェの店員のように、明るくハキハキとした少し高めのトーンで話している。"


def design_reference_wav(server, caption, seed, output, passage=None,
                         timeout=600, *, root=None, provenance=None, method=None):
    """Create one persistent voice using the selected recipe, without rerolls."""
    from .compat import load_yaml_file

    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    recipe_path = root / "recipes/tts-irodori-v4.1.yaml"
    recipe = load_yaml_file(recipe_path)
    if method not in {None, "no_ref", "synthetic_reference"}:
        raise TTSError("unknown voice design method")
    design = recipe["voice_design_no_ref" if method == "no_ref" else "voice_design"]
    caption = (caption or "").strip()
    if not caption:
        raise TTSError("どんな声かを文章で書いてください")
    if not 0 <= int(seed) <= 2**32 - 1:
        raise TTSError("seed must be from 0 to 4294967295")
    options = dict(design["sampling"], caption=caption, seed=int(seed))
    source = None
    if design["mode"] == "synthetic_reference":
        source = (root / design["reference"]).resolve()
        if not source.is_relative_to(root.resolve()):
            raise TTSError("voice design reference escapes the repository")
        if not source.is_file() or _sha256(source) != design["reference_sha256"]:
            raise TTSError("voice design reference is missing or its hash does not match")
        options["ref_wav"] = "/workspace/" + source.relative_to(root.resolve()).as_posix()
    elif design["mode"] == "no_ref":
        options["no_ref"] = True
    else:
        raise TTSError("unknown voice design mode")
    payload = {
        "model": "irodori-tts",
        "input": passage or design["passage"],
        "response_format": "wav",
        "irodori": options,
    }
    runtime = backend_health(server)
    audio, headers = _request(
        server, "POST", "/v1/audio/speech", payload=payload, timeout=timeout)
    if not audio.startswith(b"RIFF"):
        raise TTSError("Irodori-TTS response was not a WAV file")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(audio)
    temporary.replace(output)
    if provenance is not None:
        provenance.update({
            "caption": caption, "seed": int(seed), "method": design["mode"],
            "recipe": recipe["id"], "recipe_sha256": _sha256(recipe_path),
            "request": payload, "runtime": runtime,
            "model_revision": MODEL_REVISION, "codec_revision": CODEC_REVISION,
            "irodori_commit": IRODORI_COMMIT, "server_commit": SERVER_COMMIT,
            "reference": ({"path": design["reference"], "sha256": _sha256(source)}
                          if source else None),
            "output_sha256": _sha256(output),
        })
    return output


def user_characters_root(root):
    return Path(root) / USER_CHARACTER_DIRNAME


def character_root(root, character_id):
    if not character_id or not all(
            ch.isalnum() or ch in "-_" for ch in character_id):
        raise TTSError("invalid character id")
    return user_characters_root(root) / character_id


def character_id_from_label(label, taken=()):
    """Derive a filesystem-safe id, keeping the label for display."""
    base = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                   for ch in (label or "").strip().lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    if not base or not base[0].isascii() or not base[0].isalnum():
        # Japanese labels normalise to nothing useful; stay deterministic.
        base = "voice-" + hashlib.sha256(
            (label or "").encode("utf-8")).hexdigest()[:8]
    candidate, suffix = base[:40], 2
    while candidate in taken:
        candidate = "%s-%d" % (base[:36], suffix)
        suffix += 1
    return candidate


def _stable_seed(character_id):
    """A fixed starting seed, so a character reproduces its own first take."""
    return int(hashlib.sha256(character_id.encode("utf-8")).hexdigest()[:8], 16)


def load_user_characters(root, include_drafts=False):
    """Characters created on this machine, resolved to concrete file paths."""
    root = Path(root)
    base = user_characters_root(root)
    if not base.is_dir():
        return {}
    found = {}
    for directory in sorted(path for path in base.iterdir() if path.is_dir()):
        try:
            record = json.loads(
                (directory / "character.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("draft") is True and not include_drafts:
            continue
        character_id = record.get("id") or directory.name
        reference = root / record.get("reference", {}).get("path", "")
        portrait = root / record.get("portrait", {}).get("path", "")
        if not reference.is_file() or not portrait.is_file():
            continue
        found[character_id] = {
            "label_ja": record.get("label") or character_id,
            "label_en": record.get("label") or character_id,
            "caption": record.get("caption") or DEFAULT_USER_CAPTION,
            "seed": int(record.get("seed", _stable_seed(character_id))),
            "reference": record["reference"]["path"],
            "portrait": record["portrait"]["path"],
            "source": "user",
            "derived_from": record.get("derived_from"),
            "designed": bool(record.get("reference", {}).get("designed_from")),
            "design_seed": (record.get("reference", {}).get("designed_from")
                            or {}).get("seed"),
            "created_at": record.get("created_at"),
            "draft": record.get("draft") is True,
        }
    return found


def list_characters(root, include_drafts=False):
    """Bundled characters first, then the ones created on this machine."""
    merged = {}
    for key, value in narration.CHARACTERS.items():
        entry = dict(value)
        entry.setdefault("source", "bundled")
        entry.setdefault("portrait", str(
            Path("assets/characters") / key / "images" / BUNDLED_PORTRAITS[key]))
        merged[key] = entry
    merged.update(load_user_characters(root, include_drafts=include_drafts))
    return merged


BUNDLED_PORTRAITS = {
    "aoi": "aoi-portrait-angled-01.png",
    "sakura": "sakura-portrait-seated-01.png",
}


def voice_alias_path(root, character_id, characters=None):
    """The reference path the Irodori container resolves for one voice."""
    characters = characters or list_characters(root)
    entry = characters.get(character_id)
    if not entry:
        raise TTSError("unknown character: %s" % character_id)
    reference = Path(entry["reference"])
    if reference.parts and reference.parts[0] == USER_CHARACTER_DIRNAME:
        return "user/%s" % Path(*reference.parts[1:]).as_posix()
    return Path(*reference.parts[1:]).as_posix()


def write_runtime_voices(root):
    """Materialise the alias file and mount points the container expects.

    The bundled config file stays untouched: user characters live outside the
    repository, and merging happens into a generated directory so the
    container never needs a single-file bind mount that a host-side rewrite
    would silently detach from.
    """
    root = Path(root)
    runtime = root / "outputs" / "tts" / RUNTIME_VOICES_DIRNAME
    (runtime / "characters").mkdir(parents=True, exist_ok=True)
    (runtime / "user").mkdir(parents=True, exist_ok=True)
    user_characters_root(root).mkdir(parents=True, exist_ok=True)
    try:
        aliases = json.loads(
            (root / "config" / "tts-voices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        aliases = {}
    characters = list_characters(root, include_drafts=True)
    for character_id, entry in characters.items():
        if entry.get("source") == "user":
            aliases[character_id] = {
                "ref_wav": voice_alias_path(root, character_id, characters)}
    narration.write_json_atomic(runtime / "aliases.json", aliases)
    return runtime / "aliases.json"


def measure_reference_bandwidth(root, wav_path):
    """Ask the container how far up the reference actually reaches.

    Returns None when the measurement cannot run: it is an advisory signal, so
    a stopped container must not stop a character from being made.
    """
    root = Path(root)
    try:
        relative = Path(wav_path).resolve().relative_to(root.resolve())
    except ValueError:
        return None
    try:
        finished = subprocess.run(
            [str(root / "scripts" / "tts-audio-check.sh"), str(relative)],
            cwd=str(root), check=True, capture_output=True, text=True)
        return json.loads(finished.stdout)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def bandwidth_advice(measured, designed=True):
    if not measured or not measured.get("cutoff_hz"):
        return []
    if measured["cutoff_hz"] >= REFERENCE_DULL_CUTOFF_HZ:
        return []
    action = ("「別の声にする」で試し直せます" if designed else
              "別の音声ファイルで試してください")
    return ["声がこもる可能性があります（%.1fkHzまで）。%s"
            % (measured["cutoff_hz"] / 1000.0, action)]


def inspect_reference_audio(path):
    """Report why a reference recording is unusable, in the caller's terms."""
    info = narration.inspect_wav(path)
    with wave.open(str(path), "rb") as source:
        payload = source.readframes(source.getnframes())
    seconds = info["duration_seconds"]
    levels = narration._window_rms(payload, round(0.02 * narration.SAMPLE_RATE))
    voiced = [level for level in levels if level >= round(0.012 * 32768)]
    voiced_ratio = len(voiced) / len(levels) if levels else 0.0
    peak = max((abs(value[0]) for value in struct.iter_unpack(
        "<h", payload[:len(payload) // 2 * 2])), default=0)
    quiet = narration._pcm_rms(
        narration._quietest_window(payload), narration.SAMPLE_WIDTH)
    loud = narration._pcm_rms(payload, narration.SAMPLE_WIDTH)
    problems, advice = [], []
    if seconds < REFERENCE_MINIMUM_SECONDS:
        problems.append(
            "音声が%.1f秒しかありません。あと%.1f秒ぶん読んでください"
            % (seconds, REFERENCE_MINIMUM_SECONDS - seconds))
    elif seconds < REFERENCE_ADVISED_SECONDS:
        advice.append(
            "%.0f秒です。10秒以上あると声が安定しやすくなります" % seconds)
    elif seconds > REFERENCE_MAXIMUM_SECONDS:
        advice.append(
            "%.0f秒です。音声エンジンは先頭%.0f秒だけを使います"
            % (seconds, REFERENCE_MAXIMUM_SECONDS))
    if voiced_ratio < 0.35:
        advice.append("無音の割合が多めです。間を空けずに読んでみてください")
    if not voiced:
        problems.append("声を確認できませんでした。話し声の入った音声を選んでください")
    if peak >= 32700:
        advice.append("音量のピークが上限付近です。音割れがないか試し聞きしてください")
    if loud and quiet * 8 > loud:
        advice.append("背景の音が大きめです。静かな場所で録り直すと安定します")
    return {
        "duration_seconds": seconds,
        "voiced_ratio": voiced_ratio,
        "peak": peak,
        "noise_rms": quiet,
        "speech_rms": loud,
        "problems": problems,
        "advice": advice,
        "wav": info,
    }


def convert_to_reference_wav(root, source, target):
    """Convert any uploaded or recorded audio inside the TTS container.

    ffmpeg already ships in the pinned TTS image, so nothing is installed on
    the host and every machine converts with the same pinned binary.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [str(Path(root) / "scripts" / "tts-backend.sh"), "convert",
               "--input", str(source), "--output", str(target)]
    try:
        subprocess.run(command, cwd=str(root), check=True,
                       stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise TTSError("scripts/tts-backend.sh が見つかりません: %s" % exc) from exc
    except subprocess.CalledProcessError as exc:
        detail = " ".join((exc.stderr or "").split())[-300:]
        raise TTSError(
            "音声ファイルを読み取れませんでした%s"
            % (("： " + detail) if detail else "")) from exc
    if not target.is_file():
        raise TTSError("音声ファイルを変換できませんでした")
    return target


def create_user_character(root, label, portrait_path=None, audio_path=None,
                         voice_from=None, consent_confirmed=False, caption=None,
                         designed_from=None, draft=False):
    """Register a locally-made character.

    A described voice, an imported recording or an existing character supplies
    the reference. User-owned source files are copied so later changes to the
    original character cannot change a derived character's voice or portrait.
    """
    root = Path(root)
    label = (label or "").strip()
    if not label or len(label) > 40:
        raise TTSError("名前は1〜40文字で入力してください")
    existing = list_characters(root, include_drafts=True)
    if any(entry.get("label_ja") == label for entry in existing.values()):
        raise TTSError("同じ名前のキャラクターがすでにあります")
    base = None
    if voice_from:
        base = existing.get(voice_from)
        if not base:
            raise TTSError("元にするキャラクターが見つかりません: %s" % voice_from)
    elif audio_path is None:
        raise TTSError("声のもとを選ぶか、音声ファイルを選んでください")
    elif designed_from is None and consent_confirmed is not True:
        # A designed voice belongs to nobody, so it needs no such confirmation.
        raise TTSError("この声を使ってよいという確認にチェックを入れてください")
    if portrait_path is None and base is None:
        raise TTSError("画像を1枚選んでください")
    if portrait_path is not None:
        portrait_path = Path(portrait_path)
        if portrait_path.suffix.lower() not in PORTRAIT_SUFFIXES:
            raise TTSError("画像はPNG、JPEG、WebPのいずれかにしてください")

    character_id = character_id_from_label(label, taken=set(existing))
    directory = character_root(root, character_id)
    if directory.exists():
        raise TTSError("character already exists: %s" % character_id)
    staging = directory.with_name(directory.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    advice, seconds = [], None
    try:
        # Files are written under the staging name but recorded under the final
        # one, because the directory is renamed into place only once it is
        # complete and the registry must not point at the staging path.
        if base is not None and audio_path is None:
            if base.get("source") == "user":
                reference = staging / "reference.wav"
                shutil.copy2(root / base["reference"], reference)
                reference_path = str((directory / reference.name).relative_to(root))
            else:
                reference_path = base["reference"]
            reference_sha = _sha256(root / base["reference"])
        else:
            reference = convert_to_reference_wav(
                root, audio_path, staging / "reference.wav")
            report = inspect_reference_audio(reference)
            if report["problems"]:
                raise TTSError(report["problems"][0])
            advice, seconds = report["advice"], report["duration_seconds"]
            reference_path = str((directory / reference.name).relative_to(root))
            reference_sha = _sha256(reference)
        if portrait_path is not None:
            portrait = staging / ("portrait" + portrait_path.suffix.lower())
            shutil.copy2(portrait_path, portrait)
            portrait_sha = _sha256(portrait)
            portrait_relative = str((directory / portrait.name).relative_to(root))
        elif base.get("source") == "user":
            original = root / base["portrait"]
            portrait = staging / ("portrait" + original.suffix.lower())
            shutil.copy2(original, portrait)
            portrait_relative = str((directory / portrait.name).relative_to(root))
            portrait_sha = _sha256(portrait)
        else:
            portrait_relative = base["portrait"]
            portrait_sha = _sha256(root / portrait_relative)
        record = {
            "schema_version": 1,
            "id": character_id,
            "label": label,
            "caption": caption or (base or {}).get("caption") or DEFAULT_USER_CAPTION,
            "seed": int((base or {}).get("seed", _stable_seed(character_id))),
            "derived_from": voice_from,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "draft": draft is True,
            "reference": {
                "path": reference_path,
                "sha256": reference_sha,
                "inherited_from": voice_from if base is not None
                                  and audio_path is None else None,
                "source_name": (None if designed_from
                                else Path(audio_path).name if audio_path else None),
                "designed_from": designed_from,
            },
            "portrait": {
                "path": portrait_relative,
                "sha256": portrait_sha,
                "inherited_from": voice_from if portrait_path is None else None,
            },
            "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        }
        if audio_path is not None and designed_from is None:
            record["consent"] = {
                "confirmed": True,
                "statement": "本人の声、または本人からはっきり許可を得た声である"
                             "ことを確認した",
                "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        narration.write_json_atomic(staging / "character.json", record)
        # The staging name is not a valid character id, so a half-written
        # character is never picked up by the registry.
        staging.rename(directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    write_runtime_voices(root)
    if voice_from is None:
        advice = list(advice) + bandwidth_advice(
            measure_reference_bandwidth(root, root / reference_path),
            designed=designed_from is not None)
    return {"id": character_id, "label": label, "advice": advice,
            "duration_seconds": seconds, "derived_from": voice_from,
            "designed_from": designed_from, "draft": draft is True}


def commit_user_character(root, character_id):
    """Make an auditioned draft visible as a normal user character."""
    root = Path(root)
    path = character_root(root, character_id) / "character.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TTSError("unknown character: %s" % character_id) from exc
    # Treat a repeated commit as success.  A browser may retry after losing the
    # response, and making that recovery path fail only strands a usable voice.
    if record.get("draft") is not True:
        return character_id
    record["draft"] = False
    record["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    narration.write_json_atomic(path, record)
    write_runtime_voices(root)
    return character_id


def redesign_character_voice(root, character_id, server=DEFAULT_SERVER,
                            caption=None, seed=None):
    """Give a described character a different voice, keeping everything else.

    Finding a voice takes several attempts, and re-entering the name, the
    portrait and the description for each one is the whole cost of trying.
    Only the reference is replaced, so the character keeps its identity.
    """
    root = Path(root)
    directory = character_root(root, character_id)
    path = directory / "character.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TTSError("unknown character: %s" % character_id) from exc
    designed = (record.get("reference") or {}).get("designed_from")
    if not designed:
        raise TTSError(
            "この声は音声ファイルから作られています。"
            "変えるには新しいキャラクターを作ってください")
    using = character_jobs(root, character_id)
    if using:
        raise TTSError(
            "このキャラクターを使った音声が%d件あります。"
            "声を変えるには新しいキャラクターを作ってください"
            % len(using))
    _check_shared_character_files(root, character_id, ("reference",))
    caption = (caption or designed.get("caption") or "").strip()
    seed = int(designed.get("seed", 0) if seed is None else seed)
    staged = directory / "reference.new.wav"
    try:
        provenance = {"caption": caption, "seed": seed}
        design_reference_wav(server, caption, seed, staged, root=root,
                             provenance=provenance, method=designed.get("method", "no_ref"))
        report = inspect_reference_audio(staged)
        if report["problems"]:
            raise TTSError(report["problems"][0])
        os.replace(staged, directory / "reference.wav")
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    record["caption"] = caption
    record["reference"].update({
        "sha256": _sha256(directory / "reference.wav"),
        "designed_from": provenance,
        "redesigned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    narration.write_json_atomic(path, record)
    write_runtime_voices(root)
    advice = list(report["advice"]) + bandwidth_advice(
        measure_reference_bandwidth(root, directory / "reference.wav"))
    return {"id": character_id, "label": record.get("label", character_id),
            "caption": caption, "seed": seed, "advice": advice,
            "duration_seconds": report["duration_seconds"]}


def replace_character_portrait(root, character_id, portrait_path):
    """Swap the picture without touching the voice, seed or past jobs."""
    root = Path(root)
    directory = character_root(root, character_id)
    path = directory / "character.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TTSError("unknown character: %s" % character_id) from exc
    _check_shared_character_files(root, character_id, ("portrait",))
    portrait_path = Path(portrait_path)
    if portrait_path.suffix.lower() not in PORTRAIT_SUFFIXES:
        raise TTSError("画像はPNG、JPEG、WebPのいずれかにしてください")
    replacement = directory / ("portrait" + portrait_path.suffix.lower())
    previous = root / record["portrait"]["path"]
    shutil.copy2(portrait_path, replacement)
    record["portrait"] = {
        "path": str(replacement.relative_to(root)),
        "sha256": _sha256(replacement),
        "inherited_from": None,
        "replaced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    narration.write_json_atomic(path, record)
    # Only ever remove a portrait this character owned; an inherited one is a
    # bundled asset that other characters still point at.
    if previous != replacement and directory in previous.parents:
        previous.unlink(missing_ok=True)
    return record


def character_jobs(root, character_id, include_tests=False):
    """Narrations that cite one character.

    The audition take this tool generates for a brand-new character does not
    count: it exists only to let the character be judged, so it must not be
    the reason the same character cannot be discarded.
    """
    base = Path(root) / "outputs" / "tts"
    if not base.is_dir():
        return []
    using = []
    for directory in sorted(path for path in base.iterdir() if path.is_dir()):
        try:
            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if manifest.get("character") != character_id:
            continue
        if not include_tests and manifest.get("purpose") == "voice_test":
            continue
        using.append(manifest.get("job_id", directory.name))
    return using


def delete_user_character(root, character_id):
    """Remove a locally-made character, never one that a job still cites."""
    root = Path(root)
    if character_id in narration.CHARACTERS:
        raise TTSError("同梱のキャラクターは削除できません")
    directory = character_root(root, character_id)
    if not (directory / "character.json").is_file():
        raise TTSError("unknown character: %s" % character_id)
    using = character_jobs(root, character_id)
    if using:
        raise TTSError(
            "このキャラクターを使った音声が%d件あるため、削除できません"
            % len(using))
    _check_shared_character_files(root, character_id)
    for job_id in character_jobs(root, character_id, include_tests=True):
        manifest = load_job(root, job_id)
        if manifest.get("purpose") == "voice_test":
            shutil.rmtree(job_root(root, job_id), ignore_errors=True)
    shutil.rmtree(directory)
    write_runtime_voices(root)
    return character_id


def _check_shared_character_files(root, character_id,
                                  fields=("reference", "portrait")):
    """Keep references made by older versions valid when their owner changes."""
    directory = character_root(root, character_id).resolve()
    for key, entry in load_user_characters(root).items():
        if key == character_id:
            continue
        if any(directory in (Path(root) / entry[field]).resolve().parents
               for field in fields):
            raise TTSError(
                "「%s」がこのキャラクターのファイルを使用しているため、変更できません"
                % entry["label_ja"])


def synthesize_part(server, text, voice, caption, seed, output, timeout=900,
                    duration_scale=None):
    options = {
        "caption": caption,
        "seed": int(seed),
        "chunking_enabled": False,
    }
    if duration_scale is not None:
        options["duration_scale"] = float(duration_scale)
    payload = {
        "model": "irodori-tts",
        "input": text,
        "voice": voice,
        "response_format": "wav",
        "irodori": options,
    }
    audio, headers = _request(
        server, "POST", "/v1/audio/speech", payload=payload, timeout=timeout)
    if not audio.startswith(b"RIFF"):
        raise TTSError("Irodori-TTS response was not a WAV file")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(audio)
    temporary.replace(output)
    info = narration.inspect_wav(output)
    return info, headers


def generate_narration(root, script, character, server=DEFAULT_SERVER,
                       caption=None, seed=None, gaps_ms=None, outro_ms=1500,
                       output=None, job_id=None, on_progress=None, run_asr=True,
                       metadata=None, allow_draft=False):
    root = Path(root)
    characters = list_characters(root, include_drafts=allow_draft)
    if character not in characters:
        raise TTSError("unknown character: %s" % character)
    if not models_prepared(root):
        raise TTSError("TTS models are not prepared; download the models first")
    character_config = characters[character]
    caption = caption or character_config["caption"]
    seed = character_config["seed"] if seed is None else int(seed)
    parts = narration.split_script(script)
    if gaps_ms is not None:
        if len(gaps_ms) != max(0, len(parts) - 1):
            raise TTSError("expected %d gap values" % max(0, len(parts) - 1))
        for index, value in enumerate(gaps_ms):
            parts[index].gap_after_ms = int(value)
    job_id = job_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    directory = job_root(root, job_id)
    part_dir = directory / "parts"
    part_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "job_id": job_id,
        "status": "generating",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "script": script,
        "character": character,
        "character_label": character_config.get("label_ja", character),
        "voice_source": character_config.get("source", "bundled"),
        "runtime": backend_health(server),
        "voice_reference": {
            "path": character_config["reference"],
            "sha256": (_sha256(root / character_config["reference"])
                       if (root / character_config["reference"]).is_file() else None),
        },
        "caption": caption,
        "seed": seed,
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "codec": {"id": CODEC_ID, "revision": CODEC_REVISION},
        "code": {"server_commit": SERVER_COMMIT,
                 "irodori_commit": IRODORI_COMMIT},
        "parts": narration.serialise_parts(parts),
        "outro_ms": int(outro_ms),
        "human_review": "pending",
    }
    if metadata:
        manifest.update(metadata)
    narration.write_json_atomic(directory / "manifest.json", manifest)
    try:
        for part in parts:
            if on_progress:
                on_progress(part.index, len(parts), part.text)
            wav_path = part_dir / ("part_%03d.wav" % part.index)
            info, headers = synthesize_part(
                server, part.text, character, caption, seed, wav_path)
            used_seed = int(headers.get("x-irodori-seed", str(seed)))
            item = manifest["parts"][part.index - 1]
            item.update({
                "audio": str(wav_path.relative_to(directory)),
                "sha256": _sha256(wav_path),
                "wav": info,
                "quality": _part_quality(part.text, info),
                "seed": used_seed,
                "initial_seed": used_seed,
                "used_seed": str(used_seed),
                "asr": {"status": "not_run"},
            })
            narration.write_json_atomic(directory / "manifest.json", manifest)
        final_path = directory / "narration.wav"
        gap_values = [part.gap_after_ms for part in parts[:-1]]
        final_info = narration.join_wav_parts(
            _part_audio_paths(directory, manifest["parts"]),
            final_path, gaps_ms=gap_values, outro_ms=outro_ms)
        manifest["status"] = "review"
        manifest["output"] = {
            "audio": "narration.wav", "sha256": _sha256(final_path),
            "wav": final_info,
        }
        narration.write_json_atomic(directory / "manifest.json", manifest)
        if run_asr:
            if on_progress:
                on_progress(len(parts), len(parts), "音声認識で台本を照合中")
            try:
                subprocess.run(
                    [str(root / "scripts" / "tts-asr.sh"), job_id],
                    cwd=str(root), check=True, capture_output=True, text=True)
                manifest = load_job(root, job_id)
            except (OSError, subprocess.CalledProcessError) as exc:
                manifest["asr"] = {"status": "error", "error": str(exc)}
        if output:
            output = Path(output).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_path, output)
            manifest["exported_to"] = str(output)
        narration.write_json_atomic(directory / "manifest.json", manifest)
        return manifest, directory
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        narration.write_json_atomic(directory / "manifest.json", manifest)
        raise


def rejoin_job(root, job_id, gaps_ms, outro_ms=1500, output=None):
    with job_lock(root, job_id) as directory:
        manifest_path = directory / "manifest.json"
        manifest = load_job(root, job_id)
        parts = manifest.get("parts") or []
        if len(gaps_ms) != max(0, len(parts) - 1):
            raise TTSError("expected %d gap values" % max(0, len(parts) - 1))
        gaps_ms = [int(gap) for gap in gaps_ms]
        paths = _part_audio_paths(directory, parts)
        for index, gap in enumerate(gaps_ms):
            parts[index]["gap_after_ms"] = gap
        final_path = directory / "narration.wav"
        info = narration.join_wav_parts(
            paths, final_path, gaps_ms=gaps_ms, outro_ms=int(outro_ms))
        manifest["outro_ms"] = int(outro_ms)
        manifest["output"] = {
            "audio": "narration.wav", "sha256": _sha256(final_path), "wav": info}
        manifest["human_review"] = "pending"
        if output:
            output = Path(output).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_path, output)
            manifest["exported_to"] = str(output)
        narration.write_json_atomic(manifest_path, manifest)
        return manifest


def regenerate_part(root, job_id, part_index, server=DEFAULT_SERVER,
                    duration_scale=None, seed=None, run_asr=True,
                    on_progress=None):
    root = Path(root)
    with job_lock(root, job_id) as directory:
        manifest = load_job(root, job_id)
        parts = manifest.get("parts") or []
        if part_index < 1 or part_index > len(parts):
            raise TTSError("part index must be from 1 to %d" % len(parts))
        part = parts[part_index - 1]
        if duration_scale is not None and not 0.1 <= float(duration_scale) <= 4.0:
            raise TTSError("duration scale must be from 0.1 to 4.0")
        current_seed = part.get("seed", part.get("used_seed", manifest["seed"]))
        selected_seed = int(current_seed if seed is None else seed)
        if not 0 <= selected_seed <= 2**32 - 1:
            raise TTSError("seed must be from 0 to 4294967295")
        selected_scale = (float(duration_scale) if duration_scale is not None
                          else part.get("duration_scale"))
        paths = _part_audio_paths(directory, parts)
        if on_progress:
            on_progress("パート%dを再生成中" % part_index)
        wav_path = paths[part_index - 1]
        info, headers = synthesize_part(
            server, part["text"], manifest["character"], manifest["caption"],
            selected_seed, wav_path, duration_scale=selected_scale)
        used_seed = int(headers.get("x-irodori-seed", str(selected_seed)))
        part.update({
            "sha256": _sha256(wav_path), "wav": info,
            "quality": _part_quality(part["text"], info),
            "seed": used_seed,
            "initial_seed": int(part.get("initial_seed", manifest["seed"])),
            "used_seed": str(used_seed),
            "asr": {"status": "not_run"},
        })
        if selected_scale is not None:
            part["duration_scale"] = float(selected_scale)
        gaps = [int(item["gap_after_ms"]) for item in parts[:-1]]
        final_path = directory / "narration.wav"
        final_info = narration.join_wav_parts(
            paths, final_path, gaps_ms=gaps,
            outro_ms=int(manifest.get("outro_ms", 1500)))
        manifest["output"] = {
            "audio": "narration.wav", "sha256": _sha256(final_path),
            "wav": final_info,
        }
        manifest["human_review"] = "pending"
        narration.write_json_atomic(directory / "manifest.json", manifest)
        if run_asr:
            if on_progress:
                on_progress("音声認識で台本を再照合中")
            try:
                subprocess.run(
                    [str(root / "scripts" / "tts-asr.sh"), job_id],
                    cwd=str(root), check=True, capture_output=True, text=True)
                manifest = load_job(root, job_id)
            except (OSError, subprocess.CalledProcessError) as exc:
                manifest["asr"] = {"status": "error", "error": str(exc)}
                narration.write_json_atomic(directory / "manifest.json", manifest)
        return manifest


def load_job(root, job_id):
    path = job_root(root, job_id) / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TTSError("cannot read narration job %s: %s" % (job_id, exc)) from exc


def delete_narration_job(root, job_id):
    """Delete one finished local narration while preserving exported copies."""
    with job_lock(root, job_id) as directory:
        manifest = load_job(root, job_id)
        if manifest.get("status") == "generating":
            raise TTSError("生成中の音声は削除できません。完了を待ってください")
        shutil.rmtree(directory)
        return manifest


def adopt_as_input_set(root, job_id, name=None):
    """Copy a reviewed narration and matching bundled portrait into inputs/."""
    root = Path(root)
    with job_lock(root, job_id):
        return _adopt_as_input_set(root, job_id, name)


def archive_adopted_input_set(root, name):
    """Remove one TTS input from the runner while keeping a recoverable copy."""
    if not isinstance(name, str) or not name \
            or not all(ch.isalnum() or ch in "-_" for ch in name):
        raise TTSError("invalid input set name")
    root = Path(root).resolve()
    if _video_generation_active(root):
        raise TTSError("動画を生成中です。完了または中止後に削除してください")
    inputs = root / "inputs"
    target = inputs / name
    if inputs.is_symlink() or target.is_symlink() or not target.is_dir():
        raise TTSError("動画用の音声・台本が見つかりません")
    target = target.resolve()
    if target.parent != inputs.resolve():
        raise TTSError("invalid input set path")
    manifest_path = target / "tts-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise TTSError("この入力セットはTTS画面から削除できません")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise TTSError("cannot read TTS input manifest") from exc
    if not isinstance(manifest, dict) or not isinstance(
            manifest.get("source_job"), str):
        raise TTSError("この入力セットはTTS画面から削除できません")
    outputs = root / "outputs"
    archive_parent = outputs / "tts"
    archive = archive_parent / "deleted-inputs"
    if outputs.is_symlink() or archive_parent.is_symlink() or archive.is_symlink():
        raise TTSError("invalid deleted-input archive")
    archive.mkdir(parents=True, exist_ok=True)
    archive = archive.resolve()
    if os.path.commonpath([str(root), str(archive)]) != str(root):
        raise TTSError("invalid deleted-input archive")
    destination = archive / ("%s-%s-%s" % (
        name, time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]))
    target.rename(destination)
    return destination


def _video_generation_active(root):
    """Return true only for a tracked run whose worker still owns its PID."""
    state = run_state.latest(root, running_only=True)
    if not state:
        return False
    try:
        pid = int(state.get("pid"))
        command = [part for part in Path(
            "/proc/%d/cmdline" % pid).read_bytes().split(b"\0") if part]
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


def _adopt_as_input_set(root, job_id, name=None):
    manifest = load_job(root, job_id)
    if manifest.get("status") not in ("review", "completed"):
        raise TTSError("only a completed narration can become an input set")
    if manifest.get("human_review") != "passed":
        raise TTSError("listen to every part and the joined WAV before adopting it")
    character = manifest.get("character")
    characters = list_characters(root)
    if character not in characters:
        raise TTSError("narration has no supported character")
    safe_name = name or ("tts-" + job_id)
    if not safe_name or not all(ch.isalnum() or ch in "-_" for ch in safe_name):
        raise TTSError("input-set name may contain only letters, numbers, - and _")
    target = root / "inputs" / safe_name
    if target.exists():
        raise TTSError("input set already exists: %s" % target)
    image = root / characters[character]["portrait"]
    if not image.is_file():
        raise TTSError("character portrait is missing: %s" % image)
    audio = job_root(root, job_id) / manifest["output"]["audio"]
    target.mkdir(parents=True)
    try:
        # The video stage loads whatever single image the set contains, so keep
        # the real format rather than renaming a JPEG to .png.
        shutil.copy2(image, target / ("image" + image.suffix.lower()))
        shutil.copy2(audio, target / "audio.wav")
        (target / "script.txt").write_text(manifest["script"], encoding="utf-8")
        narration.write_json_atomic(target / "tts-manifest.json", {
            "source_job": job_id,
            "source_audio_sha256": manifest["output"]["sha256"],
            "character": character,
            "character_label": characters[character].get("label_ja", character),
            "voice_source": characters[character].get("source", "bundled"),
            "human_review": manifest.get("human_review", "pending"),
            "adopted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration_seconds": manifest.get("output", {}).get(
                "wav", {}).get("duration_seconds"),
            "title": next((line.strip() for line in manifest["script"].splitlines()
                           if line.strip()), "")[:80],
        })
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def confirm_human_review(root, job_id):
    with job_lock(root, job_id) as directory:
        manifest = load_job(root, job_id)
        if manifest.get("status") not in ("review", "completed"):
            raise TTSError("narration generation has not completed")
        manifest["human_review"] = "passed"
        manifest["reviewed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        narration.write_json_atomic(directory / "manifest.json", manifest)
        return manifest
