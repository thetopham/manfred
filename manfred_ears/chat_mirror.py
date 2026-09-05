from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings


_EVENT_ID_RE = re.compile(r"chat-[0-9a-f]{32}")
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ALLOWED_FIELDS = {
    "schema_version",
    "mirror_session_id",
    "sequence_number",
    "observed_at",
    "clock_basis",
    "source_package",
    "event_kind",
    "capture_method",
    "finality",
    "completeness",
    "observed_text",
    "observable_conversation_id",
    "interruptions",
    "image_refs",
    "audio_refs",
    "automation_events",
    "gaps",
    "metadata",
}
_REQUIRED_FIELDS = _ALLOWED_FIELDS - {"observable_conversation_id"}
_METADATA_INTEGRITY_FIELDS = (
    "event_id",
    "idempotency_key",
    "received_at",
    "received_epoch",
    "payload_sha256",
    "num_bytes",
    "mirror_session_id",
    "sequence_number",
    "observed_at",
    "source_package",
    "event_kind",
    "capture_method",
    "finality",
    "completeness",
    "clock_basis",
    "observed_epoch",
    "observed_text",
    "observable_conversation_id",
    "path",
    "metadata_path",
)


@dataclass(frozen=True)
class ChatMirrorIngestResult:
    event_id: str
    mirror_session_id: str
    sequence_number: int
    duplicate: bool
    sha256: str
    received_at: str


