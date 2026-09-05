from __future__ import annotations

import math
import re
import struct
import wave
from pathlib import Path
from typing import Any


def normalize_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower())


def word_error_rate(reference: str, hypothesis: str) -> dict[str, Any]:
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (ref_word != hyp_word)))
        previous = current
    edits = previous[-1]
    return {
        "reference_words": len(ref),
        "hypothesis_words": len(hyp),
        "word_edits": edits,
        "wer": edits / len(ref) if ref else (0.0 if not hyp else 1.0),
    }


def pcm16_metrics(raw: bytes) -> dict[str, Any]:
    if len(raw) % 2:
        raise ValueError("PCM16 metrics require an even byte count")
    samples = struct.unpack(f"<{len(raw) // 2}h", raw) if raw else ()
    peak = max((abs(value) for value in samples), default=0)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) if samples else 0.0
    clipped = sum(1 for value in samples if value in (-32768, 32767))
    dc_offset = sum(samples) / len(samples) if samples else 0.0
    return {
        "peak_abs": peak,
        "peak_fraction": peak / 32768.0,
        "rms": rms,
        "rms_dbfs": 20 * math.log10(rms / 32768.0) if rms else None,
        "clipped_samples": clipped,
        "clipped_fraction": clipped / len(samples) if samples else 0.0,
        "dc_offset": dc_offset,
    }


def wav_metrics(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if channels != 1 or sample_width != 2:
        raise ValueError("benchmark expects mono PCM16 WAV")
    quality = pcm16_metrics(raw)
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "frames": frames,
        "duration_seconds": frames / sample_rate if sample_rate else 0.0,
        **quality,
    }
