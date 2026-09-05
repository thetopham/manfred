"""Manfred Ears v0: evidence-first wearable audio ingestion."""

from .archive import AudioArchive, CaptureMetadata, IngestResult
from .asr import FasterWhisperASR, ParakeetASR, StubASR, TranscriptResult
from .chat_mirror import ChatMirrorArchive, ChatMirrorIngestResult
from .config import Settings
from .episodes import EpisodeArchive, EpisodeRefreshResult
from .vision import VisionArchive, VisionIngestResult

__all__ = [
    "AudioArchive",
    "CaptureMetadata",
    "ChatMirrorArchive",
    "ChatMirrorIngestResult",
    "EpisodeArchive",
    "EpisodeRefreshResult",
    "FasterWhisperASR",
    "IngestResult",
    "Settings",
    "ParakeetASR",
    "StubASR",
    "TranscriptResult",
    "VisionArchive",
    "VisionIngestResult",
]
