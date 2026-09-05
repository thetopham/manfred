from __future__ import annotations

import importlib
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    speaker: str | None = None
    speaker_confidence: float | None = None


@dataclass
class TranscriptResult:
    text: str
    model: str
    language: str | None
    language_probability: float | None
    confidence: float | None
    avg_logprob: float | None
    no_speech_probability: float | None
    latency_seconds: float
    segments: list[TranscriptSegment] = field(default_factory=list)
    diarization_status: str = "not_run"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["segments"] = [asdict(segment) for segment in self.segments]
        return value


class ASRBackend(Protocol):
    name: str
    supports_full_session_vad: bool

    def transcribe(self, audio_path: Path) -> TranscriptResult: ...


def require_full_session_vad(asr: ASRBackend) -> None:
    if not getattr(asr, "supports_full_session_vad", False):
        raise RuntimeError(
            "ASR backend does not provide full-session speech filtering; "
            "refusing ungated production transcription"
        )


def _pathological_single_character_repetition(text: str) -> bool:
    alphanumeric = "".join(character for character in text.casefold() if character.isalnum())
    return len(alphanumeric) >= 6 and len(set(alphanumeric)) == 1


class StubASR:
    """Deterministic test backend; never selected by the production default."""

    supports_full_session_vad = True

    def __init__(self, text: str = "deterministic test transcript", name: str = "stub-asr") -> None:
        self.text = text
        self.name = name

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        started = time.perf_counter()
        return TranscriptResult(
            text=self.text,
            model=self.name,
            language="en",
            language_probability=1.0,
            confidence=None,
            avg_logprob=None,
            no_speech_probability=None,
            latency_seconds=time.perf_counter() - started,
            segments=[TranscriptSegment(start=0.0, end=0.0, text=self.text)],
        )


class FasterWhisperASR:
    supports_full_session_vad = True

    def __init__(
        self,
        model: str,
        device: str = "cpu",
        compute_type: str = "int8",
        *,
        loaded_model: Any | None = None,
        vad_parameters: dict[str, Any] | None = None,
        rolling_vad_parameters: dict[str, Any] | None = None,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.name = f"faster-whisper:{model}:{device}:{compute_type}"
        self._model = loaded_model
        self.vad_parameters = dict(vad_parameters or {})
        self.rolling_vad_parameters = dict(rolling_vad_parameters or self.vad_parameters)

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return self._model

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        return self._transcribe(
            audio_path,
            vad_filter=True,
            condition_on_previous_text=False,
            vad_parameters=self.vad_parameters,
        )

    def transcribe_selected_speech(self, audio_path: Path) -> TranscriptResult:
        """Transcribe a rolling window with the backend's packaged Silero VAD.

        The durable outer energy gate is intentionally only a cheap prefilter.
        It must not be trusted as proof of human speech because movement, wind,
        traffic, and animal sounds can all cross an energy threshold.
        """
        return self._transcribe(
            audio_path,
            vad_filter=True,
            condition_on_previous_text=False,
            vad_parameters=self.rolling_vad_parameters,
        )

    def _transcribe(
        self,
        audio_path: Path,
        *,
        vad_filter: bool,
        condition_on_previous_text: bool,
        vad_parameters: dict[str, Any],
    ) -> TranscriptResult:
        started = time.perf_counter()
        model = self._load()
        raw_segments, info = model.transcribe(
            str(audio_path),
            beam_size=5,
            vad_filter=vad_filter,
            vad_parameters=vad_parameters if vad_filter else None,
            word_timestamps=False,
            condition_on_previous_text=condition_on_previous_text,
            language="en",
        )
        segments: list[TranscriptSegment] = []
        weighted_logprob = 0.0
        weighted_seconds = 0.0
        weighted_no_speech = 0.0
        for segment in raw_segments:
            text = str(segment.text).strip()
            if _pathological_single_character_repetition(text):
                continue
            duration = max(0.001, float(segment.end) - float(segment.start))
            avg_logprob = float(segment.avg_logprob) if segment.avg_logprob is not None else None
            no_speech = float(segment.no_speech_prob) if segment.no_speech_prob is not None else None
            segments.append(
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=text,
                    avg_logprob=avg_logprob,
                    no_speech_prob=no_speech,
                )
            )
            if avg_logprob is not None:
                weighted_logprob += avg_logprob * duration
                weighted_seconds += duration
            if no_speech is not None:
                weighted_no_speech += no_speech * duration
        avg = weighted_logprob / weighted_seconds if weighted_seconds else None
        no_speech_avg = weighted_no_speech / weighted_seconds if weighted_seconds else None
        # Whisper log probabilities are preserved directly. Converting them into a
        # calibrated 0-1 confidence would overstate what the model supplies.
        return TranscriptResult(
            text=" ".join(segment.text for segment in segments if segment.text).strip(),
            model=self.name,
            language=getattr(info, "language", None),
            language_probability=float(getattr(info, "language_probability", 0.0)) or None,
            confidence=None,
            avg_logprob=avg,
            no_speech_probability=no_speech_avg,
            latency_seconds=time.perf_counter() - started,
            segments=segments,
        )


