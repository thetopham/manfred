from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manfred_ears.config import Settings


class SettingsTests(unittest.TestCase):
    def test_silero_vad_parameters_are_backend_owned_and_environment_configurable(self) -> None:
        environment = {
            "MANFRED_ROLLING_TRANSCRIPTION_ENABLED": "true",
            "MANFRED_SILERO_VAD_THRESHOLD": "0.12",
            "MANFRED_SILERO_VAD_NEG_THRESHOLD": "0.06",
            "MANFRED_SILERO_VAD_ROLLING_THRESHOLD": "0.55",
            "MANFRED_SILERO_VAD_ROLLING_NEG_THRESHOLD": "0.40",
            "MANFRED_SILERO_VAD_MIN_SPEECH_DURATION_MS": "60",
            "MANFRED_SILERO_VAD_MIN_SILENCE_DURATION_MS": "350",
            "MANFRED_SILERO_VAD_SPEECH_PAD_MS": "275",
        }
        with patch.dict(os.environ, environment):
            settings = Settings.from_env()

        self.assertTrue(settings.rolling_transcription_enabled)
        self.assertEqual(
            settings.silero_vad_parameters(),
            {
                "threshold": 0.12,
                "neg_threshold": 0.06,
                "min_speech_duration_ms": 60,
                "min_silence_duration_ms": 350,
                "speech_pad_ms": 275,
            },
        )
        self.assertEqual(
            settings.rolling_silero_vad_parameters(),
            {
                "threshold": 0.55,
                "neg_threshold": 0.40,
                "min_speech_duration_ms": 60,
                "min_silence_duration_ms": 350,
                "speech_pad_ms": 275,
            },
        )

    def test_silero_vad_defaults_preserve_low_signal_wearable_speech(self) -> None:
        settings = Settings(state_dir=Path("/tmp/manfred-settings-test"))
        self.assertFalse(settings.rolling_transcription_enabled)
        self.assertEqual(settings.silero_vad_threshold, 0.1)
        self.assertEqual(settings.silero_vad_neg_threshold, 0.05)
        self.assertEqual(settings.silero_vad_rolling_threshold, 0.5)
        self.assertEqual(settings.silero_vad_rolling_neg_threshold, 0.35)
        self.assertEqual(settings.silero_vad_min_speech_duration_ms, 50)
        self.assertEqual(settings.silero_vad_min_silence_duration_ms, 300)
        self.assertEqual(settings.silero_vad_speech_pad_ms, 300)

    def test_invalid_silero_vad_thresholds_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Silero VAD thresholds"):
            Settings(
                state_dir=Path("/tmp/manfred-settings-test"),
                silero_vad_threshold=0.05,
                silero_vad_neg_threshold=0.1,
            )

    def test_daily_timezone_is_iana_configurable_and_fail_closed(self) -> None:
        self.assertEqual(
            Settings(state_dir=Path("/tmp/manfred-settings-test")).daily_timezone,
            "America/Denver",
        )
        with patch.dict(os.environ, {"MANFRED_DAILY_TIMEZONE": "America/New_York"}):
            self.assertEqual(Settings.from_env().daily_timezone, "America/New_York")
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            Settings(
                state_dir=Path("/tmp/manfred-settings-test"),
                daily_timezone="not/a-real-zone",
            )


if __name__ == "__main__":
    unittest.main()
