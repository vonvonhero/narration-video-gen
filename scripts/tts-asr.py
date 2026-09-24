#!/usr/bin/env python3
"""Transcribe one narration job inside the isolated TTS container."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
import whisper

from narration_video_gen.narration import classify_transcript, write_json_atomic


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--model-root", default="/models/whisper")
    args = parser.parse_args()
    manifest_path = args.job / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model("base", device=device, download_root=args.model_root)
    summaries = {"pass": 0, "review": 0, "fail": 0, "error": 0}
    for part in manifest.get("parts", []):
        audio = args.job / part["audio"]
        try:
            result = model.transcribe(
                str(audio), language="ja", fp16=device == "cuda",
                word_timestamps=True, temperature=0,
                condition_on_previous_text=False)
            accepted = [part["text"]]
            # Whisper commonly writes character names as their homophones.
            # These are acoustically indistinguishable, so treating them as a
            # spoken mismatch only creates noise and can hide real extra speech.
            name_variants = {"葵": "青い", "さくら": "桜"}
            for written, heard in name_variants.items():
                if written in part["text"]:
                    accepted.append(part["text"].replace(written, heard))
            classification = classify_transcript(result["text"], accepted)
            status = classification["status"]
            bucket = ("pass" if status == "pass" else
                      "fail" if status.startswith("fail") else "review")
            summaries[bucket] += 1
            part["asr"] = {
                "transcript": result["text"].strip(),
                "segments": [{
                    "start": item["start"], "end": item["end"],
                    "text": item["text"].strip(),
                    "avg_logprob": item.get("avg_logprob"),
                    "no_speech_prob": item.get("no_speech_prob"),
                } for item in result["segments"]],
                **classification,
            }
            quality = part.setdefault("quality", {})
            quality["issues"] = [
                issue for issue in quality.get("issues", [])
                if not issue.startswith("ASR result requires listening review:")]
            if status != "pass":
                quality["issues"].append(
                    "ASR result requires listening review: %s" % status)
            quality["status"] = "review" if quality["issues"] else "pass"
        except Exception as exc:
            summaries["error"] += 1
            part["asr"] = {"status": "error", "error": str(exc)}
    manifest["asr"] = {
        "engine": "openai-whisper", "model": "base",
        "version": whisper.__version__, "device": device,
        "summary": summaries,
    }
    write_json_atomic(manifest_path, manifest)
    print(json.dumps(manifest["asr"], ensure_ascii=False))


if __name__ == "__main__":
    main()
