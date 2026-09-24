#!/usr/bin/env python3
"""Measure the usable bandwidth of one reference WAV inside the TTS container.

Reference-free synthesis draws a speaker from a distribution holding both
wideband and narrowband voices, so an otherwise good take can come out audibly
dull. That is a spectral question and needs a real transform, which is why it
runs here rather than in the dependency-free host module. The WAV is read with
the standard library so no audio decoder has to be importable.
"""

from __future__ import annotations

import argparse
import json
import struct
import wave

import torch


def measure(path):
    with wave.open(path, "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        sample_rate = source.getframerate()
        payload = source.readframes(source.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit PCM audio")
    samples = torch.tensor(
        [value[0] for value in struct.iter_unpack("<h", payload)],
        dtype=torch.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(dim=1)
    size = 8192
    if samples.numel() < size:
        raise ValueError("audio is too short to measure")
    spectrum = torch.stft(
        samples, n_fft=size, hop_length=size // 2,
        window=torch.hann_window(size), return_complex=True).abs()
    power = (spectrum ** 2).mean(dim=1)
    total = power.sum()
    if total <= 0:
        return {"cutoff_hz": 0.0, "sample_rate": sample_rate}
    frequencies = torch.linspace(0, sample_rate / 2, power.numel())
    # The highest frequency still holding 0.01% of the energy above it, which
    # lands on the shoulder of a band limit rather than on the noise floor.
    remaining = torch.flip(torch.cumsum(torch.flip(power, [0]), 0), [0]) / total
    cutoff = frequencies[(remaining > 0.0001).nonzero()[-1]].item()
    result = {"cutoff_hz": round(cutoff, 1), "sample_rate": int(sample_rate),
            "above_8khz_percent": round(float(power[frequencies >= 8000].sum() / total * 100), 4),
            "above_12khz_percent": round(float(power[frequencies >= 12000].sum() / total * 100), 4),
            "clip_percent": round(float((samples.abs() >= 32735).float().mean() * 100), 4)}
    # Speech-only energy helps distinguish an unchanged band edge from a loss
    # of treble throughout that band. It remains content/voice dependent.
    frames = (samples / 32768.0).unfold(0, 2048, 512)
    rms = frames.square().mean(dim=1).sqrt()
    active = frames[rms > 0.01]  # -40 dBFS, matching the legacy comparison.
    if len(active):
        ps = torch.fft.rfft(active * torch.hann_window(2048)).abs().square().mean(dim=0)
        fs = torch.linspace(0, sample_rate / 2, ps.numel())
        energy = ps.sum()
        result["active_speech"] = {
            "frame_count": len(active),
            "spectral_centroid_hz": round(float((fs * ps).sum() / energy), 1),
            "rolloff_95_hz": round(float(fs[torch.searchsorted(ps.cumsum(0), energy * .95)]), 1),
            "band_energy_db": {str(hz): round(float(10 * torch.log10(
                (ps[fs >= hz].sum() / energy).clamp(min=1e-12))), 2)
                for hz in (4000, 6000, 8000, 10000, 12000)}}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="+")
    paths = parser.parse_args().wav
    results = [measure(path) for path in paths]
    print(json.dumps(results[0] if len(results) == 1 else results))


if __name__ == "__main__":
    main()
