from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, cast
from zoneinfo import ZoneInfo

from .asr import ASRBackend, TranscriptResult, TranscriptSegment
from .config import Settings
from .metrics import normalize_words, pcm16_metrics
from .vad import SpeechGate

WIKI_EXPORT_MAX_BYTES = 60_000


def _bounded_wiki_export(text: str, max_bytes: int = WIKI_EXPORT_MAX_BYTES) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    marker = (
        f"\n\n[TRUNCATED MIDDLE: daily Wiki scratch exceeded {max_bytes} UTF-8 bytes; "
        f"original was {len(encoded)} bytes. Preserved earliest and latest context; "
        "search the canonical transcript archive on 7090 for the omitted middle.]\n\n"
    )
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        raise ValueError("Wiki export byte limit is too small for the truncation marker")
    content_budget = max_bytes - len(marker_bytes)
    head_budget = content_budget // 2
    tail_budget = content_budget - head_budget
    head = encoded[:head_budget].decode("utf-8", errors="ignore").rstrip()
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore").lstrip()
    bounded = head + marker + tail
    if len(bounded.encode("utf-8")) > max_bytes:
        raise AssertionError("bounded Wiki export exceeded its byte contract")
    return bounded, True


@dataclass(frozen=True)
class IngestResult:
    chunk_id: str
    session_id: str
    duplicate: bool
    duration_seconds: float
    sha256: str


@dataclass(frozen=True)
class CaptureMetadata:
    """Phone-originated capture facts for one decoded PCM chunk."""

    source_session_id: str
    sequence_number: int
    capture_started_at: datetime
    capture_ended_at: datetime
    codec: str
    transport: str
    decoder_generation: int | None = None

    def validate(self, *, audio_duration_seconds: float) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.source_session_id):
            raise ValueError("capture session id is invalid")
        if not 0 <= self.sequence_number < 2**63:
            raise ValueError("capture sequence is invalid")
        if self.capture_started_at.tzinfo is None or self.capture_ended_at.tzinfo is None:
            raise ValueError("capture timestamps must include a timezone")
        started = self.capture_started_at.astimezone(timezone.utc)
        ended = self.capture_ended_at.astimezone(timezone.utc)
        if ended <= started:
            raise ValueError("capture end must follow capture start")
        stated_duration = (ended - started).total_seconds()
        tolerance = max(0.1, audio_duration_seconds * 0.1)
        if abs(stated_duration - audio_duration_seconds) > tolerance:
            raise ValueError("capture timestamps disagree with PCM duration")
        if self.codec != "pcm16le":
            raise ValueError("direct bridge payload codec must be pcm16le")
        if not re.fullmatch(r"[a-z0-9._-]{1,64}", self.transport):
            raise ValueError("capture transport is invalid")
        if self.decoder_generation is not None and not 0 <= self.decoder_generation < 2**31:
            raise ValueError("decoder generation is invalid")

    def as_record(self) -> dict[str, Any]:
        return {
            "source_session_id": self.source_session_id,
            "sequence_number": self.sequence_number,
            "capture_started_at": self.capture_started_at.astimezone(timezone.utc).isoformat(),
            "capture_ended_at": self.capture_ended_at.astimezone(timezone.utc).isoformat(),
            "codec": self.codec,
            "transport": self.transport,
            "decoder_generation": self.decoder_generation,
        }


