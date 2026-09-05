from __future__ import annotations

import math
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manfred_ears.vad import EnergySpeechGate


def tone(seconds: float, amplitude: int = 4000, sample_rate: int = 16000) -> bytes:
    values = [
        int(amplitude * math.sin(2 * math.pi * 220 * index / sample_rate))
        for index in range(int(seconds * sample_rate))
    ]
    return struct.pack(f"<{len(values)}h", *values)


class EnergySpeechGateTests(unittest.TestCase):
    def test_long_silence_is_gated_without_losing_raw_bytes(self) -> None:
        gate = EnergySpeechGate()
        silence = b"\x00\x00" * (16000 * 180)
        self.assertFalse(gate.is_speech(silence, 16000))
        self.assertEqual(len(silence), 16000 * 180 * 2)

    def test_sustained_voice_energy_is_selected(self) -> None:
        self.assertTrue(EnergySpeechGate().is_speech(tone(1.0), 16000))

    def test_short_utterance_survives_inside_a_valid_long_compatibility_chunk(self) -> None:
        brief_utterance = tone(0.1) + b"\x00\x00" * int(16000 * 14.9)
        self.assertTrue(EnergySpeechGate().is_speech(brief_utterance, 16000))

    def test_tiny_click_does_not_open_a_speech_window(self) -> None:
        click = tone(0.02) + b"\x00\x00" * int(16000 * 0.98)
        self.assertFalse(EnergySpeechGate().is_speech(click, 16000))


if __name__ == "__main__":
    unittest.main()