class ParakeetASR:
    """Lazy NVIDIA NeMo Parakeet adapter for offline session transcription."""

    supports_full_session_vad = False

    def __init__(self, model: str, device: str = "cuda", *, loaded_model: Any | None = None) -> None:
        self.model_name = model
        self.device = device
        self.name = f"parakeet:{model}:{device}"
        self._model = loaded_model

    def _load(self):
        if self._model is None:
            try:
                torch = importlib.import_module("torch")
                ASRModel = importlib.import_module("nemo.collections.asr.models").ASRModel
            except ImportError as exc:
                raise RuntimeError(
                    "Parakeet requires a separate NeMo ASR environment; install requirements-parakeet.txt"
                ) from exc
            if self.device.startswith("cuda") and not torch.cuda.is_available():
                raise RuntimeError("Parakeet CUDA backend requested but CUDA is unavailable")
            self._model = ASRModel.from_pretrained(model_name=self.model_name)
            self._model.eval()
            if hasattr(self._model, "to"):
                self._model.to(self.device)
        return self._model

    @staticmethod
    def _segments(hypothesis: Any) -> list[TranscriptSegment]:
        timestamps = getattr(hypothesis, "timestamp", None) or {}
        raw_segments = timestamps.get("segment") or timestamps.get("word") or []
        segments: list[TranscriptSegment] = []
        for raw in raw_segments:
            if not isinstance(raw, dict):
                continue
            text = raw.get("segment") or raw.get("word") or raw.get("text") or ""
            start = raw.get("start", 0.0)
            end = raw.get("end", start)
            segments.append(
                TranscriptSegment(
                    start=float(start),
                    end=float(end),
                    text=str(text).strip(),
                )
            )
        return segments

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        started = time.perf_counter()
        model = self._load()
        arguments = {
            "audio": [str(audio_path)],
            "batch_size": 1,
            "return_hypotheses": True,
        }
        try:
            outputs = model.transcribe(**arguments, timestamps=True)
        except TypeError:
            # Older NeMo releases attach timestamps without accepting the flag.
            outputs = model.transcribe(**arguments)
        if not outputs:
            hypothesis: Any = ""
        else:
            hypothesis = outputs[0]
        text = hypothesis if isinstance(hypothesis, str) else getattr(hypothesis, "text", "")
        segments = [] if isinstance(hypothesis, str) else self._segments(hypothesis)
        if not segments and text:
            segments = [TranscriptSegment(start=0.0, end=0.0, text=str(text).strip())]
        # NeMo hypothesis scores are decoder scores, not calibrated confidence.
        return TranscriptResult(
            text=str(text).strip(),
            model=self.name,
            language="en",
            language_probability=None,
            confidence=None,
            avg_logprob=None,
            no_speech_probability=None,
            latency_seconds=time.perf_counter() - started,
            segments=segments,
        )


def build_asr(
    backend: str,
    model: str,
    device: str,
    compute_type: str,
    *,
    vad_parameters: dict[str, Any] | None = None,
    rolling_vad_parameters: dict[str, Any] | None = None,
) -> ASRBackend:
    if backend == "stub":
        return StubASR()
    if backend == "faster-whisper":
        return FasterWhisperASR(
            model=model,
            device=device,
            compute_type=compute_type,
            vad_parameters=vad_parameters,
            rolling_vad_parameters=rolling_vad_parameters,
        )
    if backend == "parakeet":
        return ParakeetASR(model=model, device=device)
    raise ValueError(f"unsupported ASR backend: {backend}")
