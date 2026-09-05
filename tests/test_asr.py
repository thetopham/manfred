from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manfred_ears.asr import FasterWhisperASR, ParakeetASR, StubASR, require_full_session_vad


class FakeWhisperSegment:
    start = 0.0
    end = 1.0
    text = " quiet foreground speech "
    avg_logprob = -0.2
    no_speech_prob = 0.1


class FakeWhisperInfo:
    language = "en"
    language_probability = 0.99


class FakeRepeatedNoiseSegment:
    start = 1.0
    end = 2.0
    text = " RRRRRRRRRRRRRRR "
    avg_logprob = -0.4
    no_speech_prob = 0.01


class FakeWhisperModel:
    def __init__(self, segments=None) -> None:
        self.calls = []
        self.segments = segments or [FakeWhisperSegment()]

    def transcribe(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return iter(self.segments), FakeWhisperInfo()


class FakeHypothesis:
    text = "Demerzel heard Omi clearly."
    timestamp = {
        "segment": [
            {"start": 0.0, "end": 1.25, "segment": "Demerzel heard Omi clearly."},
        ]
    }


class FakeParakeetModel:
    def __init__(self) -> None:
        self.calls = []

    def transcribe(self, **kwargs):
        self.calls.append(kwargs)
        return [FakeHypothesis()]


class ParakeetTests(unittest.TestCase):
    def test_ungated_backend_fails_closed_for_full_session_transcription(self) -> None:
        require_full_session_vad(StubASR())
        with self.assertRaisesRegex(RuntimeError, "full-session speech filtering"):
            require_full_session_vad(
                ParakeetASR(
                    model="nvidia/parakeet-unified-en-0.6b",
                    device="cuda",
                    loaded_model=FakeParakeetModel(),
                )
            )

    def test_parakeet_adapter_preserves_text_timestamps_and_honest_confidence(self) -> None:
        model = FakeParakeetModel()
        backend = ParakeetASR(
            model="nvidia/parakeet-unified-en-0.6b",
            device="cuda",
            loaded_model=model,
        )
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"fixture")
            result = backend.transcribe(audio)
        self.assertEqual(result.text, "Demerzel heard Omi clearly.")
        self.assertEqual(result.model, "parakeet:nvidia/parakeet-unified-en-0.6b:cuda")
        self.assertEqual(result.language, "en")
        self.assertIsNone(result.confidence)
        self.assertIsNone(result.avg_logprob)
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].start, 0.0)
        self.assertEqual(result.segments[0].end, 1.25)
        self.assertEqual(model.calls[0]["audio"], [str(audio)])
        self.assertTrue(model.calls[0]["return_hypotheses"])


class FasterWhisperTests(unittest.TestCase):
    def test_pathological_single_character_noise_is_not_transcript_text(self) -> None:
        model = FakeWhisperModel([FakeWhisperSegment(), FakeRepeatedNoiseSegment()])
        backend = FasterWhisperASR(
            model="large-v3-turbo",
            loaded_model=model,
            vad_parameters={"threshold": 0.1},
        )
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"fixture")
            result = backend.transcribe(audio)

        self.assertEqual(result.text, "quiet foreground speech")
        self.assertEqual(len(result.segments), 1)

    def test_every_transcription_path_uses_configured_silero_vad(self) -> None:
        model = FakeWhisperModel()
        vad_parameters = {
            "threshold": 0.1,
            "neg_threshold": 0.05,
            "min_speech_duration_ms": 50,
            "min_silence_duration_ms": 300,
            "speech_pad_ms": 300,
        }
        rolling_vad_parameters = {
            **vad_parameters,
            "threshold": 0.5,
            "neg_threshold": 0.35,
        }
        backend = FasterWhisperASR(
            model="large-v3-turbo",
            device="cpu",
            compute_type="int8",
            loaded_model=model,
            vad_parameters=vad_parameters,
            rolling_vad_parameters=rolling_vad_parameters,
        )
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"fixture")
            result = backend.transcribe_selected_speech(audio)

        self.assertEqual(result.text, "quiet foreground speech")
        args, kwargs = model.calls[0]
        self.assertEqual(args, (str(audio),))
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(kwargs["vad_parameters"], rolling_vad_parameters)
        self.assertFalse(kwargs["condition_on_previous_text"])
        self.assertEqual(kwargs["language"], "en")

        backend.transcribe(audio)
        _, full_session_kwargs = model.calls[1]
        self.assertTrue(full_session_kwargs["vad_filter"])
        self.assertEqual(full_session_kwargs["vad_parameters"], vad_parameters)
        self.assertFalse(full_session_kwargs["condition_on_previous_text"])
        self.assertEqual(full_session_kwargs["language"], "en")


if __name__ == "__main__":
    unittest.main()
