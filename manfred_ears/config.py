from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    daily_timezone: str = "America/Denver"
    auth_token: str | None = None
    operator_token: str | None = None
    vision_token: str | None = None
    min_sample_rate: int = 8_000
    max_sample_rate: int = 48_000
    max_body_bytes: int = 1_048_576
    max_vision_request_bytes: int = 8_388_608
    max_vision_image_bytes: int = 2_097_152
    episode_context_seconds: float = 15.0
    episode_finalize_grace_seconds: float = 2.0
    max_chunk_duration_seconds: float = 15.0
    session_gap_seconds: float = 90.0
    idle_flush_seconds: float = 20.0
    worker_poll_seconds: float = 5.0
    rolling_window_seconds: float = 10.0
    rolling_trailing_silence_seconds: float = 1.0
    rolling_recovery_seconds: float = 60.0
    rolling_reconcile_seconds: float = 3600.0
    rolling_transcription_enabled: bool = False
    vad_threshold_dbfs: float = -45.0
    vad_minimum_active_ratio: float = 0.08
    silero_vad_threshold: float = 0.1
    silero_vad_neg_threshold: float = 0.05
    silero_vad_rolling_threshold: float = 0.5
    silero_vad_rolling_neg_threshold: float = 0.35
    silero_vad_min_speech_duration_ms: int = 50
    silero_vad_min_silence_duration_ms: int = 300
    silero_vad_speech_pad_ms: int = 300
    asr_backend: str = "faster-whisper"
    asr_model: str = "large-v3-turbo"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    source_device: str = "omi-devkit-2"
    phone_bridge: str = "samsung-galaxy-s25"
    source_transport: str = "omi-stock-app-audio-bytes-webhook"
    relay: str = "omi-cloud"

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.daily_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("daily timezone must be a valid IANA timezone") from exc
        if self.episode_context_seconds <= 0:
            raise ValueError("episode context seconds must be positive")
        if self.episode_finalize_grace_seconds < 0:
            raise ValueError("episode finalize grace seconds must be non-negative")
        if not 0.0 <= self.silero_vad_neg_threshold <= self.silero_vad_threshold <= 1.0:
            raise ValueError(
                "Silero VAD thresholds must satisfy 0 <= neg_threshold <= threshold <= 1"
            )
        if not (
            0.0
            <= self.silero_vad_rolling_neg_threshold
            <= self.silero_vad_rolling_threshold
            <= 1.0
        ):
            raise ValueError(
                "Rolling Silero VAD thresholds must satisfy "
                "0 <= neg_threshold <= threshold <= 1"
            )
        durations = (
            self.silero_vad_min_speech_duration_ms,
            self.silero_vad_min_silence_duration_ms,
            self.silero_vad_speech_pad_ms,
        )
        if any(value < 0 for value in durations):
            raise ValueError("Silero VAD durations must be non-negative")

    def silero_vad_parameters(self) -> dict[str, float | int]:
        """Parameters for faster-whisper's packaged full-session Silero VAD.

        These stay backend-owned so the phone app remains a capture/transport
        client rather than an ASR tuning console.
        """
        return {
            "threshold": self.silero_vad_threshold,
            "neg_threshold": self.silero_vad_neg_threshold,
            "min_speech_duration_ms": self.silero_vad_min_speech_duration_ms,
            "min_silence_duration_ms": self.silero_vad_min_silence_duration_ms,
            "speech_pad_ms": self.silero_vad_speech_pad_ms,
        }

    def rolling_silero_vad_parameters(self) -> dict[str, float | int]:
        parameters = self.silero_vad_parameters()
        parameters["threshold"] = self.silero_vad_rolling_threshold
        parameters["neg_threshold"] = self.silero_vad_rolling_neg_threshold
        return parameters

    @classmethod
    def from_env(cls) -> "Settings":
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
        state_dir = Path(os.environ.get("MANFRED_STATE_DIR", str(home / "manfred-ears"))).expanduser()
        token = os.environ.get("MANFRED_AUTH_TOKEN") or None
        operator_token = os.environ.get("MANFRED_OPERATOR_TOKEN") or None
        vision_token = os.environ.get("MANFRED_VISION_TOKEN") or None
        asr_backend = os.environ.get("MANFRED_ASR_BACKEND", "faster-whisper")
        default_model = (
            "nvidia/parakeet-unified-en-0.6b"
            if asr_backend == "parakeet"
            else "large-v3-turbo"
        )
        default_device = "cuda" if asr_backend == "parakeet" else "cpu"
        return cls(
            state_dir=state_dir,
            daily_timezone=os.environ.get("MANFRED_DAILY_TIMEZONE", "America/Denver"),
            auth_token=token,
            operator_token=operator_token,
            vision_token=vision_token,
            min_sample_rate=int(os.environ.get("MANFRED_MIN_SAMPLE_RATE", "8000")),
            max_sample_rate=int(os.environ.get("MANFRED_MAX_SAMPLE_RATE", "48000")),
            max_body_bytes=int(os.environ.get("MANFRED_MAX_BODY_BYTES", "1048576")),
            max_vision_request_bytes=int(
                os.environ.get("MANFRED_MAX_VISION_REQUEST_BYTES", "8388608")
            ),
            max_vision_image_bytes=int(
                os.environ.get("MANFRED_MAX_VISION_IMAGE_BYTES", "2097152")
            ),
            episode_context_seconds=float(
                os.environ.get("MANFRED_EPISODE_CONTEXT_SECONDS", "15")
            ),
            episode_finalize_grace_seconds=float(
                os.environ.get("MANFRED_EPISODE_FINALIZE_GRACE_SECONDS", "2")
            ),
            max_chunk_duration_seconds=float(
                os.environ.get("MANFRED_MAX_CHUNK_DURATION_SECONDS", "15")
            ),
            session_gap_seconds=float(os.environ.get("MANFRED_SESSION_GAP_SECONDS", "90")),
            idle_flush_seconds=float(os.environ.get("MANFRED_IDLE_FLUSH_SECONDS", "20")),
            worker_poll_seconds=float(os.environ.get("MANFRED_WORKER_POLL_SECONDS", "5")),
            rolling_window_seconds=float(os.environ.get("MANFRED_ROLLING_WINDOW_SECONDS", "10")),
            rolling_trailing_silence_seconds=float(
                os.environ.get("MANFRED_ROLLING_TRAILING_SILENCE_SECONDS", "1")
            ),
            rolling_recovery_seconds=float(os.environ.get("MANFRED_ROLLING_RECOVERY_SECONDS", "60")),
            rolling_reconcile_seconds=float(os.environ.get("MANFRED_ROLLING_RECONCILE_SECONDS", "3600")),
            rolling_transcription_enabled=_env_bool(
                "MANFRED_ROLLING_TRANSCRIPTION_ENABLED", False
            ),
            vad_threshold_dbfs=float(os.environ.get("MANFRED_VAD_THRESHOLD_DBFS", "-45")),
            vad_minimum_active_ratio=float(os.environ.get("MANFRED_VAD_MINIMUM_ACTIVE_RATIO", "0.08")),
            silero_vad_threshold=float(os.environ.get("MANFRED_SILERO_VAD_THRESHOLD", "0.1")),
            silero_vad_neg_threshold=float(
                os.environ.get("MANFRED_SILERO_VAD_NEG_THRESHOLD", "0.05")
            ),
            silero_vad_rolling_threshold=float(
                os.environ.get("MANFRED_SILERO_VAD_ROLLING_THRESHOLD", "0.5")
            ),
            silero_vad_rolling_neg_threshold=float(
                os.environ.get("MANFRED_SILERO_VAD_ROLLING_NEG_THRESHOLD", "0.35")
            ),
            silero_vad_min_speech_duration_ms=int(
                os.environ.get("MANFRED_SILERO_VAD_MIN_SPEECH_DURATION_MS", "50")
            ),
            silero_vad_min_silence_duration_ms=int(
                os.environ.get("MANFRED_SILERO_VAD_MIN_SILENCE_DURATION_MS", "300")
            ),
            silero_vad_speech_pad_ms=int(
                os.environ.get("MANFRED_SILERO_VAD_SPEECH_PAD_MS", "300")
            ),
            asr_backend=asr_backend,
            asr_model=os.environ.get("MANFRED_ASR_MODEL", default_model),
            asr_device=os.environ.get("MANFRED_ASR_DEVICE", default_device),
            asr_compute_type=os.environ.get("MANFRED_ASR_COMPUTE_TYPE", "int8"),
        )