class AudioArchive:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.state_dir
        self.daily_timezone = ZoneInfo(settings.daily_timezone)
        self.db_path = self.root / "archive.sqlite3"
        for path in (
            self.root,
            self.root / "raw",
            self.root / "events",
            self.root / "wiki-inbox",
            self.root / "audit",
            self.root / "deletions",
            self.root / "tmp",
        ):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        self._init_db()
        self._reconcile_session_deletions()

    def _daily_date(self, value: datetime) -> date:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.daily_timezone).date()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA foreign_keys=ON")
        for attempt in range(20):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    conn.close()
                    raise
                time.sleep(0.05 * (attempt + 1))
        return conn

    @contextmanager
    def _transcript_write_lock(self, session_id: str) -> Iterator[None]:
        lock_name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        path = self.root / "tmp" / f"transcript-{lock_name}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _archive_mutation_lock(self) -> Iterator[None]:
        path = self.root / "tmp" / "archive-mutation.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _day_export_lock(self, day: date) -> Iterator[None]:
        path = self.root / "tmp" / f"day-export-{day.isoformat()}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _ensure_private_directory(self, path: Path) -> None:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        path.mkdir(parents=True, exist_ok=True)
        for directory in reversed(missing):
            os.chmod(directory, 0o700)
            self._fsync_directory(directory)
            self._fsync_directory(directory.parent)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    uid_hash TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    first_receive_epoch REAL NOT NULL,
                    last_receive_epoch REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','transcribed','error')),
                    last_error TEXT,
                    transcription_failures INTEGER NOT NULL DEFAULT 0,
                    next_transcription_epoch REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS sessions_uid_open
                    ON sessions(uid_hash,status,last_receive_epoch);

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    uid_hash TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    received_at TEXT NOT NULL,
                    received_epoch REAL NOT NULL,
                    sample_rate INTEGER NOT NULL,
                    num_bytes INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    sha256 TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','processed','error')) DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS chunks_session_time
                    ON chunks(session_id,received_epoch,chunk_id);

                CREATE TABLE IF NOT EXISTS transcripts (
                    transcript_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    observed_date TEXT NOT NULL,
                    transcribed_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    language TEXT,
                    confidence REAL,
                    avg_logprob REAL,
                    no_speech_probability REAL,
                    latency_seconds REAL NOT NULL,
                    duration_seconds REAL NOT NULL,
                    audio_sha256 TEXT NOT NULL,
                    text TEXT NOT NULL,
                    event_path TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    UNIQUE(session_id,version)
                );
                CREATE INDEX IF NOT EXISTS transcripts_day ON transcripts(observed_date,transcribed_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
                    transcript_id UNINDEXED,
                    session_id UNINDEXED,
                    text,
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TABLE IF NOT EXISTS rolling_windows (
                    window_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('processing','committed','failed')),
                    created_at TEXT NOT NULL,
                    claimed_epoch REAL NOT NULL,
                    claim_token TEXT NOT NULL,
                    committed_at TEXT,
                    chunk_ids_json TEXT NOT NULL,
                    first_sequence INTEGER,
                    last_sequence INTEGER,
                    sample_rate INTEGER,
                    duration_seconds REAL,
                    audio_sha256 TEXT,
                    observed_start TEXT,
                    observed_end TEXT,
                    timestamp_basis TEXT,
                    model TEXT,
                    language TEXT,
                    latency_seconds REAL,
                    text TEXT,
                    result_json TEXT,
                    event_path TEXT,
                    event_json TEXT,
                    last_error TEXT,
                    retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0,1))
                );
                CREATE INDEX IF NOT EXISTS rolling_windows_session_status
                    ON rolling_windows(session_id,status,claimed_epoch);

                CREATE TABLE IF NOT EXISTS rolling_chunk_state (
                    chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    decision TEXT NOT NULL CHECK(decision IN ('speech','silence')),
                    gate TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    window_id TEXT REFERENCES rolling_windows(window_id) ON DELETE SET NULL,
                    finalized INTEGER NOT NULL DEFAULT 0 CHECK(finalized IN (0,1))
                );
                CREATE INDEX IF NOT EXISTS rolling_chunk_state_pending
                    ON rolling_chunk_state(session_id,finalized,window_id);

                CREATE TABLE IF NOT EXISTS rolling_segments (
                    segment_id TEXT PRIMARY KEY,
                    window_id TEXT NOT NULL REFERENCES rolling_windows(window_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    segment_index INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('provisional','finalized','superseded')),
                    reconciled_by TEXT,
                    observed_date TEXT NOT NULL,
                    observed_start TEXT NOT NULL,
                    observed_end TEXT NOT NULL,
                    start_sample INTEGER NOT NULL,
                    end_sample INTEGER NOT NULL,
                    sample_rate INTEGER NOT NULL,
                    source_chunk_ids_json TEXT NOT NULL,
                    source_sequence_first INTEGER,
                    source_sequence_last INTEGER,
                    model TEXT NOT NULL,
                    asr_revision TEXT NOT NULL,
                    text TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    UNIQUE(window_id,segment_index)
                );
                CREATE INDEX IF NOT EXISTS rolling_segments_session_state
                    ON rolling_segments(session_id,state,reconciled_by,observed_start);
                CREATE INDEX IF NOT EXISTS rolling_segments_day
                    ON rolling_segments(observed_date,observed_start);
                CREATE VIRTUAL TABLE IF NOT EXISTS rolling_segments_fts USING fts5(
                    segment_id UNINDEXED,
                    session_id UNINDEXED,
                    text,
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TABLE IF NOT EXISTS search_documents (
                    document_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('canonical_transcript','rolling_segment')),
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS search_documents_session_kind
                    ON search_documents(session_id,kind);
                CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(
                    document_id UNINDEXED,
                    kind UNINDEXED,
                    session_id UNINDEXED,
                    text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS search_documents_insert
                AFTER INSERT ON search_documents BEGIN
                    INSERT INTO search_documents_fts(document_id,kind,session_id,text)
                    VALUES (new.document_id,new.kind,new.session_id,new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS search_documents_delete
                AFTER DELETE ON search_documents BEGIN
                    DELETE FROM search_documents_fts WHERE document_id=old.document_id;
                END;
                CREATE TRIGGER IF NOT EXISTS search_documents_update
                AFTER UPDATE OF kind,session_id,text ON search_documents BEGIN
                    DELETE FROM search_documents_fts WHERE document_id=old.document_id;
                    INSERT INTO search_documents_fts(document_id,kind,session_id,text)
                    VALUES (new.document_id,new.kind,new.session_id,new.text);
                END;
                """
            )
            migrations = {
                "sessions": (
                    "source_session_id TEXT",
                    "transport TEXT",
                    "transcription_failures INTEGER NOT NULL DEFAULT 0",
                    "next_transcription_epoch REAL NOT NULL DEFAULT 0",
                ),
                "chunks": (
                    "source_session_id TEXT",
                    "sequence_number INTEGER",
                    "capture_started_at TEXT",
                    "capture_ended_at TEXT",
                    "capture_started_epoch REAL",
                    "capture_ended_epoch REAL",
                    "codec TEXT",
                    "transport TEXT",
                    "decoder_generation INTEGER",
                    "pcm_peak_abs INTEGER",
                    "pcm_rms REAL",
                    "pcm_clipped_samples INTEGER",
                    "pcm_dc_offset REAL",
                ),
                "rolling_windows": (
                    "claim_token TEXT",
                    "retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0,1))",
                ),
            }
            for table, definitions in migrations.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for definition in definitions:
                    column = definition.split()[0]
                    if column not in existing:
                        try:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
                        except sqlite3.OperationalError as exc:
                            # Receiver and operator processes may migrate the same
                            # archive concurrently during first direct-bridge boot.
                            if "duplicate column name" not in str(exc).lower():
                                raise
            stale_capture_epochs = conn.execute(
                """SELECT chunk_id,capture_started_at,capture_ended_at
                   FROM chunks
                   WHERE capture_started_at IS NOT NULL AND capture_ended_at IS NOT NULL
                     AND (capture_started_epoch IS NULL OR capture_ended_epoch IS NULL)"""
            ).fetchall()
            for row in stale_capture_epochs:
                try:
                    started = datetime.fromisoformat(row["capture_started_at"])
                    ended = datetime.fromisoformat(row["capture_ended_at"])
                    if started.tzinfo is None or ended.tzinfo is None:
                        continue
                    started_epoch = started.astimezone(timezone.utc).timestamp()
                    ended_epoch = ended.astimezone(timezone.utc).timestamp()
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    """UPDATE chunks SET capture_started_epoch=?,capture_ended_epoch=?
                       WHERE chunk_id=?""",
                    (started_epoch, ended_epoch, row["chunk_id"]),
                )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS chunks_capture_interval
                   ON chunks(capture_started_epoch,capture_ended_epoch,chunk_id)"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS sessions_uid_source_session
                   ON sessions(uid_hash,source_session_id)
                   WHERE source_session_id IS NOT NULL"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS chunks_source_sequence
                   ON chunks(uid_hash,source_session_id,sequence_number)
                   WHERE source_session_id IS NOT NULL AND sequence_number IS NOT NULL"""
            )
            conn.execute(
                """DELETE FROM search_documents
                   WHERE kind='canonical_transcript' AND document_id NOT IN (
                       SELECT t.transcript_id FROM transcripts t
                       JOIN (
                           SELECT session_id,MAX(version) AS version
                           FROM transcripts GROUP BY session_id
                       ) latest ON latest.session_id=t.session_id AND latest.version=t.version
                   )"""
            )
            conn.execute(
                """DELETE FROM search_documents
                   WHERE kind='rolling_segment' AND document_id NOT IN (
                       SELECT segment_id FROM rolling_segments
                       WHERE state='provisional' AND reconciled_by IS NULL
                   )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO search_documents(document_id,kind,session_id,text)
                   SELECT t.transcript_id,'canonical_transcript',t.session_id,t.text
                   FROM transcripts t
                   JOIN (
                       SELECT session_id,MAX(version) AS version
                       FROM transcripts GROUP BY session_id
                   ) latest ON latest.session_id=t.session_id AND latest.version=t.version"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO search_documents(document_id,kind,session_id,text)
                   SELECT segment_id,'rolling_segment',session_id,text FROM rolling_segments
                   WHERE state='provisional' AND reconciled_by IS NULL"""
            )
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def hash_uid(uid: str) -> str:
        return hashlib.sha256(uid.encode("utf-8")).hexdigest()

    def _verify_indexed_chunk(self, row: sqlite3.Row, expected_sha256: str) -> None:
        evidence = self.root / str(row["path"])
        if evidence.is_symlink() or not evidence.is_file():
            raise RuntimeError("indexed raw chunk evidence is missing")
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != expected_sha256:
            raise RuntimeError("indexed raw chunk evidence is corrupt")

    def _audit(self, action: str, **fields: Any) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **fields,
        }
        path = self.root / "audit" / "events.jsonl"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def ingest_pcm(
        self,
        *,
        uid: str,
        sample_rate: int,
        body: bytes,
        idempotency_key: str | None = None,
        claimed_sha256: str | None = None,
        received_at: datetime | None = None,
        capture: CaptureMetadata | None = None,
    ) -> IngestResult:
        with self._archive_mutation_lock():
            return self._ingest_pcm_unlocked(
                uid=uid,
                sample_rate=sample_rate,
                body=body,
                idempotency_key=idempotency_key,
                claimed_sha256=claimed_sha256,
                received_at=received_at,
                capture=capture,
            )

    def _ingest_pcm_unlocked(
        self,
        *,
        uid: str,
        sample_rate: int,
        body: bytes,
        idempotency_key: str | None = None,
        claimed_sha256: str | None = None,
        received_at: datetime | None = None,
        capture: CaptureMetadata | None = None,
    ) -> IngestResult:
        if not uid or len(uid) > 512:
            raise ValueError("uid must be present and bounded")
        if not self.settings.min_sample_rate <= sample_rate <= self.settings.max_sample_rate:
            raise ValueError("sample_rate outside configured bounds")
        if not body:
            raise ValueError("audio body is empty")
        if len(body) > self.settings.max_body_bytes:
            raise ValueError("audio body exceeds configured maximum")
        if len(body) % 2:
            raise ValueError("PCM16 body must contain an even number of bytes")
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
            raise ValueError("Idempotency-Key length is invalid")

        received = received_at or datetime.now(timezone.utc)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        received = received.astimezone(timezone.utc)
        epoch = received.timestamp()
        uid_hash = self.hash_uid(uid)
        digest = hashlib.sha256(body).hexdigest()
        if claimed_sha256 is not None:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", claimed_sha256):
                raise ValueError("declared PCM SHA-256 is invalid")
            if claimed_sha256.lower() != digest:
                raise ValueError("declared PCM SHA-256 does not match audio")
        if capture is not None and idempotency_key:
            embedded_digest = idempotency_key.rsplit(":", 1)[-1]
            if re.fullmatch(r"[0-9a-fA-F]{64}", embedded_digest) and embedded_digest.lower() != digest:
                raise ValueError("Idempotency-Key embedded SHA-256 does not match audio")
        duration = len(body) / (sample_rate * 2)
        if duration > self.settings.max_chunk_duration_seconds:
            raise ValueError("audio chunk duration exceeds configured maximum")
        capture_record: dict[str, Any] | None = None
        if capture is not None:
            capture.validate(audio_duration_seconds=duration)
            capture_record = capture.as_record()
        quality = pcm16_metrics(body)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT chunk_id,session_id,duration_seconds,sha256,path FROM chunks WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    if existing["sha256"] != digest:
                        conn.rollback()
                        raise ValueError("Idempotency-Key was reused with different audio")
                    self._verify_indexed_chunk(existing, digest)
                    conn.rollback()
                    return IngestResult(
                        chunk_id=existing["chunk_id"],
                        session_id=existing["session_id"],
                        duplicate=True,
                        duration_seconds=float(existing["duration_seconds"]),
                        sha256=existing["sha256"],
                    )

            if capture_record is not None:
                existing = conn.execute(
                    """SELECT chunk_id,session_id,duration_seconds,sha256,path FROM chunks
                       WHERE uid_hash=? AND source_session_id=? AND sequence_number=?""",
                    (uid_hash, capture_record["source_session_id"], capture_record["sequence_number"]),
                ).fetchone()
                if existing:
                    if existing["sha256"] != digest:
                        conn.rollback()
                        raise ValueError("capture sequence was reused with different audio")
                    self._verify_indexed_chunk(existing, digest)
                    conn.rollback()
                    return IngestResult(
                        chunk_id=existing["chunk_id"],
                        session_id=existing["session_id"],
                        duplicate=True,
                        duration_seconds=float(existing["duration_seconds"]),
                        sha256=existing["sha256"],
                    )
                current = conn.execute(
                    "SELECT * FROM sessions WHERE uid_hash=? AND source_session_id=? LIMIT 1",
                    (uid_hash, capture_record["source_session_id"]),
                ).fetchone()
            else:
                current = conn.execute(
                    """SELECT * FROM sessions
                       WHERE uid_hash=? AND status IN ('open','error')
                       ORDER BY last_receive_epoch DESC LIMIT 1""",
                    (uid_hash,),
                ).fetchone()

            within_gap = current and epoch - float(current["last_receive_epoch"]) <= self.settings.session_gap_seconds
            if current and (capture_record is not None or within_gap):
                session_id = current["session_id"]
                conn.execute(
                    """UPDATE sessions
                       SET last_receive_epoch=?,status='open',last_error=NULL,
                           transcription_failures=0,next_transcription_epoch=0
                       WHERE session_id=?""",
                    (epoch, session_id),
                )
            else:
                prefix = "s25" if capture_record is not None else "omi"
                session_id = f"{prefix}-{received.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """INSERT INTO sessions
                       (session_id,uid_hash,opened_at,first_receive_epoch,last_receive_epoch,status,last_error,
                        source_session_id,transport)
                       VALUES (?,?,?,?,?,'open',NULL,?,?)""",
                    (
                        session_id,
                        uid_hash,
                        received.isoformat(),
                        epoch,
                        epoch,
                        capture_record["source_session_id"] if capture_record else None,
                        capture_record["transport"] if capture_record else None,
                    ),
                )

            chunk_id = f"chunk-{uuid.uuid4().hex}"
            evidence_date = (
                datetime.fromisoformat(capture_record["capture_started_at"]).date()
                if capture_record
                else received.date()
            )
            relative = Path("raw") / evidence_date.isoformat() / session_id / f"{chunk_id}.pcm"
            destination = self.root / relative
            self._ensure_private_directory(destination.parent)
            os.chmod(destination.parent, 0o700)
            temporary = destination.with_suffix(".pcm.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            self._fsync_directory(destination.parent)
            conn.execute(
                """INSERT INTO chunks
                   (chunk_id,idempotency_key,uid_hash,session_id,received_at,received_epoch,
                    sample_rate,num_bytes,duration_seconds,sha256,path,status,source_session_id,
                    sequence_number,capture_started_at,capture_ended_at,
                    capture_started_epoch,capture_ended_epoch,codec,transport,
                    decoder_generation,pcm_peak_abs,pcm_rms,pcm_clipped_samples,pcm_dc_offset)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    chunk_id,
                    idempotency_key,
                    uid_hash,
                    session_id,
                    received.isoformat(),
                    epoch,
                    sample_rate,
                    len(body),
                    duration,
                    digest,
                    str(relative),
                    capture_record["source_session_id"] if capture_record else None,
                    capture_record["sequence_number"] if capture_record else None,
                    capture_record["capture_started_at"] if capture_record else None,
                    capture_record["capture_ended_at"] if capture_record else None,
                    capture.capture_started_at.astimezone(timezone.utc).timestamp()
                    if capture is not None
                    else None,
                    capture.capture_ended_at.astimezone(timezone.utc).timestamp()
                    if capture is not None
                    else None,
                    capture_record["codec"] if capture_record else "pcm16le",
                    capture_record["transport"] if capture_record else self.settings.source_transport,
                    capture_record["decoder_generation"] if capture_record else None,
                    quality["peak_abs"],
                    quality["rms"],
                    quality["clipped_samples"],
                    quality["dc_offset"],
                ),
            )
            conn.commit()
        self._audit(
            "chunk_ingested",
            chunk_id=chunk_id,
            session_id=session_id,
            sha256=digest,
            num_bytes=len(body),
            sample_rate=sample_rate,
            source_session_id=capture_record["source_session_id"] if capture_record else None,
            sequence_number=capture_record["sequence_number"] if capture_record else None,
            capture_started_at=capture_record["capture_started_at"] if capture_record else None,
            capture_ended_at=capture_record["capture_ended_at"] if capture_record else None,
            transport=capture_record["transport"] if capture_record else self.settings.source_transport,
            decoder_generation=capture_record["decoder_generation"] if capture_record else None,
            pcm_quality=quality,
        )
        return IngestResult(chunk_id, session_id, False, duration, digest)

    def _session_chunks(self, session_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM chunks WHERE session_id=?
                   ORDER BY CASE WHEN sequence_number IS NULL THEN 1 ELSE 0 END,
                            sequence_number,received_epoch,chunk_id""",
                (session_id,),
            ).fetchall()

    def _reconciliation_chunks(self, session_id: str) -> list[sqlite3.Row]:
        """Return only evidence already classified and durably committed for rolling ASR."""
        with self._connect() as conn:
            return conn.execute(
                """SELECT c.* FROM chunks c
                   LEFT JOIN rolling_chunk_state r ON r.chunk_id=c.chunk_id
                   LEFT JOIN rolling_windows w ON w.window_id=r.window_id
                   WHERE c.session_id=? AND (
                       c.status='processed'
                       OR (r.finalized=1 AND (r.window_id IS NULL OR w.status='committed'))
                   )
                   ORDER BY CASE WHEN c.sequence_number IS NULL THEN 1 ELSE 0 END,
                            c.sequence_number,c.received_epoch,c.chunk_id""",
                (session_id,),
            ).fetchall()

    def _assemble_temp_wav(self, session_id: str, chunks: list[sqlite3.Row]) -> tuple[Path, str, float]:
        if not chunks:
            raise ValueError("session has no chunks")
        rates = {int(row["sample_rate"]) for row in chunks}
        if len(rates) != 1:
            raise ValueError("session contains mixed sample rates")
        sample_rate = rates.pop()
        digest = hashlib.sha256()
        fd, raw_name = tempfile.mkstemp(prefix=f"{session_id}-", suffix=".wav", dir=self.root / "tmp")
        os.close(fd)
        path = Path(raw_name)
        os.chmod(path, 0o600)
        total_bytes = 0
        try:
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                for row in chunks:
                    data = (self.root / row["path"]).read_bytes()
                    if hashlib.sha256(data).hexdigest() != row["sha256"]:
                        raise ValueError(f"raw chunk hash mismatch: {row['chunk_id']}")
                    digest.update(data)
                    total_bytes += len(data)
                    output.writeframesraw(data)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path, digest.hexdigest(), total_bytes / (sample_rate * 2)

    def _read_verified_chunk(self, row: sqlite3.Row) -> bytes:
        data = (self.root / row["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(f"raw chunk hash mismatch: {row['chunk_id']}")
        return data

    @staticmethod
    def _chunk_capture_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "chunk_id": row["chunk_id"],
            "sequence_number": row["sequence_number"],
            "capture_started_at": row["capture_started_at"],
            "capture_ended_at": row["capture_ended_at"],
            "codec": row["codec"],
            "transport": row["transport"],
            "decoder_generation": row["decoder_generation"],
            "pcm_quality": {
                "peak_abs": row["pcm_peak_abs"],
                "rms": row["pcm_rms"],
                "clipped_samples": row["pcm_clipped_samples"],
                "dc_offset": row["pcm_dc_offset"],
            },
        }

    @staticmethod
    def _sequence_summary(chunks: list[sqlite3.Row]) -> dict[str, Any] | None:
        sequence_numbers = [int(row["sequence_number"]) for row in chunks if row["sequence_number"] is not None]
        if not sequence_numbers:
            return None
        ordered = sorted(set(sequence_numbers))
        first = ordered[0]
        last = ordered[-1]
        missing_count = (last - first + 1) - len(ordered)
        missing: list[int] = []
        for left, right in zip(ordered, ordered[1:]):
            if len(missing) >= 1000:
                break
            take = min(right - left - 1, 1000 - len(missing))
            if take > 0:
                missing.extend(range(left + 1, left + 1 + take))
        return {
            "first": first,
            "last": last,
            "missing": missing,
            "missing_count": missing_count,
            "missing_truncated": missing_count > len(missing),
        }

    @staticmethod
    def _observed_interval(
        chunks: list[sqlite3.Row], duration: float
    ) -> tuple[datetime, datetime, str, str]:
        if all(row["capture_started_at"] and row["capture_ended_at"] for row in chunks):
            starts = [datetime.fromisoformat(row["capture_started_at"]) for row in chunks]
            ends = [datetime.fromisoformat(row["capture_ended_at"]) for row in chunks]
            return (
                min(starts),
                max(ends),
                "phone-derived-capture-time",
                "s25_pcm_sample_clock",
            )
        received = [datetime.fromisoformat(row["received_at"]) for row in chunks]
        observed_end = max(received)
        return (
            observed_end - timedelta(seconds=duration),
            observed_end,
            "relay-arrival-estimate",
            "last_relay_receive_time_minus_window_pcm_duration",
        )

    def _evidence_digest(self, chunks: list[sqlite3.Row]) -> tuple[str, float]:
        if not chunks:
            raise ValueError("session has no chunks")
        rates = {int(row["sample_rate"]) for row in chunks}
        if len(rates) != 1:
            raise ValueError("session contains mixed sample rates")
        sample_rate = rates.pop()
        digest = hashlib.sha256()
        total_bytes = 0
        for row in chunks:
            data = self._read_verified_chunk(row)
            digest.update(data)
            total_bytes += len(data)
        return digest.hexdigest(), total_bytes / (sample_rate * 2)

    def record_rolling_error(self, session_id: str, error: Exception) -> None:
        bounded = f"{type(error).__name__}: {str(error)[:500]}"
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_error=? WHERE session_id=?",
                (bounded, session_id),
            )
        self._audit(
            "rolling_cycle_failed",
            session_id=session_id,
            error_type=type(error).__name__,
        )

    def _classify_rolling_chunks(self, session_id: str, gate: SpeechGate) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT c.* FROM chunks c
                   LEFT JOIN rolling_chunk_state r ON r.chunk_id=c.chunk_id
                   WHERE c.session_id=? AND c.status='pending' AND r.chunk_id IS NULL
                   ORDER BY CASE WHEN c.sequence_number IS NULL THEN 1 ELSE 0 END,
                            c.sequence_number,c.received_epoch,c.chunk_id""",
                (session_id,),
            ).fetchall()
        decisions: list[tuple[str, str, str, str]] = []
        decided_at = datetime.now(timezone.utc).isoformat()
        for row in rows:
            data = self._read_verified_chunk(row)
            decision = "speech" if gate.is_speech(data, int(row["sample_rate"])) else "silence"
            decisions.append((row["chunk_id"], session_id, decision, gate.name))
        if decisions:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    """INSERT OR IGNORE INTO rolling_chunk_state
                       (chunk_id,session_id,decision,gate,decided_at,window_id,finalized)
                       VALUES (?,?,?,?,?,NULL,0)""",
                    ((chunk_id, sid, decision, gate_name, decided_at) for chunk_id, sid, decision, gate_name in decisions),
                )
                conn.commit()
        with self._connect() as conn:
            boundary_rows = conn.execute(
                """SELECT c.*,r.decision FROM rolling_chunk_state r
                   JOIN chunks c ON c.chunk_id=r.chunk_id
                   WHERE r.session_id=? AND r.finalized=0 AND r.window_id IS NULL
                   ORDER BY CASE WHEN c.sequence_number IS NULL THEN 1 ELSE 0 END,
                            c.sequence_number,c.received_epoch,c.chunk_id""",
                (session_id,),
            ).fetchall()
        boundary_speech_ids: set[str] = set()
        for left, right in zip(boundary_rows, boundary_rows[1:]):
            if left["decision"] != "silence" or right["decision"] != "silence":
                continue
            if int(left["sample_rate"]) != int(right["sample_rate"]):
                continue
            left_sequence = left["sequence_number"]
            right_sequence = right["sequence_number"]
            if (
                left_sequence is not None
                and right_sequence is not None
                and int(right_sequence) != int(left_sequence) + 1
            ):
                continue
            combined = self._read_verified_chunk(left) + self._read_verified_chunk(right)
            if gate.is_speech(combined, int(left["sample_rate"])):
                boundary_speech_ids.update((str(left["chunk_id"]), str(right["chunk_id"])))
        if boundary_speech_ids:
            with self._connect() as conn:
                conn.executemany(
                    """UPDATE rolling_chunk_state SET decision='speech',gate=?,decided_at=?
                       WHERE session_id=? AND chunk_id=? AND finalized=0 AND window_id IS NULL""",
                    (
                        (gate.name, decided_at, session_id, chunk_id)
                        for chunk_id in sorted(boundary_speech_ids)
                    ),
                )
                conn.commit()
        return len(decisions)

    def _claim_rolling_window(
        self,
        session_id: str,
        *,
        force: bool,
        now_epoch: float,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """SELECT * FROM rolling_windows
                   WHERE session_id=? AND status='processing' AND retired=0
                   ORDER BY claimed_epoch LIMIT 1""",
                (session_id,),
            ).fetchone()
            retry_before = now_epoch - self.settings.rolling_recovery_seconds
            if active is not None and float(active["claimed_epoch"]) > retry_before:
                conn.rollback()
                return None
            retry = conn.execute(
                """SELECT * FROM rolling_windows
                   WHERE session_id=? AND retired=0
                     AND (status='failed' OR (status='processing' AND claimed_epoch<=?))
                   ORDER BY created_at LIMIT 1""",
                (session_id, retry_before),
            ).fetchone()
            if retry is not None:
                claim_token = uuid.uuid4().hex
                conn.execute(
                    """UPDATE rolling_windows
                       SET status='processing',claimed_epoch=?,claim_token=?,last_error=NULL
                       WHERE window_id=?""",
                    (now_epoch, claim_token, retry["window_id"]),
                )
                conn.commit()
                value = dict(retry)
                value["status"] = "processing"
                value["claimed_epoch"] = now_epoch
                value["claim_token"] = claim_token
                return value

            rows = conn.execute(
                """SELECT c.*,r.decision FROM rolling_chunk_state r
                   JOIN chunks c ON c.chunk_id=r.chunk_id
                   WHERE r.session_id=? AND r.finalized=0 AND r.window_id IS NULL
                   ORDER BY CASE WHEN c.sequence_number IS NULL THEN 1 ELSE 0 END,
                            c.sequence_number,c.received_epoch,c.chunk_id""",
                (session_id,),
            ).fetchall()
            first_speech = next((index for index, row in enumerate(rows) if row["decision"] == "speech"), None)
            if first_speech is None:
                finalizable_rows = rows if force else rows[:-1]
                if finalizable_rows:
                    conn.executemany(
                        "UPDATE rolling_chunk_state SET finalized=1 WHERE chunk_id=?",
                        ((row["chunk_id"],) for row in finalizable_rows),
                    )
                    conn.commit()
                else:
                    conn.rollback()
                return None

            leading = rows[:first_speech]
            span = 0.0
            trailing_silence = 0.0
            last_speech = first_speech
            close_at: int | None = None
            previous_sequence: int | None = None
            for index in range(first_speech, len(rows)):
                row = rows[index]
                current_sequence = int(row["sequence_number"]) if row["sequence_number"] is not None else None
                if (
                    index > first_speech
                    and previous_sequence is not None
                    and current_sequence is not None
                    and current_sequence != previous_sequence + 1
                ):
                    close_at = index - 1
                    break
                previous_sequence = current_sequence
                row_duration = float(row["duration_seconds"])
                if (
                    index > first_speech
                    and span > 0
                    and span + row_duration > self.settings.rolling_window_seconds
                ):
                    close_at = index - 1
                    break
                span += row_duration
                if row["decision"] == "speech":
                    last_speech = index
                    trailing_silence = 0.0
                else:
                    trailing_silence += row_duration
                if span >= self.settings.rolling_window_seconds:
                    close_at = index
                    break
                if trailing_silence >= self.settings.rolling_trailing_silence_seconds:
                    close_at = index
                    break
            if close_at is None and force:
                close_at = len(rows) - 1
            if close_at is None:
                if leading:
                    conn.executemany(
                        "UPDATE rolling_chunk_state SET finalized=1 WHERE chunk_id=?",
                        ((row["chunk_id"],) for row in leading),
                    )
                    conn.commit()
                else:
                    conn.rollback()
                return None

            selected = rows[first_speech : last_speech + 1]
            trailing = rows[last_speech + 1 : close_at + 1]
            finalizable_trailing = trailing if force else trailing[:-1]
            chunk_ids = [str(row["chunk_id"]) for row in selected]
            sequences = [int(row["sequence_number"]) for row in selected if row["sequence_number"] is not None]
            window_id = f"rolling-{session_id}-{uuid.uuid4().hex[:12]}"
            claim_token = uuid.uuid4().hex
            created_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO rolling_windows
                   (window_id,session_id,status,created_at,claimed_epoch,claim_token,chunk_ids_json,
                    first_sequence,last_sequence)
                   VALUES (?,?,'processing',?,?,?,?,?,?)""",
                (
                    window_id,
                    session_id,
                    created_at,
                    now_epoch,
                    claim_token,
                    json.dumps(chunk_ids),
                    min(sequences) if sequences else None,
                    max(sequences) if sequences else None,
                ),
            )
            if leading or finalizable_trailing:
                conn.executemany(
                    "UPDATE rolling_chunk_state SET finalized=1 WHERE chunk_id=?",
                    ((row["chunk_id"],) for row in [*leading, *finalizable_trailing]),
                )
            conn.executemany(
                "UPDATE rolling_chunk_state SET finalized=1,window_id=? WHERE chunk_id=?",
                ((window_id, chunk_id) for chunk_id in chunk_ids),
            )
            conn.commit()
            return {
                "window_id": window_id,
                "session_id": session_id,
                "status": "processing",
                "created_at": created_at,
                "claimed_epoch": now_epoch,
                "claim_token": claim_token,
                "chunk_ids_json": json.dumps(chunk_ids),
                "first_sequence": min(sequences) if sequences else None,
                "last_sequence": max(sequences) if sequences else None,
            }

    def process_incremental(
        self,
        session_id: str,
        asr: ASRBackend,
        gate: SpeechGate,
        *,
        force: bool = False,
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        with self._transcript_write_lock(session_id):
            return self._process_incremental_unlocked(
                session_id,
                asr,
                gate,
                force=force,
                now_epoch=now_epoch,
            )

    def _process_incremental_unlocked(
        self,
        session_id: str,
        asr: ASRBackend,
        gate: SpeechGate,
        *,
        force: bool = False,
        now_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        self._classify_rolling_chunks(session_id, gate)
        claimed = self._claim_rolling_window(
            session_id,
            force=force,
            now_epoch=now_epoch if now_epoch is not None else time.time(),
        )
        if claimed is None:
            return None
        chunk_ids = [str(value) for value in json.loads(claimed["chunk_ids_json"])]
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._connect() as conn:
            fetched = conn.execute(
                f"SELECT * FROM chunks WHERE session_id=? AND chunk_id IN ({placeholders})",
                (session_id, *chunk_ids),
            ).fetchall()
        by_id = {str(row["chunk_id"]): row for row in fetched}
        if set(by_id) != set(chunk_ids):
            raise ValueError("rolling window references missing chunks")
        chunks = [by_id[chunk_id] for chunk_id in chunk_ids]
        wav_path, audio_sha256, duration = self._assemble_temp_wav(claimed["window_id"], chunks)
        try:
            selected_transcribe = getattr(asr, "transcribe_selected_speech", None)
            result = (
                cast(Callable[[Path], TranscriptResult], selected_transcribe)(wav_path)
                if callable(selected_transcribe)
                else asr.transcribe(wav_path)
            )
        except Exception as exc:
            owns_claim = False
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                owns_claim = bool(
                    conn.execute(
                        """UPDATE rolling_windows SET status='failed',last_error=?
                           WHERE window_id=? AND status='processing' AND claim_token=?""",
                        (
                            f"{type(exc).__name__}: {str(exc)[:500]}",
                            claimed["window_id"],
                            claimed["claim_token"],
                        ),
                    ).rowcount
                )
                if owns_claim:
                    conn.execute(
                        "UPDATE sessions SET last_error=? WHERE session_id=?",
                        (f"{type(exc).__name__}: {str(exc)[:500]}", session_id),
                    )
                conn.commit()
            if owns_claim:
                self._audit(
                    "rolling_transcription_failed",
                    session_id=session_id,
                    window_id=claimed["window_id"],
                    error_type=type(exc).__name__,
                )
            raise
        finally:
            try:
                wav_path.unlink()
            except OSError:
                pass

        sample_rate = int(chunks[0]["sample_rate"])
        observed_start, observed_end, capture_quality, timestamp_basis = self._observed_interval(chunks, duration)
        raw_segments = result.segments
        if not raw_segments and result.text.strip():
            raw_segments = [TranscriptSegment(start=0.0, end=duration, text=result.text.strip())]
        segment_records: list[dict[str, Any]] = []
        sequences = [int(row["sequence_number"]) for row in chunks if row["sequence_number"] is not None]
        for index, segment in enumerate(raw_segments):
            text = segment.text.strip()
            if not text:
                continue
            start_seconds = max(0.0, min(duration, float(segment.start)))
            end_seconds = max(start_seconds, min(duration, float(segment.end)))
            if end_seconds <= start_seconds:
                end_seconds = duration
            start_sample = round(start_seconds * sample_rate)
            end_sample = round(end_seconds * sample_rate)
            segment_id = f"{claimed['window_id']}-s{index}"
            segment_start = observed_start + timedelta(seconds=start_seconds)
            segment_end = observed_start + timedelta(seconds=end_seconds)
            segment_event = {
                "schema_version": 1,
                "event_id": segment_id,
                "event_type": "observation.audio.transcript.segment",
                "session_id": session_id,
                "window_id": claimed["window_id"],
                "state": "provisional",
                "source": {
                    "source_session_id": chunks[0]["source_session_id"],
                    "transport": chunks[0]["transport"] or self.settings.source_transport,
                    "uid_hash": chunks[0]["uid_hash"],
                },
                "timestamps": {
                    "observed_start_estimate": segment_start.isoformat(),
                    "observed_end_estimate": segment_end.isoformat(),
                    "capture_time_quality": capture_quality,
                    "timestamp_basis": timestamp_basis,
                },
                "evidence": {
                    "canonical": "raw_audio_chunks",
                    "window_audio_sha256": audio_sha256,
                    "chunk_ids": chunk_ids,
                    "chunk_paths": [row["path"] for row in chunks],
                    "sequence_first": min(sequences) if sequences else None,
                    "sequence_last": max(sequences) if sequences else None,
                    "sample_interval": {
                        "start": start_sample,
                        "end": end_sample,
                        "sample_rate": sample_rate,
                        "basis": "rolling_window_concatenated_pcm",
                    },
                    "raw_retention": "indefinite-until-explicit-policy-change",
                },
                "transcript": {
                    "text": text,
                    "model": result.model,
                    "asr_revision": "rolling-v1",
                    "avg_logprob": segment.avg_logprob,
                    "no_speech_probability": segment.no_speech_prob,
                    "transcript_is_machine_interpretation": True,
                },
                "reconciliation": {"reconciled_by": None, "superseded_by": None},
            }
            segment_records.append(
                {
                    "segment_id": segment_id,
                    "segment_index": index,
                    "observed_date": self._daily_date(segment_start).isoformat(),
                    "observed_start": segment_start.isoformat(),
                    "observed_end": segment_end.isoformat(),
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "text": text,
                    "event": segment_event,
                }
            )

        committed_at = datetime.now(timezone.utc)
        window_event = {
            "schema_version": 1,
            "event_id": claimed["window_id"],
            "event_type": "observation.audio.transcript.window",
            "session_id": session_id,
            "state": "committed",
            "timestamps": {
                "observed_start_estimate": observed_start.isoformat(),
                "observed_end_estimate": observed_end.isoformat(),
                "committed_at": committed_at.isoformat(),
                "capture_time_quality": capture_quality,
                "timestamp_basis": timestamp_basis,
            },
            "evidence": {
                "canonical": "raw_audio_chunks",
                "audio_sha256": audio_sha256,
                "duration_seconds": duration,
                "sample_rate": sample_rate,
                "chunk_ids": chunk_ids,
                "chunk_paths": [row["path"] for row in chunks],
                "chunk_capture": [self._chunk_capture_record(row) for row in chunks],
                "sequence": self._sequence_summary(chunks),
                "derived_from": [f"audio-chunk:{chunk_id}" for chunk_id in chunk_ids],
                "raw_retention": "indefinite-until-explicit-policy-change",
            },
            "transcript": {
                **result.to_dict(),
                "state": "provisional",
                "asr_revision": "rolling-v1",
                "speech_gate": gate.name,
                "transcript_is_machine_interpretation": True,
            },
            "segments": [record["event"] for record in segment_records],
        }
        event_dir = self.root / "events" / self._daily_date(observed_start).isoformat()
        event_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(event_dir, 0o700)
        event_path = event_dir / f"{claimed['window_id']}.json"
        temporary_event_path = event_path.with_name(
            f"{event_path.name}.{claimed['claim_token']}.tmp"
        )
        fd = os.open(temporary_event_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(window_event, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status,event_json,claim_token FROM rolling_windows WHERE window_id=?",
                (claimed["window_id"],),
            ).fetchone()
            if current is None:
                conn.rollback()
                temporary_event_path.unlink(missing_ok=True)
                raise ValueError("rolling window disappeared before commit")
            if current["status"] == "committed":
                conn.rollback()
                temporary_event_path.unlink(missing_ok=True)
                return json.loads(current["event_json"])
            if current["status"] != "processing" or current["claim_token"] != claimed["claim_token"]:
                conn.rollback()
                temporary_event_path.unlink(missing_ok=True)
                return None
            os.replace(temporary_event_path, event_path)
            updated = conn.execute(
                """UPDATE rolling_windows SET
                   status='committed',committed_at=?,sample_rate=?,duration_seconds=?,audio_sha256=?,
                   observed_start=?,observed_end=?,timestamp_basis=?,model=?,language=?,latency_seconds=?,
                   text=?,result_json=?,event_path=?,event_json=?,last_error=NULL
                   WHERE window_id=? AND status='processing' AND claim_token=?""",
                (
                    committed_at.isoformat(),
                    sample_rate,
                    duration,
                    audio_sha256,
                    observed_start.isoformat(),
                    observed_end.isoformat(),
                    timestamp_basis,
                    result.model,
                    result.language,
                    result.latency_seconds,
                    result.text,
                    json.dumps(result.to_dict(), sort_keys=True),
                    str(event_path.relative_to(self.root)),
                    json.dumps(window_event, sort_keys=True),
                    claimed["window_id"],
                    claimed["claim_token"],
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                raise RuntimeError("rolling window claim was lost during commit")
            for record in segment_records:
                conn.execute(
                    """INSERT OR IGNORE INTO rolling_segments
                       (segment_id,window_id,session_id,segment_index,state,reconciled_by,observed_date,
                        observed_start,observed_end,start_sample,end_sample,sample_rate,source_chunk_ids_json,
                        source_sequence_first,source_sequence_last,model,asr_revision,text,event_json)
                       VALUES (?,?,?,?,'provisional',NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record["segment_id"],
                        claimed["window_id"],
                        session_id,
                        record["segment_index"],
                        record["observed_date"],
                        record["observed_start"],
                        record["observed_end"],
                        record["start_sample"],
                        record["end_sample"],
                        sample_rate,
                        json.dumps(chunk_ids),
                        min(sequences) if sequences else None,
                        max(sequences) if sequences else None,
                        result.model,
                        "rolling-v1",
                        record["text"],
                        json.dumps(record["event"], sort_keys=True),
                    ),
                )
                conn.execute(
                    "INSERT INTO rolling_segments_fts(segment_id,session_id,text) VALUES (?,?,?)",
                    (record["segment_id"], session_id, record["text"]),
                )
                conn.execute(
                    """INSERT INTO search_documents(document_id,kind,session_id,text)
                       VALUES (?,'rolling_segment',?,?)""",
                    (record["segment_id"], session_id, record["text"]),
                )
            conn.execute(
                "UPDATE sessions SET last_error=NULL WHERE session_id=?",
                (session_id,),
            )
            conn.commit()
        for stale_temporary in event_dir.glob(f"{event_path.name}.*.tmp"):
            stale_temporary.unlink(missing_ok=True)
        self._audit(
            "rolling_window_committed",
            session_id=session_id,
            window_id=claimed["window_id"],
            chunk_count=len(chunk_ids),
            segment_count=len(segment_records),
            model=result.model,
            duration_seconds=duration,
            latency_seconds=result.latency_seconds,
        )
        return window_event

    def open_sessions(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE status='open' ORDER BY last_receive_epoch"
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    def transcript_observed_dates(self, session_id: str) -> list[date]:
        observed_dates: set[date] = set()

        def add_interval(start_value: str, end_value: str) -> None:
            current = self._daily_date(datetime.fromisoformat(start_value))
            end = self._daily_date(datetime.fromisoformat(end_value))
            while current <= end:
                observed_dates.add(current)
                current += timedelta(days=1)

        with self._connect() as conn:
            transcript_rows = conn.execute(
                "SELECT event_json FROM transcripts WHERE session_id=?",
                (session_id,),
            ).fetchall()
            rolling_rows = conn.execute(
                "SELECT observed_start,observed_end FROM rolling_segments WHERE session_id=?",
                (session_id,),
            ).fetchall()
        for row in transcript_rows:
            event = json.loads(row["event_json"])
            add_interval(
                event["timestamps"]["observed_start_estimate"],
                event["timestamps"]["observed_end_estimate"],
            )
        for row in rolling_rows:
            add_interval(str(row["observed_start"]), str(row["observed_end"]))
        return sorted(observed_dates)

    def sessions_due_reconciliation(self, now_epoch: float | None = None) -> list[str]:
        cutoff = datetime.fromtimestamp(
            (now_epoch if now_epoch is not None else time.time()) - self.settings.rolling_reconcile_seconds,
            tz=timezone.utc,
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT s.session_id FROM sessions s
                   JOIN rolling_segments r ON r.session_id=s.session_id
                   JOIN rolling_windows w ON w.window_id=r.window_id
                   WHERE s.status='open' AND r.state='provisional' AND r.reconciled_by IS NULL
                     AND w.committed_at<=?
                   ORDER BY s.last_receive_epoch""",
                (cutoff,),
            ).fetchall()
        return [str(row["session_id"]) for row in rows]

    def reconcile_incremental_session(self, session_id: str, *, final: bool) -> dict[str, Any]:
        with self._transcript_write_lock(session_id):
            return self._reconcile_incremental_session_unlocked(session_id, final=final)

    def _reconcile_incremental_session_unlocked(
        self,
        session_id: str,
        *,
        final: bool,
        idle_cutoff_epoch: float | None = None,
    ) -> dict[str, Any]:
        chunks = self._reconciliation_chunks(session_id)
        if not chunks:
            raise KeyError(session_id)
        with self._connect() as conn:
            segments = conn.execute(
                """SELECT * FROM rolling_segments
                   WHERE session_id=? AND state!='superseded'
                   ORDER BY observed_start,window_id,segment_index""",
                (session_id,),
            ).fetchall()
            windows = conn.execute(
                """SELECT * FROM rolling_windows
                   WHERE session_id=? AND status='committed' AND retired=0
                   ORDER BY observed_start,window_id""",
                (session_id,),
            ).fetchall()
            existing = conn.execute(
                "SELECT event_json FROM transcripts WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            unreconciled = int(
                conn.execute(
                    "SELECT COUNT(*) FROM rolling_segments WHERE session_id=? AND state='provisional' AND reconciled_by IS NULL",
                    (session_id,),
                ).fetchone()[0]
            )
            pending_chunks = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE session_id=? AND status='pending'",
                    (session_id,),
                ).fetchone()[0]
            )
        existing_event: dict[str, Any] | None = (
            json.loads(existing["event_json"]) if existing is not None else None
        )
        if existing is not None and unreconciled == 0 and (not final or pending_chunks == 0):
            assert existing_event is not None
            return existing_event

        audio_sha256, duration = self._evidence_digest(chunks)
        observed_start, observed_end, capture_quality, timestamp_basis = self._observed_interval(chunks, duration)
        with self._connect() as conn:
            version = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM transcripts WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
        transcript_id = f"transcript-{session_id}-v{version}"
        transcribed_at = datetime.now(timezone.utc)
        existing_strategy = (
            existing_event.get("reconciliation", {}).get("strategy") if existing_event is not None else None
        )
        base_transcript_id: str | None = None
        base_text = ""
        base_model: str | None = None
        base_observed_start: datetime | None = None
        base_observed_end: datetime | None = None
        base_chunk_capture: list[dict[str, Any]] = []
        base_segments: list[dict[str, Any]] = []
        if existing_event is not None and existing_strategy != "compose_committed_rolling_segments":
            base_transcript_id = str(existing_event.get("event_id"))
            base_text = str(existing_event.get("transcript", {}).get("text", "")).strip()
            prior_model = existing_event.get("transcript", {}).get("model")
            base_model = str(prior_model) if prior_model else None
            base_observed_start = datetime.fromisoformat(
                existing_event["timestamps"]["observed_start_estimate"]
            )
            base_observed_end = datetime.fromisoformat(
                existing_event["timestamps"]["observed_end_estimate"]
            )
            base_chunk_capture = [
                dict(item)
                for item in existing_event.get("evidence", {}).get("chunk_capture", [])
                if isinstance(item, dict)
            ]
            base_segments = [
                {**segment, "state": "finalized", "source": "prior_canonical_transcript"}
                for segment in existing_event.get("transcript", {}).get("segments", [])
                if isinstance(segment, dict)
            ]
        elif existing_event is not None:
            reconciliation = existing_event.get("reconciliation", {})
            inherited_id = reconciliation.get("base_transcript_id")
            base_transcript_id = str(inherited_id) if inherited_id else None
            base_text = str(reconciliation.get("base_transcript_text") or "").strip()
            inherited_model = reconciliation.get("base_transcript_model")
            base_model = str(inherited_model) if inherited_model else None
            inherited_start = reconciliation.get("base_transcript_observed_start")
            inherited_end = reconciliation.get("base_transcript_observed_end")
            if inherited_start:
                base_observed_start = datetime.fromisoformat(str(inherited_start))
            elif base_transcript_id:
                base_observed_start = datetime.fromisoformat(
                    existing_event["timestamps"]["observed_start_estimate"]
                )
            if inherited_end:
                base_observed_end = datetime.fromisoformat(str(inherited_end))
            elif base_transcript_id:
                base_observed_end = datetime.fromisoformat(
                    existing_event["timestamps"]["observed_end_estimate"]
                )
            base_chunk_capture = [
                dict(item)
                for item in reconciliation.get("base_transcript_chunk_capture", [])
                if isinstance(item, dict)
            ]
            if not base_chunk_capture and base_transcript_id:
                base_chunk_capture = [
                    dict(item)
                    for item in existing_event.get("evidence", {}).get("chunk_capture", [])
                    if isinstance(item, dict)
                ]
            base_segments = [
                dict(segment)
                for segment in reconciliation.get("base_transcript_segments", [])
                if isinstance(segment, dict)
            ]
        def base_offset_observed(offset_seconds: float) -> datetime:
            fallback_start = base_observed_start or observed_start
            cumulative = 0.0
            timed_chunks = [
                item
                for item in base_chunk_capture
                if item.get("capture_started_at") and item.get("capture_ended_at")
            ]
            for index, item in enumerate(timed_chunks):
                chunk_start = datetime.fromisoformat(str(item["capture_started_at"]))
                chunk_end = datetime.fromisoformat(str(item["capture_ended_at"]))
                chunk_duration = max(0.0, (chunk_end - chunk_start).total_seconds())
                boundary = cumulative + chunk_duration
                if offset_seconds < boundary or index == len(timed_chunks) - 1:
                    within_chunk = min(max(0.0, offset_seconds - cumulative), chunk_duration)
                    return chunk_start + timedelta(seconds=within_chunk)
                cumulative = boundary
            return fallback_start + timedelta(seconds=max(0.0, offset_seconds))

        transcript_parts: list[tuple[datetime, int, str]] = []
        base_segment_parts = [
            (
                base_offset_observed(float(segment.get("start", 0.0))),
                0,
                str(segment.get("text", "")).strip(),
            )
            for segment in base_segments
            if str(segment.get("text", "")).strip()
        ]
        if base_segment_parts:
            transcript_parts.extend(base_segment_parts)
        elif base_text:
            transcript_parts.append((base_observed_start or observed_start, 0, base_text))
        transcript_parts.extend(
            (datetime.fromisoformat(row["observed_start"]), 1, str(row["text"]).strip())
            for row in segments
            if str(row["text"]).strip()
        )
        transcript_parts.sort(key=lambda part: (part[0], part[1]))
        text = " ".join(part[2] for part in transcript_parts).strip()
        models = {str(row["model"]) for row in segments}
        if base_model:
            models.add(base_model)
        model = "rolling-reconciliation:" + ("+".join(sorted(models)) if models else "no-speech")
        compatibility_segments = [
            {
                **segment,
                "start": max(
                    0.0,
                    (
                        base_offset_observed(float(segment.get("start", 0.0)))
                        - observed_start
                    ).total_seconds(),
                ),
                "end": max(
                    0.0,
                    (
                        base_offset_observed(
                            float(segment.get("end", segment.get("start", 0.0)))
                        )
                        - observed_start
                    ).total_seconds(),
                ),
            }
            for segment in base_segments
        ]
        compatibility_segments.extend(
            {
                "start": max(
                    0.0,
                    (datetime.fromisoformat(row["observed_start"]) - observed_start).total_seconds(),
                ),
                "end": max(
                    0.0,
                    (datetime.fromisoformat(row["observed_end"]) - observed_start).total_seconds(),
                ),
                "text": row["text"],
                "avg_logprob": None,
                "no_speech_prob": None,
                "speaker": None,
                "speaker_confidence": None,
                "segment_id": row["segment_id"],
                "window_id": row["window_id"],
                "state": "finalized" if final else "provisional",
            }
            for row in segments
        )
        compatibility_segments.sort(
            key=lambda segment: (
                float(segment.get("start", 0.0)),
                float(segment.get("end", segment.get("start", 0.0))),
            )
        )
        received_times = [datetime.fromisoformat(row["received_at"]) for row in chunks]
        transport = chunks[0]["transport"] or self.settings.source_transport
        event = {
            "schema_version": 1,
            "event_id": transcript_id,
            "event_type": "observation.audio.transcript",
            "session_id": session_id,
            "transcript_version": version,
            "source": {
                "device": self.settings.source_device,
                "phone_bridge": self.settings.phone_bridge,
                "transport": transport,
                "relay": "tailscale-direct" if transport == "s25-direct-ble-tailscale" else self.settings.relay,
                "uid_hash": chunks[0]["uid_hash"],
                "source_session_id": chunks[0]["source_session_id"],
            },
            "timestamps": {
                "observed_start_estimate": observed_start.isoformat(),
                "observed_end_estimate": observed_end.isoformat(),
                "first_received_at": min(received_times).isoformat(),
                "last_received_at": max(received_times).isoformat(),
                "transcribed_at": transcribed_at.isoformat(),
                "capture_time_quality": capture_quality,
                "timestamp_basis": timestamp_basis,
            },
            "evidence": {
                "canonical": "raw_audio_chunks",
                "audio_sha256": audio_sha256,
                "duration_seconds": duration,
                "sample_rate": int(chunks[0]["sample_rate"]),
                "chunk_ids": [row["chunk_id"] for row in chunks],
                "chunk_paths": [row["path"] for row in chunks],
                "chunk_capture": [self._chunk_capture_record(row) for row in chunks],
                "sequence": self._sequence_summary(chunks),
                "derived_from": [f"audio-chunk:{row['chunk_id']}" for row in chunks],
                "raw_retention": "indefinite-until-explicit-policy-change",
            },
            "transcript": {
                "text": text,
                "model": model,
                "language": "en" if text else None,
                "language_probability": None,
                "confidence": None,
                "avg_logprob": None,
                "no_speech_probability": None,
                "latency_seconds": sum(float(row["latency_seconds"] or 0.0) for row in windows),
                "segments": compatibility_segments,
                "diarization_status": "not_run",
                "raw_or_near_raw": True,
                "explicit_or_inferred": "explicit_observation",
                "transcript_is_machine_interpretation": True,
                "speaker": None,
                "speaker_confidence": None,
            },
            "reconciliation": {
                "strategy": "compose_committed_rolling_segments",
                "final": final,
                "rolling_window_ids": [row["window_id"] for row in windows],
                "rolling_segment_ids": [row["segment_id"] for row in segments],
                "base_transcript_id": base_transcript_id,
                "base_transcript_text": base_text or None,
                "base_transcript_model": base_model,
                "base_transcript_observed_start": (
                    base_observed_start.isoformat() if base_observed_start else None
                ),
                "base_transcript_observed_end": (
                    base_observed_end.isoformat() if base_observed_end else None
                ),
                "base_transcript_chunk_capture": base_chunk_capture,
                "base_transcript_segments": base_segments,
                "full_session_asr_rerun": False,
            },
            "promotion": {
                "wiki_status": "pending_daily_distillation",
                "derived_memories": [],
                "wiki_references": [],
            },
        }
        observed_date = self._daily_date(observed_start).isoformat()
        event_dir = self.root / "events" / observed_date
        event_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(event_dir, 0o700)
        event_path = event_dir / f"{transcript_id}.json"

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending_after_snapshot = pending_chunks
            if final:
                conn.executemany(
                    "UPDATE chunks SET status='processed' WHERE session_id=? AND chunk_id=?",
                    ((session_id, str(row["chunk_id"])) for row in chunks),
                )
                pending_after_snapshot = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE session_id=? AND status='pending'",
                        (session_id,),
                    ).fetchone()[0]
                )
            session_last_receive = float(
                conn.execute(
                    "SELECT last_receive_epoch FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            revived_after_idle_snapshot = (
                idle_cutoff_epoch is not None and session_last_receive > idle_cutoff_epoch
            )
            effective_final = (
                final and pending_after_snapshot == 0 and not revived_after_idle_snapshot
            )
            event["reconciliation"]["final"] = effective_final
            event["reconciliation"]["idle_cutoff_epoch"] = idle_cutoff_epoch
            event["reconciliation"]["revived_after_idle_snapshot"] = revived_after_idle_snapshot
            for segment in event["transcript"]["segments"]:
                if isinstance(segment, dict) and segment.get("window_id"):
                    segment["state"] = "finalized" if effective_final else "provisional"
            fd = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(event, indent=2, sort_keys=True) + "\n")
            conn.execute(
                """INSERT INTO transcripts
                   (transcript_id,session_id,version,observed_date,transcribed_at,model,language,
                    confidence,avg_logprob,no_speech_probability,latency_seconds,duration_seconds,
                    audio_sha256,text,event_path,event_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    transcript_id,
                    session_id,
                    version,
                    observed_date,
                    transcribed_at.isoformat(),
                    model,
                    "en" if text else None,
                    None,
                    None,
                    None,
                    event["transcript"]["latency_seconds"],
                    duration,
                    audio_sha256,
                    text,
                    str(event_path.relative_to(self.root)),
                    json.dumps(event, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT INTO transcripts_fts(transcript_id,session_id,text) VALUES (?,?,?)",
                (transcript_id, session_id, text),
            )
            conn.execute(
                "DELETE FROM search_documents WHERE session_id=? AND kind='canonical_transcript'",
                (session_id,),
            )
            conn.execute(
                """INSERT INTO search_documents(document_id,kind,session_id,text)
                   VALUES (?,'canonical_transcript',?,?)""",
                (transcript_id, session_id, text),
            )
            conn.executemany(
                """UPDATE rolling_segments SET reconciled_by=?,state=?
                   WHERE session_id=? AND segment_id=? AND state!='superseded'""",
                (
                    (
                        transcript_id,
                        "finalized" if effective_final else "provisional",
                        session_id,
                        str(row["segment_id"]),
                    )
                    for row in segments
                ),
            )
            conn.executemany(
                "DELETE FROM search_documents WHERE document_id=? AND kind='rolling_segment'",
                ((str(row["segment_id"]),) for row in segments),
            )
            if final:
                conn.execute(
                    "UPDATE sessions SET status=?,last_error=NULL WHERE session_id=?",
                    (
                        "open"
                        if pending_after_snapshot or revived_after_idle_snapshot
                        else "transcribed",
                        session_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET last_error=NULL WHERE session_id=?",
                    (session_id,),
                )
            conn.commit()
        self._audit(
            "rolling_session_reconciled",
            session_id=session_id,
            transcript_id=transcript_id,
            version=version,
            final=effective_final,
            window_count=len(windows),
            segment_count=len(segments),
            full_session_asr_rerun=False,
        )
        return event

    def _rolling_window_inflight(self, session_id: str) -> bool:
        with self._connect() as conn:
            return bool(
                conn.execute(
                    """SELECT 1 FROM rolling_windows
                       WHERE session_id=? AND status='processing' AND retired=0 LIMIT 1""",
                    (session_id,),
                ).fetchone()
            )

    def finalize_incremental_session(
        self,
        session_id: str,
        asr: ASRBackend,
        gate: SpeechGate,
        *,
        idle_cutoff_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        with self._transcript_write_lock(session_id):
            if idle_cutoff_epoch is not None:
                with self._connect() as conn:
                    session = conn.execute(
                        "SELECT status,last_receive_epoch FROM sessions WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                if (
                    session is None
                    or session["status"] != "open"
                    or float(session["last_receive_epoch"]) > idle_cutoff_epoch
                ):
                    return None
            while self._process_incremental_unlocked(session_id, asr, gate, force=True) is not None:
                pass
            if self._rolling_window_inflight(session_id):
                raise RuntimeError("rolling transcription is already in progress")
            return self._reconcile_incremental_session_unlocked(
                session_id,
                final=True,
                idle_cutoff_epoch=idle_cutoff_epoch,
            )

    def transcribe_session(
        self,
        session_id: str,
        asr: ASRBackend,
        *,
        force: bool = False,
        idle_cutoff_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        with self._transcript_write_lock(session_id):
            return self._transcribe_session_unlocked(
                session_id,
                asr,
                force=force,
                idle_cutoff_epoch=idle_cutoff_epoch,
            )

    def _transcribe_session_unlocked(
        self,
        session_id: str,
        asr: ASRBackend,
        *,
        force: bool = False,
        idle_cutoff_epoch: float | None = None,
    ) -> dict[str, Any] | None:
        if idle_cutoff_epoch is not None:
            with self._connect() as conn:
                session = conn.execute(
                    "SELECT status,last_receive_epoch FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
            if (
                session is None
                or session["status"] not in {"open", "error"}
                or float(session["last_receive_epoch"]) > idle_cutoff_epoch
            ):
                return None
        chunks = self._session_chunks(session_id)
        if not chunks:
            raise KeyError(session_id)
        snapshot_chunk_ids = [str(row["chunk_id"]) for row in chunks]
        snapshot_chunk_id_set = set(snapshot_chunk_ids)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT event_json FROM transcripts WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            pending_chunks = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE session_id=? AND status='pending'",
                    (session_id,),
                ).fetchone()[0]
            )
            snapshot_rolling_segment_ids = [
                str(row["segment_id"])
                for row in conn.execute(
                    "SELECT segment_id FROM rolling_segments WHERE session_id=? AND state!='superseded'",
                    (session_id,),
                )
            ]
            snapshot_rolling_window_ids = [
                str(row["window_id"])
                for row in conn.execute(
                    "SELECT window_id,chunk_ids_json FROM rolling_windows WHERE session_id=?",
                    (session_id,),
                )
                if set(json.loads(row["chunk_ids_json"])).issubset(snapshot_chunk_id_set)
            ]
        if existing and not force and pending_chunks == 0:
            existing_event = json.loads(existing["event_json"])
            reconciliation = existing_event.get("reconciliation", {})
            if (
                reconciliation.get("strategy") == "full_session_asr"
                and reconciliation.get("final") is True
            ):
                return existing_event

        wav_path: Path | None = None
        try:
            wav_path, audio_sha256, duration = self._assemble_temp_wav(session_id, chunks)
            result = asr.transcribe(wav_path)
        except Exception as exc:
            bounded_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            with self._connect() as conn:
                failures = int(
                    conn.execute(
                        "SELECT transcription_failures FROM sessions WHERE session_id=?",
                        (session_id,),
                    ).fetchone()[0]
                ) + 1
                base_delay = max(1.0, self.settings.rolling_recovery_seconds)
                retry_delay = min(3600.0, base_delay * (2 ** min(failures - 1, 10)))
                next_attempt = time.time() + retry_delay
                conn.execute(
                    """UPDATE sessions
                       SET status='error',last_error=?,transcription_failures=?,
                           next_transcription_epoch=?
                       WHERE session_id=?""",
                    (bounded_error, failures, next_attempt, session_id),
                )
            self._audit(
                "transcription_failed",
                session_id=session_id,
                error_type=type(exc).__name__,
                failures=failures,
                retry_delay_seconds=retry_delay,
            )
            raise
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink()
                except OSError:
                    pass

        with self._connect() as conn:
            version = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM transcripts WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
        received_times = [datetime.fromisoformat(row["received_at"]) for row in chunks]
        first_received = min(received_times)
        last_received = max(received_times)
        has_device_capture_time = all(row["capture_started_at"] and row["capture_ended_at"] for row in chunks)
        if has_device_capture_time:
            capture_starts = [datetime.fromisoformat(row["capture_started_at"]) for row in chunks]
            capture_ends = [datetime.fromisoformat(row["capture_ended_at"]) for row in chunks]
            observed_start = min(capture_starts)
            observed_end = max(capture_ends)
            capture_time_quality = "phone-derived-capture-time"
            timestamp_basis = "s25_pcm_sample_clock"
        else:
            observed_end = last_received
            observed_start = last_received - timedelta(seconds=duration)
            capture_time_quality = "relay-arrival-estimate"
            timestamp_basis = "last_relay_receive_time_minus_total_pcm_duration"
        sequence_numbers = [int(row["sequence_number"]) for row in chunks if row["sequence_number"] is not None]
        sequence_summary: dict[str, Any] | None = None
        if sequence_numbers:
            ordered_sequences = sorted(set(sequence_numbers))
            first_sequence = ordered_sequences[0]
            last_sequence = ordered_sequences[-1]
            missing_count = (last_sequence - first_sequence + 1) - len(ordered_sequences)
            missing: list[int] = []
            for left, right in zip(ordered_sequences, ordered_sequences[1:]):
                if len(missing) >= 1000:
                    break
                take = min(right - left - 1, 1000 - len(missing))
                if take > 0:
                    missing.extend(range(left + 1, left + 1 + take))
            sequence_summary = {
                "first": first_sequence,
                "last": last_sequence,
                "missing": missing,
                "missing_count": missing_count,
                "missing_truncated": missing_count > len(missing),
            }
        transport = chunks[0]["transport"] or self.settings.source_transport
        source_session_id = chunks[0]["source_session_id"]
        transcript_id = f"transcript-{session_id}-v{version}"
        transcribed_at = datetime.now(timezone.utc)
        event = {
            "schema_version": 1,
            "event_id": transcript_id,
            "event_type": "observation.audio.transcript",
            "session_id": session_id,
            "transcript_version": version,
            "source": {
                "device": self.settings.source_device,
                "phone_bridge": self.settings.phone_bridge,
                "transport": transport,
                "relay": "tailscale-direct" if transport == "s25-direct-ble-tailscale" else self.settings.relay,
                "uid_hash": chunks[0]["uid_hash"],
                "source_session_id": source_session_id,
            },
            "timestamps": {
                "observed_start_estimate": observed_start.isoformat(),
                "observed_end_estimate": observed_end.isoformat(),
                "first_received_at": first_received.isoformat(),
                "last_received_at": last_received.isoformat(),
                "transcribed_at": transcribed_at.isoformat(),
                "capture_time_quality": capture_time_quality,
                "timestamp_basis": timestamp_basis,
            },
            "evidence": {
                "canonical": "raw_audio_chunks",
                "audio_sha256": audio_sha256,
                "duration_seconds": duration,
                "sample_rate": int(chunks[0]["sample_rate"]),
                "chunk_ids": [row["chunk_id"] for row in chunks],
                "chunk_paths": [row["path"] for row in chunks],
                "chunk_capture": [self._chunk_capture_record(row) for row in chunks],
                "sequence": sequence_summary,
                "derived_from": [f"audio-chunk:{row['chunk_id']}" for row in chunks],
                "raw_retention": "indefinite-until-explicit-policy-change",
            },
            "transcript": {
                **result.to_dict(),
                "raw_or_near_raw": True,
                "explicit_or_inferred": "explicit_observation",
                "transcript_is_machine_interpretation": True,
                "speaker": None,
                "speaker_confidence": None,
            },
            "reconciliation": {
                "strategy": "full_session_asr",
                "final": False,
                "idle_cutoff_epoch": idle_cutoff_epoch,
                "revived_after_idle_snapshot": False,
                "rolling_window_ids": snapshot_rolling_window_ids,
                "rolling_segment_ids": snapshot_rolling_segment_ids,
                "full_session_asr_rerun": force,
            },
            "promotion": {
                "wiki_status": "pending_daily_distillation",
                "derived_memories": [],
                "wiki_references": [],
            },
        }
        observed_date = self._daily_date(observed_start).isoformat()
        event_dir = self.root / "events" / observed_date
        event_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(event_dir, 0o700)
        event_path = event_dir / f"{transcript_id}.json"

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO transcripts
                   (transcript_id,session_id,version,observed_date,transcribed_at,model,language,
                    confidence,avg_logprob,no_speech_probability,latency_seconds,duration_seconds,
                    audio_sha256,text,event_path,event_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    transcript_id,
                    session_id,
                    version,
                    observed_date,
                    transcribed_at.isoformat(),
                    result.model,
                    result.language,
                    result.confidence,
                    result.avg_logprob,
                    result.no_speech_probability,
                    result.latency_seconds,
                    duration,
                    audio_sha256,
                    result.text,
                    str(event_path.relative_to(self.root)),
                    json.dumps(event, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT INTO transcripts_fts(transcript_id,session_id,text) VALUES (?,?,?)",
                (transcript_id, session_id, result.text),
            )
            conn.execute(
                "DELETE FROM search_documents WHERE session_id=? AND kind='canonical_transcript'",
                (session_id,),
            )
            conn.execute(
                """INSERT INTO search_documents(document_id,kind,session_id,text)
                   VALUES (?,'canonical_transcript',?,?)""",
                (transcript_id, session_id, result.text),
            )
            conn.executemany(
                "UPDATE chunks SET status='processed' WHERE session_id=? AND chunk_id=?",
                ((session_id, chunk_id) for chunk_id in snapshot_chunk_ids),
            )
            conn.executemany(
                "UPDATE rolling_chunk_state SET finalized=1 WHERE session_id=? AND chunk_id=?",
                ((session_id, chunk_id) for chunk_id in snapshot_chunk_ids),
            )
            conn.executemany(
                "UPDATE rolling_windows SET retired=1 WHERE session_id=? AND window_id=?",
                ((session_id, window_id) for window_id in snapshot_rolling_window_ids),
            )
            conn.executemany(
                """UPDATE rolling_segments SET state='superseded',reconciled_by=?
                   WHERE session_id=? AND segment_id=? AND state!='superseded'""",
                (
                    (transcript_id, session_id, segment_id)
                    for segment_id in snapshot_rolling_segment_ids
                ),
            )
            conn.executemany(
                "DELETE FROM search_documents WHERE document_id=? AND kind='rolling_segment'",
                ((segment_id,) for segment_id in snapshot_rolling_segment_ids),
            )
            pending_after_snapshot = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE session_id=? AND status='pending'",
                    (session_id,),
                ).fetchone()[0]
            )
            session_last_receive = float(
                conn.execute(
                    "SELECT last_receive_epoch FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            revived_after_idle_snapshot = (
                idle_cutoff_epoch is not None and session_last_receive > idle_cutoff_epoch
            )
            effective_final = pending_after_snapshot == 0 and not revived_after_idle_snapshot
            event["reconciliation"]["final"] = effective_final
            event["reconciliation"]["revived_after_idle_snapshot"] = revived_after_idle_snapshot
            payload = json.dumps(event, indent=2, sort_keys=True) + "\n"
            fd = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            conn.execute(
                "UPDATE transcripts SET event_json=? WHERE transcript_id=?",
                (json.dumps(event, sort_keys=True), transcript_id),
            )
            session_status = (
                "open" if pending_after_snapshot or revived_after_idle_snapshot else "transcribed"
            )
            conn.execute(
                """UPDATE sessions
                   SET status=?,last_error=NULL,transcription_failures=0,next_transcription_epoch=0
                   WHERE session_id=?""",
                (session_status, session_id),
            )
            conn.commit()
        self._audit(
            "session_transcribed",
            session_id=session_id,
            transcript_id=transcript_id,
            version=version,
            model=result.model,
            duration_seconds=duration,
            latency_seconds=result.latency_seconds,
            final=effective_final,
        )
        return event

    def idle_open_sessions(self, now_epoch: float | None = None) -> list[str]:
        now_value = now_epoch if now_epoch is not None else time.time()
        threshold = now_value - self.settings.idle_flush_seconds
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT session_id FROM sessions
                   WHERE status IN ('open','error')
                     AND last_receive_epoch<=?
                     AND next_transcription_epoch<=?
                   ORDER BY last_receive_epoch""",
                (threshold, now_value),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = normalize_words(query)
        if not terms:
            return []
        bounded_limit = max(1, min(limit, 100))
        fts_query = " AND ".join(f'"{term}"' for term in terms)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT d.document_id,d.kind,d.session_id,
                          snippet(search_documents_fts,3,'[',']',' … ',24) AS snippet,
                          bm25(search_documents_fts) AS score,
                          r.state,r.observed_date AS rolling_observed_date,
                          r.observed_start,r.observed_end,r.model AS rolling_model,
                          r.event_json AS rolling_event_json,
                          t.version,t.observed_date AS transcript_observed_date,
                          t.transcribed_at,t.model AS transcript_model,t.duration_seconds,
                          t.event_json AS transcript_event_json
                   FROM search_documents_fts
                   JOIN search_documents d USING(document_id)
                   LEFT JOIN rolling_segments r
                     ON d.kind='rolling_segment' AND r.segment_id=d.document_id
                   LEFT JOIN transcripts t
                     ON d.kind='canonical_transcript' AND t.transcript_id=d.document_id
                   LEFT JOIN (
                       SELECT session_id,MAX(version) AS version
                       FROM transcripts GROUP BY session_id
                   ) latest ON latest.session_id=t.session_id
                   WHERE search_documents_fts MATCH ? AND (
                       (d.kind='rolling_segment' AND r.state='provisional' AND r.reconciled_by IS NULL)
                       OR (d.kind='canonical_transcript' AND t.version=latest.version)
                   )
                   ORDER BY score,COALESCE(r.observed_start,t.transcribed_at) DESC
                   LIMIT ?""",
                (fts_query, bounded_limit),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            score = float(row["score"])
            if row["kind"] == "rolling_segment":
                results.append(
                    {
                        "kind": "rolling_segment",
                        "state": row["state"],
                        "relevance_score": score,
                        "source_bm25": score,
                        "common_bm25_score": -score,
                        "segment_id": row["document_id"],
                        "session_id": row["session_id"],
                        "snippet": row["snippet"],
                        "observed_date": row["rolling_observed_date"],
                        "observed_start": row["observed_start"],
                        "observed_end": row["observed_end"],
                        "model": row["rolling_model"],
                        "event": json.loads(row["rolling_event_json"]),
                    }
                )
                continue
            event = json.loads(row["transcript_event_json"])
            results.append(
                {
                    "kind": "canonical_transcript",
                    "state": (
                        "provisional"
                        if event.get("reconciliation", {}).get("final") is False
                        else "finalized"
                    ),
                    "relevance_score": score,
                    "source_bm25": score,
                    "common_bm25_score": -score,
                    "transcript_id": row["document_id"],
                    "session_id": row["session_id"],
                    "snippet": row["snippet"],
                    "version": row["version"],
                    "observed_date": row["transcript_observed_date"],
                    "transcribed_at": row["transcribed_at"],
                    "model": row["transcript_model"],
                    "duration_seconds": row["duration_seconds"],
                    "event": event,
                }
            )
        return results

    def export_day(self, target: date) -> Path:
        with self._day_export_lock(target):
            return self._export_day_unlocked(target)

    def _export_day_unlocked(self, target: date) -> Path:
        day = target.isoformat()
        with self._connect() as conn:
            candidate_rows = conn.execute(
                """SELECT t.* FROM transcripts t
                   JOIN (SELECT session_id,MAX(version) AS version FROM transcripts GROUP BY session_id) latest
                     ON latest.session_id=t.session_id AND latest.version=t.version
                   ORDER BY t.transcribed_at"""
            ).fetchall()
            rows = [
                row
                for row in candidate_rows
                if self._daily_date(datetime.fromisoformat(
                    json.loads(row["event_json"])["timestamps"]["observed_start_estimate"]
                ))
                <= target
                <= self._daily_date(datetime.fromisoformat(
                    json.loads(row["event_json"])["timestamps"]["observed_end_estimate"]
                ))
            ]
            rolling_candidates = conn.execute(
                """SELECT r.*,w.event_path,w.audio_sha256,w.chunk_ids_json,
                          reconciled.event_json AS reconciled_event_json
                   FROM rolling_segments r JOIN rolling_windows w USING(window_id)
                   LEFT JOIN transcripts reconciled ON reconciled.transcript_id=r.reconciled_by
                   WHERE r.state!='superseded'
                   ORDER BY r.observed_start,r.window_id,r.segment_index""",
            ).fetchall()
            rolling_rows = []
            for row in rolling_candidates:
                rolling_start = self._daily_date(datetime.fromisoformat(str(row["observed_start"])))
                rolling_end = self._daily_date(datetime.fromisoformat(str(row["observed_end"])))
                if not rolling_start <= target <= rolling_end:
                    continue
                if row["reconciled_event_json"]:
                    reconciled_event = json.loads(row["reconciled_event_json"])
                    reconciled_start = self._daily_date(datetime.fromisoformat(
                        reconciled_event["timestamps"]["observed_start_estimate"]
                    ))
                    reconciled_end = self._daily_date(datetime.fromisoformat(
                        reconciled_event["timestamps"]["observed_end_estimate"]
                    ))
                    if reconciled_start <= target <= reconciled_end:
                        continue
                rolling_rows.append(row)
        lines = [
            f"# Wearable transcript context — {day}",
            "",
            "Local scratch input for daily LLM Wiki distillation. The searchable transcript archive and raw audio remain the evidence source; do not commit this scratch file as raw Wiki content.",
            "",
            f"Sessions: {len(rows)}",
            f"Rolling segments without same-day canonical coverage: {len(rolling_rows)}",
            "",
        ]
        for row in rows:
            event = json.loads(row["event_json"])
            timestamps = event["timestamps"]
            transcript = event["transcript"]
            lines.extend(
                [
                    f"## {row['session_id']} — transcript v{row['version']}",
                    f"- Estimated observed: {timestamps['observed_start_estimate']} to {timestamps['observed_end_estimate']}",
                    f"- Source: {event['source']['device']} via {event['source']['transport']} / {event['source']['relay']}",
                    f"- Evidence: {len(event['evidence']['chunk_ids'])} chunks; audio SHA-256 `{event['evidence']['audio_sha256']}`",
                    f"- ASR: {row['model']}; confidence: {transcript.get('confidence')}; avg_logprob: {transcript.get('avg_logprob')}",
                    f"- Event: `{row['event_path']}`",
                    "",
                    str(row["text"]),
                    "",
                ]
            )
        if rolling_rows:
            lines.extend(
                [
                    "## Rolling transcript segment evidence without same-day canonical coverage",
                    "",
                    "These committed ASR segments are durable derived evidence from bounded windows. Their state and reconciliation link are explicit; they must not be promoted as unquestioned memory.",
                    "",
                ]
            )
            for row in rolling_rows:
                lines.extend(
                    [
                        f"### {row['session_id']} — {row['segment_id']}",
                        f"- State: {row['state']}; reconciled by: {row['reconciled_by'] or 'not yet'}; observed: {row['observed_start']} to {row['observed_end']}",
                        f"- Evidence: {len(json.loads(row['chunk_ids_json']))} chunks; window audio SHA-256 `{row['audio_sha256']}`",
                        f"- ASR: {row['model']}; revision: {row['asr_revision']}",
                        f"- Window event: `{row['event_path']}`",
                        "",
                        str(row["text"]),
                        "",
                    ]
                )
        path = self.root / "wiki-inbox" / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        unbounded_payload = "\n".join(lines).rstrip() + "\n"
        payload, truncated = _bounded_wiki_export(unbounded_payload)
        temporary_dir = self.root / "tmp" / "wiki-exports"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(temporary_dir, 0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{day}.", suffix=".tmp", dir=temporary_dir
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        self._audit(
            "day_exported",
            observed_date=day,
            sessions=len(rows),
            rolling_segments_without_same_day_canonical=len(rolling_rows),
            export_bytes=len(payload.encode("utf-8")),
            export_truncated=truncated,
            path=str(path.relative_to(self.root)),
        )
        return path

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            session_counts = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) n FROM sessions GROUP BY status")}
            chunks = conn.execute("SELECT COUNT(*) n,COALESCE(SUM(num_bytes),0) b FROM chunks").fetchone()
            transcripts = conn.execute("SELECT COUNT(*) n FROM transcripts").fetchone()[0]
            rolling_windows = {
                row["status"]: row["n"]
                for row in conn.execute("SELECT status,COUNT(*) n FROM rolling_windows GROUP BY status")
            }
            rolling_segments = {
                row["state"]: row["n"]
                for row in conn.execute("SELECT state,COUNT(*) n FROM rolling_segments GROUP BY state")
            }
        return {
            "state_dir": str(self.root),
            "sessions": session_counts,
            "chunks": int(chunks["n"]),
            "raw_bytes": int(chunks["b"]),
            "transcripts": int(transcripts),
            "rolling_windows": rolling_windows,
            "rolling_segments": rolling_segments,
            "raw_retention": "indefinite-until-explicit-policy-change",
        }

    def _deletion_tombstone_path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        return self.root / "deletions" / f"session-{digest}.json"

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_existing_directory(self, path: Path) -> None:
        if path.is_dir():
            self._fsync_directory(path)

    def _read_deletion_tombstone(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or path.parent != self.root / "deletions":
            raise ValueError("session deletion tombstone path is unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
        session_id = payload.get("session_id")
        affected_days = payload.get("affected_days")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session deletion tombstone has no session id")
        if path != self._deletion_tombstone_path(session_id):
            raise ValueError("session deletion tombstone identity is invalid")
        if not isinstance(affected_days, list) or any(
            not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            for day in affected_days
        ):
            raise ValueError("session deletion tombstone dates are invalid")
        return {"session_id": session_id, "affected_days": sorted(set(affected_days))}

    def _write_deletion_tombstone(self, session_id: str, affected_days: set[str]) -> Path:
        path = self._deletion_tombstone_path(session_id)
        payload = {
            "schema": 1,
            "session_id": session_id,
            "affected_days": sorted(affected_days),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        if path.exists():
            existing = self._read_deletion_tombstone(path)
            if existing["session_id"] != session_id or existing["affected_days"] != payload["affected_days"]:
                raise ValueError("session deletion tombstone conflicts with archive state")
            return path
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _finish_deletion_tombstone(self, path: Path) -> None:
        payload = self._read_deletion_tombstone(path)
        failures: list[tuple[str, Exception]] = []
        for day in payload["affected_days"]:
            try:
                self.export_day(date.fromisoformat(day))
            except Exception as exc:
                exc.add_note(f"Post-deletion Wiki export failed for observed date {day}")
                failures.append((day, exc))
        if failures:
            primary_day, primary_error = failures[0]
            for day, error in failures[1:]:
                primary_error.add_note(f"Additional post-deletion Wiki export failure for {day}: {error}")
            primary_error.add_note(
                f"Attempted all {len(payload['affected_days'])} affected dates before re-raising; "
                f"first failure was {primary_day}"
            )
            raise primary_error
        self._audit(
            "session_deleted",
            session_id=payload["session_id"],
            explicit=True,
            wiki_export_failure_days=[],
        )
        path.unlink(missing_ok=True)
        self._fsync_directory(path.parent)

    def _reconcile_session_deletions(self) -> None:
        for path in sorted((self.root / "deletions").glob("session-*.json")):
            payload = self._read_deletion_tombstone(path)
            self.delete_session(str(payload["session_id"]))

    def delete_session(self, session_id: str) -> bool:
        with self._archive_mutation_lock():
            return self._delete_session_unlocked(session_id)

    def _delete_session_unlocked(self, session_id: str) -> bool:
        tombstone_path = self._deletion_tombstone_path(session_id)
        with self._connect() as conn:
            chunk_rows = conn.execute("SELECT path FROM chunks WHERE session_id=?", (session_id,)).fetchall()
            transcript_rows = conn.execute(
                """SELECT transcript_id,event_path,observed_date,event_json
                   FROM transcripts WHERE session_id=?""",
                (session_id,),
            ).fetchall()
            rolling_window_rows = conn.execute(
                "SELECT event_path,observed_start FROM rolling_windows WHERE session_id=?",
                (session_id,),
            ).fetchall()
            rolling_segment_rows = conn.execute(
                """SELECT segment_id,observed_date,observed_start,observed_end
                   FROM rolling_segments WHERE session_id=?""",
                (session_id,),
            ).fetchall()
        if not chunk_rows and not transcript_rows and not rolling_window_rows:
            if tombstone_path.exists():
                self._finish_deletion_tombstone(tombstone_path)
                return True
            return False

        affected_days: set[str] = set()
        for row in transcript_rows:
            event = json.loads(row["event_json"])
            current = self._daily_date(
                datetime.fromisoformat(event["timestamps"]["observed_start_estimate"])
            )
            end_day = self._daily_date(
                datetime.fromisoformat(event["timestamps"]["observed_end_estimate"])
            )
            while current <= end_day:
                affected_days.add(current.isoformat())
                current += timedelta(days=1)
        for row in rolling_segment_rows:
            current = self._daily_date(datetime.fromisoformat(str(row["observed_start"])))
            end_day = self._daily_date(datetime.fromisoformat(str(row["observed_end"])))
            while current <= end_day:
                affected_days.add(current.isoformat())
                current += timedelta(days=1)
        for row in rolling_window_rows:
            if row["observed_start"]:
                affected_days.add(
                    self._daily_date(datetime.fromisoformat(row["observed_start"])).isoformat()
                )

        tombstone_path = self._write_deletion_tombstone(session_id, affected_days)

        raw_dirs: set[Path] = set()
        for row in chunk_rows:
            evidence_path = self.root / row["path"]
            raw_dirs.add(evidence_path.parent)
            evidence_path.unlink(missing_ok=True)
            self._fsync_existing_directory(evidence_path.parent)
        for row in transcript_rows:
            event_path = self.root / row["event_path"]
            event_path.unlink(missing_ok=True)
            self._fsync_existing_directory(event_path.parent)
        for row in rolling_window_rows:
            if row["event_path"]:
                event_path = self.root / row["event_path"]
                event_path.unlink(missing_ok=True)
                self._fsync_existing_directory(event_path.parent)
        for directory in sorted(raw_dirs, reverse=True):
            try:
                directory.rmdir()
                self._fsync_directory(directory.parent)
            except OSError:
                pass

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in transcript_rows:
                conn.execute("DELETE FROM transcripts_fts WHERE transcript_id=?", (row["transcript_id"],))
            for row in rolling_segment_rows:
                conn.execute("DELETE FROM rolling_segments_fts WHERE segment_id=?", (row["segment_id"],))
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            conn.commit()

        self._finish_deletion_tombstone(tombstone_path)
        return True