class ChatMirrorArchive:
    """Canonical archive for bounded ChatGPT Accessibility observations.

    The exact submitted JSON bytes are retained before the SQLite index is
    mutated. Accessibility output is an observed UI snapshot, never promoted to
    an official ChatGPT transcript without an explicit stronger provenance.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.state_dir
        self.db_path = self.root / "archive.sqlite3"
        self.chat_root = self.root / "chat-mirror" / "raw"
        self.deletion_root = self.root / "chat-mirror" / "deletions"
        self.audit_root = self.root / "audit"
        self.lock_path = self.root / "chat-mirror" / ".archive.lock"
        self._ingest_lock = threading.Lock()
        for path in (
            self.root,
            self.lock_path.parent,
            self.chat_root,
            self.deletion_root,
            self.audit_root,
        ):
            self._ensure_directory_durable(path)
        self._init_db()
        with self._process_archive_lock():
            self._reconcile_deletions()
            self._reconcile_interrupted_commits()
            self._reconcile_sidecars()

    def _ensure_directory_durable(self, path: Path) -> None:
        missing: list[Path] = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            os.chmod(directory, 0o700)
            self._fsync_directory(directory.parent)
        os.chmod(path, 0o700)

    @contextmanager
    def _process_archive_lock(self):  # type: ignore[no-untyped-def]
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
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

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_mirror_events (
                    event_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    mirror_session_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    observed_epoch REAL NOT NULL,
                    received_at TEXT NOT NULL,
                    received_epoch REAL NOT NULL,
                    clock_basis TEXT NOT NULL,
                    source_package TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    capture_method TEXT NOT NULL,
                    finality TEXT NOT NULL,
                    completeness TEXT NOT NULL,
                    observed_text TEXT NOT NULL,
                    observable_conversation_id TEXT,
                    payload_sha256 TEXT NOT NULL,
                    num_bytes INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    metadata_path TEXT NOT NULL,
                    UNIQUE(mirror_session_id, sequence_number)
                );
                CREATE INDEX IF NOT EXISTS chat_mirror_session_order
                    ON chat_mirror_events(mirror_session_id,sequence_number,event_id);
                CREATE INDEX IF NOT EXISTS chat_mirror_observed_time
                    ON chat_mirror_events(observed_epoch,event_id);
                CREATE TABLE IF NOT EXISTS chat_mirror_sequence_reservations (
                    mirror_session_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(mirror_session_id, sequence_number)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chat_mirror_fts USING fts5(
                    event_id UNINDEXED,
                    observed_text
                );
                """
            )
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def _digest(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    @classmethod
    def _metadata_integrity_digest(cls, record: Any) -> str:
        trusted = {field: record[field] for field in _METADATA_INTEGRITY_FIELDS}
        return cls._digest(
            json.dumps(trusted, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    @staticmethod
    def _bounded_identifier(value: Any, label: str, *, maximum: int = 128) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        if not 1 <= len(value) <= maximum or any(ord(character) < 32 for character in value):
            raise ValueError(f"{label} length or content is invalid")
        return value

    @staticmethod
    def _timestamp(value: Any, label: str) -> datetime:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError(f"{label} must be an ISO8601 string")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be ISO8601") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{label} must include a timezone")
        return parsed.astimezone(timezone.utc)

    def _validate_payload(self, body: bytes) -> tuple[dict[str, Any], datetime]:
        if not body or len(body) > self.settings.max_chat_mirror_body_bytes:
            raise ValueError("chat mirror payload size is invalid")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("chat mirror payload must be UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("chat mirror payload must be a JSON object")
        unexpected = set(decoded) - _ALLOWED_FIELDS
        if unexpected:
            raise ValueError(f"chat mirror payload has unexpected field: {sorted(unexpected)[0]}")
        missing = _REQUIRED_FIELDS - set(decoded)
        if missing:
            raise ValueError(f"chat mirror payload is missing field: {sorted(missing)[0]}")
        if decoded["schema_version"] != 1:
            raise ValueError("chat mirror schema version is unsupported")
        session_id = self._bounded_identifier(decoded["mirror_session_id"], "mirror session id")
        if _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ValueError("mirror session id format is invalid")
        sequence = decoded["sequence_number"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 0 <= sequence < 2**63:
            raise ValueError("chat mirror sequence number is invalid")
        observed = self._timestamp(decoded["observed_at"], "observed_at")
        if decoded["clock_basis"] != "android-system-clock":
            raise ValueError("chat mirror clock basis is unsupported")
        if decoded["source_package"] != "com.openai.chatgpt":
            raise ValueError("chat mirror source package is unsupported")
        if not isinstance(decoded["event_kind"], str) or decoded["event_kind"] not in {
            "session_started",
            "ui_snapshot",
            "session_ended",
            "gap",
            "automation",
        }:
            raise ValueError("chat mirror event kind is unsupported")
        if decoded["capture_method"] != "android-accessibility":
            raise ValueError("chat mirror capture method is unsupported")
        if not isinstance(decoded["finality"], str) or decoded["finality"] not in {
            "provisional",
            "final",
        }:
            raise ValueError("chat mirror finality is invalid")
        if not isinstance(decoded["completeness"], str) or decoded["completeness"] not in {
            "partial",
            "complete",
            "gap",
        }:
            raise ValueError("chat mirror completeness is invalid")
        text = decoded["observed_text"]
        if not isinstance(text, str) or len(text.encode("utf-8")) > 100_000:
            raise ValueError("chat mirror observed text is invalid")
        conversation_id = decoded.get("observable_conversation_id")
        if conversation_id is not None:
            self._bounded_identifier(conversation_id, "observable conversation id")
        for name in (
            "interruptions",
            "image_refs",
            "audio_refs",
            "automation_events",
            "gaps",
        ):
            value = decoded[name]
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError(f"chat mirror {name} must be a bounded list")
        metadata = decoded["metadata"]
        if not isinstance(metadata, dict) or len(metadata) > 64:
            raise ValueError("chat mirror metadata must be a bounded object")
        if len(json.dumps(metadata, separators=(",", ":")).encode("utf-8")) > 16_384:
            raise ValueError("chat mirror metadata is too large")
        return decoded, observed

    def _resolve_evidence_path(self, value: str) -> Path:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("chat mirror evidence path escapes the state root")
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError("chat mirror evidence path escapes the state root")
        return resolved

    def _validate_metadata_paths(self, metadata: dict[str, Any], metadata_path: Path) -> Path:
        event_id = metadata.get("event_id")
        if not isinstance(event_id, str) or _EVENT_ID_RE.fullmatch(event_id) is None:
            raise ValueError("chat mirror event id is invalid")
        digest = metadata.get("payload_sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("chat mirror SHA-256 is invalid")
        num_bytes = metadata.get("num_bytes")
        if (
            not isinstance(num_bytes, int)
            or not 1 <= num_bytes <= self.settings.max_chat_mirror_body_bytes
        ):
            raise ValueError("chat mirror byte count is invalid")
        declared_metadata = self._resolve_evidence_path(str(metadata["metadata_path"]))
        payload_path = self._resolve_evidence_path(str(metadata["path"]))
        if declared_metadata != metadata_path.resolve():
            raise ValueError("chat mirror metadata path does not match its sidecar")
        if (
            payload_path.parent != declared_metadata.parent
            or payload_path.name != f"{event_id}.json"
            or declared_metadata.name != f"{event_id}.meta.json"
            or not payload_path.is_relative_to(self.chat_root.resolve())
        ):
            raise ValueError("chat mirror evidence paths are invalid")
        return payload_path

    @staticmethod
    def _request_identity(record: Any) -> tuple[Any, ...]:
        return (
            record["payload_sha256"],
            int(record["num_bytes"]),
            record["mirror_session_id"],
            int(record["sequence_number"]),
            record["observed_at"],
            record["source_package"],
            record["event_kind"],
            record["capture_method"],
            record["finality"],
            record["completeness"],
        )

    def ingest(
        self,
        *,
        body: bytes,
        idempotency_key: str | None,
        claimed_sha256: str | None = None,
        received_at: datetime | None = None,
    ) -> ChatMirrorIngestResult:
        with self._ingest_lock:
            with self._process_archive_lock():
                return self._ingest_locked(
                    body=body,
                    idempotency_key=idempotency_key,
                    claimed_sha256=claimed_sha256,
                    received_at=received_at,
                )

    def _ingest_locked(
        self,
        *,
        body: bytes,
        idempotency_key: str | None,
        claimed_sha256: str | None,
        received_at: datetime | None,
    ) -> ChatMirrorIngestResult:
        payload, observed = self._validate_payload(body)
        if idempotency_key is None:
            raise ValueError("Idempotency-Key is required")
        idempotency_key = self._bounded_identifier(
            idempotency_key,
            "Idempotency-Key",
            maximum=240,
        )
        payload_sha256 = self._digest(body)
        if claimed_sha256 is not None:
            if _SHA256_RE.fullmatch(claimed_sha256) is None or claimed_sha256 != payload_sha256:
                raise ValueError("declared chat mirror SHA-256 does not match the payload")
        received = received_at or datetime.now(timezone.utc)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        received = received.astimezone(timezone.utc)
        event_id = f"chat-{self._digest(idempotency_key.encode('utf-8'))[:32]}"
        request_identity = {
            "payload_sha256": payload_sha256,
            "num_bytes": len(body),
            "mirror_session_id": payload["mirror_session_id"],
            "sequence_number": payload["sequence_number"],
            "observed_at": observed.isoformat(),
            "source_package": payload["source_package"],
            "event_kind": payload["event_kind"],
            "capture_method": payload["capture_method"],
            "finality": payload["finality"],
            "completeness": payload["completeness"],
        }
        with self._connect() as conn:
            existing_key = conn.execute(
                "SELECT * FROM chat_mirror_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            existing_sequence = conn.execute(
                """SELECT * FROM chat_mirror_events
                   WHERE mirror_session_id=? AND sequence_number=?""",
                (payload["mirror_session_id"], payload["sequence_number"]),
            ).fetchone()
        if existing_key is not None:
            if self._request_identity(existing_key) != self._request_identity(request_identity):
                raise ValueError("Idempotency-Key was reused with different chat mirror evidence")
            self._validate_indexed_evidence(existing_key)
            return self._result(existing_key, duplicate=True)
        if existing_sequence is not None:
            if self._request_identity(existing_sequence) != self._request_identity(request_identity):
                raise ValueError("chat mirror session sequence was reused with different evidence")
            self._validate_indexed_evidence(existing_sequence)
            return self._result(existing_sequence, duplicate=True)

        relative_dir = Path("chat-mirror") / "raw" / observed.date().isoformat()
        relative_payload = relative_dir / f"{event_id}.json"
        relative_metadata = relative_dir / f"{event_id}.meta.json"
        metadata = {
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "received_at": received.isoformat(),
            "received_epoch": received.timestamp(),
            **request_identity,
            "clock_basis": payload["clock_basis"],
            "observed_epoch": observed.timestamp(),
            "observed_text": payload["observed_text"],
            "observable_conversation_id": payload.get("observable_conversation_id"),
            "path": str(relative_payload),
            "metadata_path": str(relative_metadata),
        }
        self._validate_metadata_paths(metadata, self.root / relative_metadata)
        reserved, reservation_duplicate = self._reserve_sequence(metadata)
        metadata_path = self._resolve_evidence_path(str(reserved["metadata_path"]))
        payload_path = self._validate_metadata_paths(reserved, metadata_path)
        created_directory = not payload_path.parent.exists()
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(payload_path.parent, 0o700)
        if created_directory:
            self._fsync_directory(payload_path.parent.parent)
        committed = self._commit_evidence(payload_path, metadata_path, body, reserved)
        indexed_duplicate = self._insert_record(committed)
        duplicate = reservation_duplicate or indexed_duplicate
        if not duplicate:
            self._audit(
                "chat_mirror_ingested",
                event_id=committed["event_id"],
                mirror_session_id=committed["mirror_session_id"],
                sequence_number=committed["sequence_number"],
                payload_sha256=committed["payload_sha256"],
                capture_method=committed["capture_method"],
                completeness=committed["completeness"],
            )
        return self._result(committed, duplicate=duplicate)

    def _committed_sidecar_metadata(
        self,
        event_id: str,
        request_identity: dict[str, Any],
    ) -> dict[str, Any] | None:
        matches = sorted(self.chat_root.glob(f"*/{event_id}.meta.json"))
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("multiple committed sidecars exist for one chat mirror event")
        metadata_path = matches[0]
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload_path = self._validate_metadata_paths(metadata, metadata_path)
        if self._request_identity(metadata) != self._request_identity(request_identity):
            raise ValueError("Idempotency-Key was reused with different chat mirror evidence")
        self._validate_payload_file(metadata, payload_path)
        return metadata

    def _reserve_sequence(self, metadata: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_sequence = conn.execute(
                """SELECT metadata_json FROM chat_mirror_sequence_reservations
                   WHERE mirror_session_id=? AND sequence_number=?""",
                (metadata["mirror_session_id"], metadata["sequence_number"]),
            ).fetchone()
            existing_key = conn.execute(
                """SELECT metadata_json FROM chat_mirror_sequence_reservations
                   WHERE idempotency_key=?""",
                (metadata["idempotency_key"],),
            ).fetchone()
            existing_event = conn.execute(
                """SELECT metadata_json FROM chat_mirror_sequence_reservations
                   WHERE event_id=?""",
                (metadata["event_id"],),
            ).fetchone()
            rows = [row for row in (existing_sequence, existing_key, existing_event) if row is not None]
            if rows:
                records = [json.loads(str(row["metadata_json"])) for row in rows]
                event_ids = {str(record["event_id"]) for record in records}
                if len(event_ids) != 1:
                    conn.rollback()
                    raise ValueError("chat mirror sequence reservation conflicts with another event")
                existing = records[0]
                if self._request_identity(existing) != self._request_identity(metadata):
                    conn.rollback()
                    if existing_key is not None or existing_event is not None:
                        raise ValueError("Idempotency-Key was reused with different chat mirror evidence")
                    raise ValueError("chat mirror session sequence was reused with different evidence")
                self._validate_metadata_paths(
                    existing,
                    self._resolve_evidence_path(str(existing["metadata_path"])),
                )
                conn.commit()
                return existing, True
            conn.execute(
                """INSERT INTO chat_mirror_sequence_reservations(
                       mirror_session_id,sequence_number,event_id,idempotency_key,metadata_json
                   ) VALUES (?,?,?,?,?)""",
                (
                    metadata["mirror_session_id"],
                    metadata["sequence_number"],
                    metadata["event_id"],
                    metadata["idempotency_key"],
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                ),
            )
            conn.commit()
        return metadata, False

    def _commit_evidence(
        self,
        payload_path: Path,
        metadata_path: Path,
        body: bytes,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if metadata_path.exists():
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            existing_payload = self._validate_metadata_paths(existing, metadata_path)
            if existing.get("idempotency_key") != metadata["idempotency_key"]:
                raise ValueError("chat mirror evidence path contains a different idempotency key")
            if self._request_identity(existing) != self._request_identity(metadata):
                raise ValueError("Idempotency-Key was reused with different chat mirror evidence")
            self._validate_payload_file(existing, existing_payload)
            return existing
        committed = self._committed_sidecar_metadata(metadata["event_id"], metadata)
        if committed is not None:
            return committed

        payload_temporary = payload_path.with_suffix(".json.tmp")
        metadata_temporary = metadata_path.with_suffix(".json.tmp")
        self._write_bytes(
            metadata_temporary,
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        self._write_bytes(payload_temporary, body)
        self._fsync_directory(payload_path.parent)
        os.replace(payload_temporary, payload_path)
        self._fsync_directory(payload_path.parent)
        os.replace(metadata_temporary, metadata_path)
        self._fsync_directory(payload_path.parent)
        return metadata

    @staticmethod
    def _write_bytes(path: Path, body: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _validate_payload_file(self, metadata: Any, payload_path: Path) -> None:
        if (
            not payload_path.exists()
            or payload_path.stat().st_size != metadata["num_bytes"]
            or self._digest(payload_path.read_bytes()) != metadata["payload_sha256"]
        ):
            raise ValueError("indexed chat mirror evidence does not match its sidecar")

    def _validate_indexed_evidence(self, record: Any) -> None:
        metadata_path = self._resolve_evidence_path(str(record["metadata_path"]))
        if not metadata_path.exists():
            raise ValueError("indexed chat mirror evidence is missing its sidecar")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload_path = self._validate_metadata_paths(metadata, metadata_path)
        if (
            metadata["event_id"] != record["event_id"]
            or metadata["idempotency_key"] != record["idempotency_key"]
            or self._request_identity(metadata) != self._request_identity(record)
        ):
            raise ValueError("indexed chat mirror evidence does not match its sidecar")
        self._validate_payload_file(metadata, payload_path)

    def _insert_record(self, metadata: dict[str, Any]) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_key = conn.execute(
                "SELECT * FROM chat_mirror_events WHERE idempotency_key=?",
                (metadata["idempotency_key"],),
            ).fetchone()
            existing_sequence = conn.execute(
                """SELECT * FROM chat_mirror_events
                   WHERE mirror_session_id=? AND sequence_number=?""",
                (metadata["mirror_session_id"], metadata["sequence_number"]),
            ).fetchone()
            existing = existing_key or existing_sequence
            if existing is not None:
                if self._request_identity(existing) != self._request_identity(metadata):
                    conn.rollback()
                    if existing_key is not None:
                        raise ValueError("Idempotency-Key was reused with different chat mirror evidence")
                    raise ValueError("chat mirror session sequence was reused with different evidence")
                conn.execute(
                    "DELETE FROM chat_mirror_fts WHERE event_id=?",
                    (metadata["event_id"],),
                )
                conn.execute(
                    "INSERT INTO chat_mirror_fts(event_id,observed_text) VALUES (?,?)",
                    (metadata["event_id"], metadata["observed_text"]),
                )
                conn.commit()
                return True
            conn.execute(
                "DELETE FROM chat_mirror_fts WHERE event_id=?",
                (metadata["event_id"],),
            )
            conn.execute(
                """INSERT INTO chat_mirror_events(
                    event_id,idempotency_key,mirror_session_id,sequence_number,
                    observed_at,observed_epoch,received_at,received_epoch,clock_basis,
                    source_package,event_kind,capture_method,finality,completeness,
                    observed_text,observable_conversation_id,payload_sha256,num_bytes,
                    path,metadata_path
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    metadata["event_id"],
                    metadata["idempotency_key"],
                    metadata["mirror_session_id"],
                    metadata["sequence_number"],
                    metadata["observed_at"],
                    metadata["observed_epoch"],
                    metadata["received_at"],
                    metadata["received_epoch"],
                    metadata["clock_basis"],
                    metadata["source_package"],
                    metadata["event_kind"],
                    metadata["capture_method"],
                    metadata["finality"],
                    metadata["completeness"],
                    metadata["observed_text"],
                    metadata.get("observable_conversation_id"),
                    metadata["payload_sha256"],
                    metadata["num_bytes"],
                    metadata["path"],
                    metadata["metadata_path"],
                ),
            )
            conn.execute(
                "INSERT INTO chat_mirror_fts(event_id,observed_text) VALUES (?,?)",
                (metadata["event_id"], metadata["observed_text"]),
            )
            conn.commit()
        return False

    def _reconcile_interrupted_commits(self) -> None:
        for metadata_temporary in self.chat_root.rglob("*.meta.json.tmp"):
            try:
                metadata = json.loads(metadata_temporary.read_text(encoding="utf-8"))
                metadata_path = metadata_temporary.with_suffix("")
                payload_path = self._validate_metadata_paths(metadata, metadata_path)
                payload_temporary = payload_path.with_suffix(".json.tmp")
                candidate = payload_path if payload_path.exists() else payload_temporary
                self._validate_payload_file(metadata, candidate)
                if candidate == payload_temporary:
                    os.replace(payload_temporary, payload_path)
                    self._fsync_directory(payload_path.parent)
                os.replace(metadata_temporary, metadata_path)
                self._fsync_directory(payload_path.parent)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
                continue

    def _reconcile_sidecars(self) -> None:
        for metadata_path in self.chat_root.rglob("*.meta.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                payload_path = self._validate_metadata_paths(metadata, metadata_path)
                self._validate_payload_file(metadata, payload_path)
                reserved, _ = self._reserve_sequence(metadata)
                self._insert_record(reserved)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, sqlite3.Error):
                continue

    @staticmethod
    def _result(record: Any, *, duplicate: bool) -> ChatMirrorIngestResult:
        return ChatMirrorIngestResult(
            event_id=str(record["event_id"]),
            mirror_session_id=str(record["mirror_session_id"]),
            sequence_number=int(record["sequence_number"]),
            duplicate=duplicate,
            sha256=str(record["payload_sha256"]),
            received_at=str(record["received_at"]),
        )

    def get_event(self, event_id: str) -> dict[str, Any]:
        if _EVENT_ID_RE.fullmatch(event_id) is None:
            raise KeyError(event_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_mirror_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        self._validate_indexed_evidence(row)
        return dict(row)

    def get_session(self, mirror_session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM chat_mirror_events WHERE mirror_session_id=?
                   ORDER BY sequence_number,event_id""",
                (mirror_session_id,),
            ).fetchall()
        if not rows:
            raise KeyError(mirror_session_id)
        events = [dict(row) for row in rows]
        for row in rows:
            self._validate_indexed_evidence(row)
        return {
            "mirror_session_id": mirror_session_id,
            "event_count": len(events),
            "observed_start": events[0]["observed_at"],
            "observed_end": events[-1]["observed_at"],
            "events": events,
        }

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not 1 <= len(query) <= 500:
            raise ValueError("chat mirror search query is invalid")
        bounded_limit = max(1, min(int(limit), 100))
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT e.event_id,e.mirror_session_id,e.sequence_number,e.observed_at,
                              e.event_kind,e.capture_method,e.finality,e.completeness,
                              snippet(chat_mirror_fts,1,'[',']','…',24) AS snippet
                       FROM chat_mirror_fts f
                       JOIN chat_mirror_events e ON e.event_id=f.event_id
                       WHERE chat_mirror_fts MATCH ?
                       ORDER BY bm25(chat_mirror_fts),e.observed_epoch,e.event_id
                       LIMIT ?""",
                    (query, bounded_limit),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("chat mirror search query is invalid") from exc
        return [dict(row) for row in rows]

    def status(self) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS events,
                          COUNT(DISTINCT mirror_session_id) AS sessions,
                          COALESCE(SUM(num_bytes),0) AS raw_bytes
                   FROM chat_mirror_events"""
            ).fetchone()
        assert row is not None
        return {
            "events": int(row["events"]),
            "sessions": int(row["sessions"]),
            "raw_bytes": int(row["raw_bytes"]),
        }

    def _write_deletion_tombstone(self, mirror_session_id: str, rows: list[Any]) -> Path:
        digest = self._digest(mirror_session_id.encode("utf-8"))[:32]
        path = self.deletion_root / f"session-{digest}.json"
        new_events = [
            {
                "event_id": str(row["event_id"]),
                "path": str(row["path"]),
                "metadata_path": str(row["metadata_path"]),
                "payload_sha256": str(row["payload_sha256"]),
                "metadata_sha256": self._metadata_integrity_digest(row),
            }
            for row in rows
        ]
        if path.exists():
            tombstone = json.loads(path.read_text(encoding="utf-8"))
            if tombstone.get("mirror_session_id") != mirror_session_id:
                raise ValueError("chat mirror deletion tombstone session is invalid")
            existing_events = tombstone.get("events")
            if not isinstance(existing_events, list):
                raise ValueError("chat mirror deletion tombstone events are invalid")
            merged: dict[tuple[str, str, str], dict[str, str]] = {}
            for event in [*existing_events, *new_events]:
                if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
                    raise ValueError("chat mirror deletion tombstone event is invalid")
                normalized = {
                    "event_id": str(event["event_id"]),
                    "path": str(event["path"]),
                    "metadata_path": str(event["metadata_path"]),
                    "payload_sha256": str(event["payload_sha256"]),
                    "metadata_sha256": str(event["metadata_sha256"]),
                }
                evidence_key = (
                    normalized["event_id"],
                    normalized["path"],
                    normalized["metadata_path"],
                )
                prior = merged.get(evidence_key)
                if prior is not None and prior != normalized:
                    raise ValueError("chat mirror deletion tombstone event changed on retry")
                merged[evidence_key] = normalized
            tombstone["events"] = [merged[evidence_key] for evidence_key in sorted(merged)]
        else:
            tombstone = {
                "mirror_session_id": mirror_session_id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "events": new_events,
            }
        temporary = path.with_suffix(".json.tmp")
        self._write_bytes(
            temporary,
            json.dumps(tombstone, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        os.replace(temporary, path)
        self._fsync_directory(self.deletion_root)
        return path

    def _rewrite_deletion_tombstone(self, path: Path, tombstone: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        self._write_bytes(
            temporary,
            json.dumps(tombstone, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        self._fsync_directory(self.deletion_root)
        os.replace(temporary, path)
        self._fsync_directory(self.deletion_root)

    def _finalize_deletion_tombstone(self, path: Path) -> dict[str, Any]:
        tombstone = json.loads(path.read_text(encoding="utf-8"))
        session_id = self._bounded_identifier(
            tombstone.get("mirror_session_id"),
            "mirror session id",
        )
        expected_tombstone = (
            self.deletion_root
            / f"session-{self._digest(session_id.encode('utf-8'))[:32]}.json"
        ).resolve()
        if path.is_symlink() or path.resolve() != expected_tombstone:
            raise ValueError("chat mirror deletion tombstone path is invalid")
        events = tombstone.get("events")
        if not isinstance(events, list) or len(events) > 100_000:
            raise ValueError("chat mirror deletion event list is invalid")
        event_ids: list[str] = []
        directories: set[Path] = set()
        persisted_tampered = tombstone.get("tampered_event_ids", [])
        if (
            not isinstance(persisted_tampered, list)
            or any(
                not isinstance(event_id, str) or _EVENT_ID_RE.fullmatch(event_id) is None
                for event_id in persisted_tampered
            )
        ):
            raise ValueError("chat mirror deletion tamper list is invalid")
        tampered_event_ids = set(persisted_tampered)
        evidence_paths: list[tuple[Path, ...]] = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("chat mirror deletion event is invalid")
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or _EVENT_ID_RE.fullmatch(event_id) is None:
                raise ValueError("chat mirror deletion event id is invalid")
            payload_path = self._resolve_evidence_path(str(event["path"]))
            metadata_path = self._resolve_evidence_path(str(event["metadata_path"]))
            digest = event.get("payload_sha256")
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise ValueError("chat mirror deletion SHA-256 is invalid")
            metadata_digest = event.get("metadata_sha256")
            if not isinstance(metadata_digest, str) or _SHA256_RE.fullmatch(metadata_digest) is None:
                raise ValueError("chat mirror deletion metadata SHA-256 is invalid")
            if (
                payload_path.parent != metadata_path.parent
                or payload_path.name != f"{event_id}.json"
                or metadata_path.name != f"{event_id}.meta.json"
                or not payload_path.is_relative_to(self.chat_root.resolve())
            ):
                raise ValueError("chat mirror deletion paths are invalid")
            payload_temporary = payload_path.with_suffix(".json.tmp")
            metadata_temporary = metadata_path.with_suffix(".json.tmp")
            for candidate in (payload_path, payload_temporary):
                if candidate.exists() and self._digest(candidate.read_bytes()) != digest:
                    tampered_event_ids.add(event_id)
            for candidate in (metadata_path, metadata_temporary):
                if not candidate.exists():
                    continue
                try:
                    metadata = json.loads(candidate.read_text(encoding="utf-8"))
                    declared_payload = self._validate_metadata_paths(metadata, metadata_path)
                    if (
                        declared_payload != payload_path
                        or metadata.get("event_id") != event_id
                        or metadata.get("payload_sha256") != digest
                        or self._metadata_integrity_digest(metadata) != metadata_digest
                    ):
                        tampered_event_ids.add(event_id)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
                    tampered_event_ids.add(event_id)
            evidence_paths.append(
                (metadata_path, metadata_temporary, payload_path, payload_temporary)
            )
            directories.add(payload_path.parent)
            event_ids.append(event_id)
        if not tampered_event_ids.issubset(set(event_ids)):
            raise ValueError("chat mirror deletion tamper list references an unknown event")
        tombstone["tampered_event_ids"] = sorted(tampered_event_ids)
        tombstone["tamper_recorded_at"] = tombstone.get(
            "tamper_recorded_at",
            datetime.now(timezone.utc).isoformat(),
        )
        self._rewrite_deletion_tombstone(path, tombstone)
        for paths in evidence_paths:
            for evidence_path in paths:
                if evidence_path.exists():
                    evidence_path.unlink()
        for directory in directories:
            self._fsync_directory(directory)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for event_id in event_ids:
                conn.execute("DELETE FROM chat_mirror_fts WHERE event_id=?", (event_id,))
            conn.execute(
                "DELETE FROM chat_mirror_events WHERE mirror_session_id=?",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM chat_mirror_sequence_reservations WHERE mirror_session_id=?",
                (session_id,),
            )
            conn.commit()
        self._audit(
            "chat_mirror_session_deleted",
            deletion_id=path.stem,
            mirror_session_id=session_id,
            event_count=len(event_ids),
            tampered_event_count=len(tombstone["tampered_event_ids"]),
        )
        path.unlink()
        self._fsync_directory(self.deletion_root)
        return tombstone

    def _reconcile_deletions(self) -> None:
        for path in self.deletion_root.glob("session-*.json"):
            try:
                self._finalize_deletion_tombstone(path)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, sqlite3.Error):
                continue

    def _reservations_for_session(self, mirror_session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT metadata_json FROM chat_mirror_sequence_reservations
                   WHERE mirror_session_id=? ORDER BY sequence_number,event_id""",
                (mirror_session_id,),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(str(row["metadata_json"]))
            self._validate_metadata_paths(
                metadata,
                self._resolve_evidence_path(str(metadata["metadata_path"])),
            )
            records.append(metadata)
        return records

    def _sidecars_for_session(self, mirror_session_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for metadata_path in self.chat_root.glob("*/chat-*.meta.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("mirror_session_id") != mirror_session_id:
                    continue
                self._validate_metadata_paths(metadata, metadata_path)
                records.append(metadata)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
                continue
        return records

    def delete_session(self, mirror_session_id: str) -> bool:
        if _SESSION_ID_RE.fullmatch(mirror_session_id) is None:
            return False
        with self._ingest_lock:
            with self._process_archive_lock():
                with self._connect() as conn:
                    rows = conn.execute(
                        "SELECT * FROM chat_mirror_events WHERE mirror_session_id=?",
                        (mirror_session_id,),
                    ).fetchall()
                trusted: dict[str, dict[str, Any]] = {}
                for metadata in self._reservations_for_session(mirror_session_id):
                    trusted[str(metadata["event_id"])] = metadata
                for row in rows:
                    trusted[str(row["event_id"])] = dict(row)
                evidence: dict[tuple[str, str, str], dict[str, Any]] = {}
                for sidecar in self._sidecars_for_session(mirror_session_id):
                    event_id = str(sidecar["event_id"])
                    record = dict(trusted.get(event_id, sidecar))
                    record["path"] = sidecar["path"]
                    record["metadata_path"] = sidecar["metadata_path"]
                    key = (event_id, str(record["path"]), str(record["metadata_path"]))
                    evidence[key] = record
                for event_id, record in trusted.items():
                    key = (event_id, str(record["path"]), str(record["metadata_path"]))
                    evidence[key] = record
                existing_tombstone = self.deletion_root / (
                    f"session-{self._digest(mirror_session_id.encode('utf-8'))[:32]}.json"
                )
                if not evidence:
                    if not existing_tombstone.exists():
                        return False
                    self._finalize_deletion_tombstone(existing_tombstone)
                    return True
                tombstone = self._write_deletion_tombstone(
                    mirror_session_id,
                    list(evidence.values()),
                )
                self._finalize_deletion_tombstone(tombstone)
        return True

    def _audit(self, action: str, **fields: Any) -> None:
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), "action": action, **fields}
        path = self.audit_root / "events.jsonl"
        created = not path.exists()
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            self._fsync_directory(self.audit_root)
