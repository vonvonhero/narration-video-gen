"""Post-run verification.

A zero exit code from the generator is not evidence that a run succeeded. The
checks here are the ones that actually caught problems during the original
work: silently truncated video, a missing audio stream, and frame counts that
drift from what the recipe asked for because the muxer trimmed to the audio.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

# Frame-accurate trimming is not possible with stream copy at the container
# level, so a couple of frames of slack at the tail is expected rather than a
# defect. Anything beyond this is reported.
FRAME_TOLERANCE = 4


def frames_for_audio(seconds, fps):
    """Frame count the pipeline will pick for ``seconds`` of audio.

    Wan packs latents in groups of four, so the frame count must satisfy 4n+1.
    This is the same rounding the generator applies, which is why the expected
    frame count for a verification is derived here rather than typed in.
    """
    n = max(int(round(seconds * fps)), 5)
    return ((n - 1 + 3) // 4) * 4 + 1


def wav_duration(path):
    """Duration of a RIFF WAV file, using the stdlib rather than ffprobe."""
    import wave

    with wave.open(str(path)) as handle:
        return handle.getnframes() / handle.getframerate()


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe(path, command_prefix=None, probe_path=None):
    """Return the parsed ``ffprobe`` record for ``path``.

    Raises ``RuntimeError`` when ffprobe is missing, because silently skipping
    verification would defeat the point of this module.
    """
    if command_prefix is None and shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe not found; install ffmpeg or run verification in the container")
    command = list(command_prefix or ["ffprobe"])
    command.extend(["-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams",
                    str(probe_path if probe_path is not None else path)])
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise RuntimeError("ffprobe could not be started: %s" % exc) from exc
    if completed.returncode != 0:
        raise RuntimeError("ffprobe failed for %s: %s" % (path, completed.stderr.strip()))
    return json.loads(completed.stdout)


def _video_stream(probe):
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    return None


def _audio_stream(probe):
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream
    return None


def _frame_count(stream):
    """Frame count from the container, falling back to duration x rate."""
    for key in ("nb_frames", "nb_read_frames"):
        value = stream.get(key)
        if value not in (None, "", "N/A"):
            try:
                return int(value)
            except ValueError:
                pass
    duration = stream.get("duration")
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if duration and rate and "/" in rate:
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / float(den)
            return int(round(float(duration) * fps))
        except (ValueError, ZeroDivisionError):
            return None
    return None


def _frame_rate(stream):
    value = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not value or "/" not in value:
        return None
    numerator, _, denominator = value.partition("/")
    try:
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None


def verify_output(path, expected=None, command_prefix=None, probe_path=None):
    """Check a produced video against ``expected`` and return a result record.

    ``expected`` may contain ``width``, ``height``, ``fps``, ``frames`` and
    ``audio_present``. Missing keys are simply not checked.
    """
    path = Path(path)
    expected = expected or {}
    checks = []
    result = {"path": str(path), "checks": checks, "passed": False}

    if not path.is_file():
        checks.append(_check("file_exists", False, "%s does not exist" % path))
        return result
    checks.append(_check("file_exists", True, "%s" % path))

    size = path.stat().st_size
    result["bytes"] = size
    checks.append(_check("file_non_empty", size > 0, "%d bytes" % size))

    result["sha256"] = sha256_file(path)

    probe = ffprobe(path, command_prefix=command_prefix, probe_path=probe_path)
    video = _video_stream(probe)
    audio = _audio_stream(probe)
    checks.append(_check("video_stream_present", video is not None,
                         video.get("codec_name") if video else "no video stream"))
    if video is None:
        return result

    width, height = video.get("width"), video.get("height")
    frames = _frame_count(video)
    fps = _frame_rate(video)
    duration = probe.get("format", {}).get("duration")
    result.update({
        "width": width,
        "height": height,
        "frames": frames,
        "fps": fps,
        "duration_seconds": float(duration) if duration else None,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
    })

    if "width" in expected and "height" in expected:
        ok = (width, height) == (expected["width"], expected["height"])
        checks.append(_check("resolution", ok, "%sx%s (expected %sx%s)"
                             % (width, height, expected["width"], expected["height"])))

    if expected.get("fps") is not None:
        expected_fps = float(expected["fps"])
        ok = fps is not None and abs(fps - expected_fps) <= 0.01
        checks.append(_check(
            "frame_rate", ok, "%s fps (expected %.3f)"
            % ("unknown" if fps is None else "%.3f" % fps, expected_fps)))

    want_audio = expected.get("audio_present", True)
    checks.append(_check("audio_stream_present", (audio is not None) == want_audio,
                         audio.get("codec_name") if audio else "no audio stream"))

    if expected.get("frames") and frames is not None:
        delta = frames - expected["frames"]
        ok = abs(delta) <= FRAME_TOLERANCE
        checks.append(_check(
            "frame_count", ok,
            "%d frames (expected %d, delta %+d, tolerance +-%d)"
            % (frames, expected["frames"], delta, FRAME_TOLERANCE),
        ))

    if audio is not None and duration:
        audio_duration = audio.get("duration")
        if audio_duration:
            drift = abs(float(duration) - float(audio_duration))
            # A drift larger than a second means video and audio were combined
            # with mismatched lengths, which -shortest hides rather than fixes.
            checks.append(_check("audio_video_alignment", drift <= 1.0,
                                 "container %.3fs vs audio %.3fs (drift %.3fs)"
                                 % (float(duration), float(audio_duration), drift)))

    result["passed"] = all(c["ok"] for c in checks)
    return result


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}
