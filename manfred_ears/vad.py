from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Protocol


class SpeechGate(Protocol):
    @property
    def name(self) -> str: ...

    def is_speech(self, pcm16le: bytes, sample_rate: int) -> bool: ...


@dataclass(frozen=True)
class EnergySpeechGate:
    """Deterministic frame-energy gate used before bounded rolling ASR.

    It is intentionally conservative and dependency-free. Raw silence is still
    archived; this gate only decides whether a chunk should enter an ASR window.
    """

    threshold_dbfs: float = -45.0
    minimum_active_ratio: float = 0.08
    frame_milliseconds: int = 20
    active_ratio_window_seconds: float = 1.0
    name: str = "energy-v2"

    def is_speech(self, pcm16le: bytes, sample_rate: int) -> bool:
        if not pcm16le or len(pcm16le) % 2 or sample_rate <= 0:
            return False
        sample_count = len(pcm16le) // 2
        samples = struct.unpack(f"<{sample_count}h", pcm16le)
        frame_samples = max(1, sample_rate * self.frame_milliseconds // 1000)
        active = 0
        total = 0
        for offset in range(0, sample_count, frame_samples):
            frame = samples[offset : offset + frame_samples]
            if not frame:
                continue
            total += 1
            mean_square = sum(sample * sample for sample in frame) / len(frame)
            if mean_square <= 0:
                continue
            rms = math.sqrt(mean_square)
            dbfs = 20.0 * math.log10(rms / 32768.0)
            if dbfs >= self.threshold_dbfs:
                active += 1
        reference_frames = min(
            total,
            max(1, round(self.active_ratio_window_seconds * 1000 / self.frame_milliseconds)),
        )
        required_active_frames = max(1, math.ceil(reference_frames * self.minimum_active_ratio))
        return total > 0 and active >= required_active_frames
