"""Local narration planning, WAV inspection and deterministic assembly.

The actual speech model deliberately lives behind a local HTTP API.  This
module stays dependency-free so the main CLI can plan and review narration
without importing torch or installing audio packages on the host.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import unicodedata
import wave
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path


SAMPLE_RATE = 48000
SAMPLE_WIDTH = 2
CHANNELS = 1
DEFAULT_OUTRO_MS = 1500
MIN_PART_CHARS = 24
MAX_PART_CHARS = 90

CHARACTERS = {
    "aoi": {
        "label_ja": "葵",
        "label_en": "Aoi",
        "caption": "落ち着いて親しみやすく、自然なテンポで明瞭に話す。",
        "seed": 1315520242,
        "reference": "assets/characters/aoi/audio/aoi-narration-take3.wav",
    },
    "sakura": {
        "label_ja": "さくら",
        "label_en": "Sakura",
        "caption": "明るく軽やかに、やや速めのテンポではっきり話す。",
        "seed": 88888,
        "reference": "assets/characters/sakura/audio/sakura-narration-blog.wav",
    },
}


@dataclass
class NarrationPart:
    index: int
    text: str
    gap_after_ms: int
    paragraph_end: bool = False


def _visible_length(text):
    return len(re.sub(r"[\s、。,.!?！？・「」『』（）()\[\]【】]", "", text))


# A URL, a path or a mail address must never be cut, so its punctuation is
# hidden from every boundary rule below.
_ATOMIC = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.|mailto:)[^\s、。「」『』（）()【】\[\]]+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    re.IGNORECASE)
_SENTENCE_END = re.compile(r"[。！？!?]+[」』）)】\]]*")
_CLAUSE_END = re.compile(r"[、,；;：:]")


def _atomic_spans(text):
    return [match.span() for match in _ATOMIC.finditer(text)]


def _inside(spans, index):
    return any(start < index < end for start, end in spans)


def _is_sentence_end(text, match):
    """Reject ASCII marks that only look like the end of a sentence."""
    if not match.group().isascii():
        return True
    following = text[match.end():match.end() + 1]
    # "docs?lang=ja" or "3!2" continues; "Wow! That" really ends a sentence.
    return not (following.isalnum() or following in "=/_-")


def _sentence_units(paragraph):
    """Split after sentence punctuation while keeping closers with the unit.

    Slices are returned verbatim, including the whitespace between sentences,
    so that merging them back together cannot alter what is spoken.
    """
    spans = _atomic_spans(paragraph)
    units = []
    start = 0
    for match in _SENTENCE_END.finditer(paragraph):
        if _inside(spans, match.start()) or not _is_sentence_end(paragraph, match):
            continue
        end = match.end()
        if paragraph[start:end].strip():
            units.append(paragraph[start:end])
        start = end
    if paragraph[start:].strip():
        units.append(paragraph[start:])
    return units


def _split_long(unit, maximum):
    if _visible_length(unit) <= maximum:
        return [unit]
    spans = _atomic_spans(unit)
    pieces = []
    start = 0
    for match in _CLAUSE_END.finditer(unit):
        if _inside(spans, match.start()):
            continue
        pieces.append(unit[start:match.end()])
        start = match.end()
    pieces.append(unit[start:])
    result = []
    current = ""
    for piece in pieces:
        if current and _visible_length(current + piece) > maximum:
            result.append(current)
            current = piece
        else:
            current += piece
    if current.strip():
        result.append(current)
    return result


def _merge_short(units, minimum):
    """Avoid the short-text failure mode without swallowing whole paragraphs."""
    merged = []
    pending = ""
    for unit in units:
        candidate = pending + unit
        if _visible_length(candidate) < minimum:
            pending = candidate
            continue
        merged.append(candidate)
        pending = ""
    if pending:
        if merged and _visible_length(merged[-1] + pending) <= MAX_PART_CHARS:
            merged[-1] += pending
        else:
            merged.append(pending)
    return [item.strip() for item in merged if item.strip()]


def inferred_gap_ms(text, paragraph_end=False):
    if paragraph_end:
        return 820
    stripped = text.rstrip("」』）)】]")
    if stripped.endswith(("！", "!", "？", "?")):
        return 620
    if stripped.endswith(("、", ",", "：", ":", "；", ";")):
        return 360
    return 500


def split_script(text, minimum=MIN_PART_CHARS, maximum=MAX_PART_CHARS):
    """Create stable, editable parts from Japanese prose.

    Blank lines are paragraph boundaries. Short consecutive sentences are
    joined because v3 frequently over-predicted their duration and produced
    extra speech. The original punctuation is never removed.
    """
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("script is empty")
    paragraphs = [item for item in re.split(r"\n\s*\n", text) if item.strip()]
    planned = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        # A non-empty line is an explicit author-controlled generation part.
        # Within a line, punctuation remains automatic and adjacent short
        # sentences are merged to avoid the measured v3 short-text failure.
        # This preserves the original BKM's "one line = one sentence" path
        # without making wrapped prose produce unsafe tiny fragments.
        lines = [item.strip() for item in paragraph.splitlines() if item.strip()]
        for line_index, line in enumerate(lines):
            units = []
            for sentence in _sentence_units(line):
                units.extend(_split_long(sentence, maximum))
            units = _merge_short(units, minimum)
            for unit_index, unit in enumerate(units):
                paragraph_end = (
                    line_index == len(lines) - 1
                    and unit_index == len(units) - 1
                    and paragraph_index < len(paragraphs) - 1)
                planned.append(NarrationPart(
                    index=len(planned) + 1,
                    text=unit,
                    gap_after_ms=inferred_gap_ms(unit, paragraph_end),
                    paragraph_end=paragraph_end,
                ))
    # A topic-changing connective belongs to the next part, so lengthen the
    # pause on the preceding one. Keep continuation connectives tighter.
    for index in range(len(planned) - 1):
        next_text = planned[index + 1].text.lstrip("「『（(")
        if next_text.startswith(("一方", "では", "次に", "ここから", "さて", "最後に")):
            planned[index].gap_after_ms = max(planned[index].gap_after_ms, 700)
        elif not planned[index].paragraph_end and next_text.startswith(
                ("そして", "また", "さらに", "つまり")):
            planned[index].gap_after_ms = min(planned[index].gap_after_ms, 480)
    if planned:
        planned[-1].gap_after_ms = 0
    return planned


def normalise_asr_text(text):
    value = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s、。,.!?！？・「」『』（）()\-ー]", "", value)


def classify_transcript(actual, accepted, review_threshold=0.70,
                        coverage_threshold=0.70, extra_coverage=1.30):
    """Grade one transcript against the script it was generated from.

    Speech beyond the script, clearly missing speech, and a substantially
    different transcript remain failures. Lesser differences are review flags,
    because Whisper routinely writes a homophone of what was actually spoken --
    the measured local runs produced
    "全部" for "ぜんぶ" and "一切" for "いっさい" on takes with no extra
    speech.  Calling those a failure trains the reviewer to ignore the flag,
    which is exactly how a real extra utterance gets adopted.
    """
    if not accepted:
        raise ValueError("no accepted transcript to compare against")
    actual_n = normalise_asr_text(actual)
    accepted_n = [value for item in accepted
                  if (value := normalise_asr_text(item))]
    if not accepted_n:
        raise ValueError("no non-empty accepted transcript to compare against")
    best = max(accepted_n, key=lambda item: SequenceMatcher(
        None, item, actual_n).ratio())
    matcher = SequenceMatcher(None, best, actual_n)
    similarity = matcher.ratio()
    coverage = len(actual_n) / len(best) if best else 1.0
    # How much of the script is recognisable inside the transcript, which
    # separates "the script plus something else" from "a different sentence".
    matched = sum(block.size for block in matcher.get_matching_blocks())
    recall = matched / len(best) if best else 1.0
    if actual_n in accepted_n:
        status, extra = "pass", ""
    elif any(actual_n.startswith(item) for item in accepted_n):
        prefix = max((item for item in accepted_n
                      if actual_n.startswith(item)), key=len)
        status, extra = "fail_extra_speech", actual_n[len(prefix):]
    elif recall >= coverage_threshold and coverage > extra_coverage:
        # The whole script is in there, followed or interleaved by more speech.
        status, extra = "fail_extra_speech", actual_n[len(best):]
    elif coverage < coverage_threshold:
        status, extra = "fail_missing_speech", ""
    elif similarity >= review_threshold:
        status, extra = "review_asr_mismatch", ""
    else:
        status, extra = "fail_mismatch", ""
    return {
        "status": status,
        "actual_normalized": actual_n,
        "accepted_normalized": accepted_n,
        "best_similarity": similarity,
        "coverage": coverage,
        "script_recall": recall,
        "extra_normalized": extra,
    }


def inspect_wav(path):
    path = Path(path)
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.getnframes()
        payload = source.readframes(frames)
    duration = frames / float(rate) if rate else 0.0
    rms = _pcm_rms(payload, width) if payload else 0
    dbfs = 20 * math.log10(max(rms / float(1 << (8 * width - 1)), 1e-9))
    issues = []
    if channels != CHANNELS:
        issues.append("expected mono audio")
    if width != SAMPLE_WIDTH:
        issues.append("expected 16-bit PCM audio")
    if rate != SAMPLE_RATE:
        issues.append("expected 48000 Hz audio")
    if duration < 0.25:
        issues.append("audio is unexpectedly short")
    cleaned_duration = duration
    if (channels, width, rate) == (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE):
        cleaned_duration = len(_clean_pcm(payload)) / float(
            SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    return {
        "path": str(path), "channels": channels, "sample_width": width,
        "sample_rate": rate, "frames": frames, "duration_seconds": duration,
        "cleaned_duration_seconds": cleaned_duration,
        "rms_dbfs": dbfs, "issues": issues,
    }


def _pcm_contract(paths):
    values = []
    for path in paths:
        with wave.open(str(path), "rb") as source:
            contract = (source.getnchannels(), source.getsampwidth(),
                        source.getframerate(), source.getcomptype())
            payload = source.readframes(source.getnframes())
        if contract[:3] != (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE) \
                or contract[3] != "NONE":
            raise ValueError("part is not mono 16-bit PCM at 48 kHz: %s" % path)
        values.append(payload)
    return values


def _quietest_window(payload, window_frames=4800):
    frame_bytes = SAMPLE_WIDTH * CHANNELS
    window_bytes = window_frames * frame_bytes
    if len(payload) <= window_bytes:
        return payload
    best = None
    step = max(frame_bytes, window_bytes // 4)
    for offset in range(0, len(payload) - window_bytes + 1, step):
        candidate = payload[offset:offset + window_bytes]
        level = _pcm_rms(candidate, SAMPLE_WIDTH)
        if best is None or level < best[0]:
            best = (level, candidate)
    return best[1] if best else b""


def _window_rms(payload, window_frames=480):
    window_bytes = window_frames * SAMPLE_WIDTH
    return [_pcm_rms(payload[offset:offset + window_bytes], SAMPLE_WIDTH)
            for offset in range(0, len(payload) - window_bytes + 1, window_bytes)]


def _pcm_rms(payload, width):
    """Return integer PCM RMS without the audioop module removed in Python 3.13."""
    if not payload:
        return 0
    if width == 1:
        values = (value - 128 for value in payload)
        count = len(payload)
    elif width == 2:
        count = len(payload) // 2
        values = (item[0] for item in struct.iter_unpack("<h", payload[:count * 2]))
    elif width == 3:
        count = len(payload) // 3
        values = (int.from_bytes(payload[index:index + 3], "little", signed=True)
                  for index in range(0, count * 3, 3))
    elif width == 4:
        count = len(payload) // 4
        values = (item[0] for item in struct.iter_unpack("<i", payload[:count * 4]))
    else:
        raise ValueError("unsupported PCM sample width: %d" % width)
    return round(math.sqrt(sum(value * value for value in values) / max(1, count)))


SPEECH_BRIDGE_SECONDS = 0.15
EXTRA_BLOB_SECONDS = 0.35
EXTRA_GAP_SECONDS = 0.45
TAIL_EXTENSION_SECONDS = 0.30


def _speech_runs(voiced, bridge):
    """Group voiced windows into utterances, bridging brief dips.

    A single window below the speech threshold occurs inside normal speech, and
    treating it as a boundary hid a real extra utterance: the trailing blob in
    a measured take was split one window from its end, so the detached-blob
    rule saw a 0.01 s blob after a 0.01 s gap and left the extra speech in.
    """
    runs = []
    for index, value in enumerate(voiced):
        if not value:
            continue
        if runs and index - runs[-1][1] <= bridge:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    return runs


def _clean_pcm(payload):
    """Remove a detached tail blob, then trim edges without cutting decay.

    Thresholds reproduce the adopted v3 joiner's normalized 0.012 speech and
    0.004 decay levels. The v3 study only ever saw the extra utterance after
    the sentence, but a measured v4.1 take put one on each side -- "シチー"
    before and "今日" after "女優をやっています" -- so both ends are checked.
    If no speech is detected, leave the part untouched.
    """
    window_frames = round(0.01 * SAMPLE_RATE)
    levels = _window_rms(payload, window_frames)
    speech_threshold = round(0.012 * 32768)
    tail_threshold = round(0.004 * 32768)
    voiced = [level >= speech_threshold for level in levels]
    if not any(voiced):
        return payload

    bridge = round(SPEECH_BRIDGE_SECONDS / 0.01)
    blob_windows = round(EXTRA_BLOB_SECONDS / 0.01)
    gap_windows = round(EXTRA_GAP_SECONDS / 0.01)
    runs = _speech_runs(voiced, bridge)
    dropped_after = None
    # A short utterance separated from the body by a long silence is the
    # extra-speech signature, and only at the end. The same shape at the start
    # is ordinary Japanese prosody: across 71 measured parts, 18 open with a
    # phrase followed by a pause of 0.45 s or more, and the shortest of those
    # openings (0.51 s) is barely longer than a measured leading intrusion
    # (0.44 s). There is no margin there, so the start is left alone and the
    # transcript check reports it for a person to judge.
    while len(runs) > 1:
        last, previous = runs[-1], runs[-2]
        if last[1] - last[0] + 1 <= blob_windows \
                and last[0] - previous[1] - 1 >= gap_windows:
            dropped_after = runs.pop()[0]
            continue
        break

    first, last = runs[0][0], runs[-1][1]
    # Follow the natural decay, but never back into speech that was removed.
    limit = len(levels) if dropped_after is None else dropped_after
    limit = min(limit, last + 1 + round(TAIL_EXTENSION_SECONDS / 0.01))
    while last + 1 < limit and levels[last + 1] >= tail_threshold:
        last += 1
    keep_frames = round(0.05 * SAMPLE_RATE)
    start_frame = max(0, first * window_frames - keep_frames)
    end_frame = min(len(payload) // SAMPLE_WIDTH,
                    (last + 1) * window_frames + keep_frames)
    return payload[start_frame * SAMPLE_WIDTH:end_frame * SAMPLE_WIDTH]


def _noise_bed(source, frame_count, seed):
    """Make non-periodic low noise with the measured native noise RMS."""
    rms = _pcm_rms(source, SAMPLE_WIDTH) if source else 0
    # Deterministic xorshift avoids a dependency on numpy and preserves a
    # realistic floor. Clamp pathological source windows to about -48 dBFS.
    amplitude = min(rms, 130)
    if amplitude <= 0:
        return b"\x00\x00" * frame_count
    state = (seed or 1) & 0xFFFFFFFF
    samples = bytearray(frame_count * 2)
    fade_frames = min(round(0.01 * SAMPLE_RATE), max(0, frame_count // 2))
    for index in range(frame_count):
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        envelope = 1.0
        if fade_frames:
            envelope = min(1.0, index / fade_frames,
                           (frame_count - 1 - index) / fade_frames)
        value = int(((state & 0xFFFF) / 32767.5 - 1.0)
                    * amplitude * max(0.0, envelope))
        struct.pack_into("<h", samples, index * 2, value)
    return bytes(samples)


def join_wav_parts(parts, output, gaps_ms=None, outro_ms=DEFAULT_OUTRO_MS):
    parts = [Path(item) for item in parts]
    if not parts:
        raise ValueError("no WAV parts to join")
    if gaps_ms is None:
        gaps_ms = [500] * (len(parts) - 1)
    if len(gaps_ms) != len(parts) - 1:
        raise ValueError("expected %d gaps, got %d" %
                         (len(parts) - 1, len(gaps_ms)))
    raw_payloads = _pcm_contract(parts)
    payloads = [_clean_pcm(item) for item in raw_payloads]
    quiet = min((_quietest_window(item) for item in raw_payloads),
                key=lambda item: _pcm_rms(item, SAMPLE_WIDTH))
    assembled = []
    for index, payload in enumerate(payloads):
        assembled.append(payload)
        if index < len(gaps_ms):
            frames = round(max(0, int(gaps_ms[index])) * SAMPLE_RATE / 1000)
            assembled.append(_noise_bed(quiet, frames, index + 1))
    assembled.append(_noise_bed(
        quiet, round(max(0, int(outro_ms)) * SAMPLE_RATE / 1000), 0xA01))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with wave.open(str(temporary), "wb") as target:
        target.setnchannels(CHANNELS)
        target.setsampwidth(SAMPLE_WIDTH)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(b"".join(assembled))
    os.replace(temporary, output)
    return inspect_wav(output)


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def serialise_parts(parts):
    return [asdict(item) if isinstance(item, NarrationPart) else dict(item)
            for item in parts]
