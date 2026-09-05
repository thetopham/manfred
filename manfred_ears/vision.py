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


@dataclass(frozen=True)
class VisionIngestResult:
    visual_id: str
    duplicate: bool
    sha256: str
    received_at: str


class VisionArchive:
    """Canonical Frame JPEG archive for the Noa compatibility surface.

    Noa's custom-server request time is emitted after capture and has no
    timezone. The receiver therefore retains it verbatim but uses server receive
    time as the initial capture estimate with an explicit, low-quality basis.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.state_dir
        self.db_path = self.root / "archive.sqlite3"
        self.visual_root = self.root / "vision" / "raw"
        self.deletion_root = self.root / "vision" / "deletions"
        self.audit_root = self.root / "audit"
        self.lock_path = self.root / "vision" / ".archive.lock"
        self._ingest_lock = threading.Lock()
        for path in (
            self.root,
            self.lock_path.parent,
            self.visual_root,
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS visual_events (
                    visual_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    received_at TEXT NOT NULL,
                    received_epoch REAL NOT NULL,
                    capture_estimate TEXT NOT NULL,
                    capture_time_basis TEXT NOT NULL,
                    client_reported_time TEXT,
                    source TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    num_bytes INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    metadata_path TEXT NOT NULL,
                    frame_audio_sha256 TEXT,
                    frame_audio_num_bytes INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS visual_events_time ON visual_events(received_epoch,visual_id)"
            )
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _resolve_evidence_path(self, value: str) -> Path:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("visual evidence path escapes the state root")
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError("visual evidence path escapes the state root")
        return resolved

    def _validate_jpeg(self, image: bytes) -> None:
        if not image:
            raise ValueError("Frame image is empty")
        if len(image) > self.settings.max_vision_image_bytes:
            raise ValueError("Frame image exceeds configured maximum")
        if len(image) < 4 or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
            raise ValueError("Frame image is not a complete JPEG")

    def _validate_metadata_paths(self, metadata: dict[str, Any], metadata_path: Path) -> Path:
        visual_id = metadata.get("visual_id")
        if not isinstance(visual_id, str) or not re.fullmatch(r"frame-[0-9a-f]{32}", visual_id):
            raise ValueError("visual evidence id is invalid")
        idempotency_key = metadata.get("idempotency_key")
        if (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 200
            or any(ord(character) < 32 for character in idempotency_key)
        ):
            raise ValueError("visual idempotency key is invalid")
        digest = metadata.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("visual evidence SHA-256 is invalid")
        num_bytes = metadata.get("num_bytes")
        if not isinstance(num_bytes, int) or not 1 <= num_bytes <= self.settings.max_vision_image_bytes:
            raise ValueError("visual evidence byte count is invalid")
        if metadata.get("source") != "brilliant-frame-via-noa":
            raise ValueError("visual evidence source is invalid")
        if metadata.get("trigger") != "noa-tap-query":
            raise ValueError("visual evidence trigger is invalid")
        declared_metadata = self._resolve_evidence_path(str(metadata["metadata_path"]))
        image_path = self._resolve_evidence_path(str(metadata["path"]))
        if declared_metadata != metadata_path.resolve():
            raise ValueError("visual metadata path does not match its sidecar")
        if image_path.parent != declared_metadata.parent or image_path.name != f"{visual_id}.jpg":
            raise ValueError("visual image path does not match its evidence id")
        return image_path

    @staticmethod
    def _bounded_client_time(value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 128 or any(ord(character) < 32 for character in value):
            raise ValueError("Noa client time is invalid")
        return value

    @staticmethod
    def _request_identity(record: Any) -> tuple[Any, ...]:
        return (
            record["sha256"],
            int(record["num_bytes"]),
            record["client_reported_time"],
            record["frame_audio_sha256"],
            int(record["frame_audio_num_bytes"]),
            record["source"],
            record["trigger"],
        )

    def _committed_sidecar_metadata(
        self,
        visual_id: str,
        request_identity: dict[str, Any],
    ) -> dict[str, Any] | None:
        matches = sorted(self.visual_root.glob(f"*/{visual_id}.json"))
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("multiple committed sidecars exist for one visual id")
        metadata_path = matches[0]
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        image_path = self._validate_metadata_paths(metadata, metadata_path)
        if self._request_identity(metadata) != self._request_identity(request_identity):
            raise ValueError("Idempotency-Key was reused with different retained provenance")
        if (
            not image_path.exists()
            or image_path.stat().st_size != metadata["num_bytes"]
            or self._digest(image_path.read_bytes()) != metadata["sha256"]
        ):
            raise ValueError("committed visual evidence does not match its metadata")
        return metadata

    def _validate_indexed_evidence(self, record: Any) -> None:
        metadata_path = self._resolve_evidence_path(str(record["metadata_path"]))
        if not metadata_path.exists():
            raise ValueError("indexed visual evidence is missing its sidecar")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        image_path = self._validate_metadata_paths(metadata, metadata_path)
        if (
            metadata["visual_id"] != record["visual_id"]
            or metadata["idempotency_key"] != record["idempotency_key"]
            or self._request_identity(metadata) != self._request_identity(record)
            or not image_path.exists()
            or image_path.stat().st_size != record["num_bytes"]
            or self._digest(image_path.read_bytes()) != record["sha256"]
        ):
            raise ValueError("indexed visual evidence does not match its sidecar")

    def ingest_noa_jpeg(
        self,
        *,
        image: bytes,
        frame_audio: bytes = b"",
        client_reported_time: str | None = None,
        idempotency_key: str | None = None,
        received_at: datetime | None = None,
    ) -> VisionIngestResult:
        with self._ingest_lock:
            with self._process_archive_lock():
                return self._ingest_noa_jpeg_locked(
                    image=image,
                    frame_audio=frame_audio,
                    client_reported_time=client_reported_time,
                    idempotency_key=idempotency_key,
                    received_at=received_at,
                )

    def _ingest_noa_jpeg_locked(
        self,
        *,
        image: bytes,
        frame_audio: bytes,
        client_reported_time: str | None,
        idempotency_key: str | None,
        received_at: datetime | None,
    ) -> VisionIngestResult:
        self._validate_jpeg(image)
        client_time = self._bounded_client_time(client_reported_time)
        received = received_at or datetime.now(timezone.utc)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        received = received.astimezone(timezone.utc)
        image_sha256 = self._digest(image)
        frame_audio_sha256 = self._digest(frame_audio) if frame_audio else None

        if idempotency_key is None:
            stable = "\0".join(
                (client_time or received.isoformat(), image_sha256, frame_audio_sha256 or "")
            )
            idempotency_key = f"noa:{self._digest(stable.encode('utf-8'))}"
        if not 1 <= len(idempotency_key) <= 200 or any(ord(character) < 32 for character in idempotency_key):
            raise ValueError("Idempotency-Key length or content is invalid")

        visual_id = f"frame-{self._digest(idempotency_key.encode('utf-8'))[:32]}"
        pending_deletion = self.deletion_root / f"{visual_id}.json"
        if pending_deletion.exists():
            deleted = self._finalize_deletion_tombstone(pending_deletion)
            self._audit(
                "visual_deleted_recovered",
                visual_id=deleted["visual_id"],
                sha256=deleted["sha256"],
            )

        request_identity = {
            "sha256": image_sha256,
            "num_bytes": len(image),
            "client_reported_time": client_time,
            "frame_audio_sha256": frame_audio_sha256,
            "frame_audio_num_bytes": len(frame_audio),
            "source": "brilliant-frame-via-noa",
            "trigger": "noa-tap-query",
        }
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT visual_id,idempotency_key,received_at,sha256,num_bytes,
                       client_reported_time,frame_audio_sha256,frame_audio_num_bytes,
                       source,trigger,path,metadata_path
                FROM visual_events WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
        if existing is not None:
            if self._request_identity(existing) != self._request_identity(request_identity):
                raise ValueError("Idempotency-Key was reused with different retained provenance")
            self._validate_indexed_evidence(existing)
            return VisionIngestResult(
                visual_id=existing["visual_id"],
                duplicate=True,
                sha256=existing["sha256"],
                received_at=existing["received_at"],
            )

        committed_metadata = self._committed_sidecar_metadata(visual_id, request_identity)
        if committed_metadata is not None:
            self._insert_metadata(committed_metadata)
            self._audit(
                "visual_index_recovered",
                visual_id=committed_metadata["visual_id"],
                sha256=committed_metadata["sha256"],
            )
            return VisionIngestResult(
                visual_id=committed_metadata["visual_id"],
                duplicate=True,
                sha256=committed_metadata["sha256"],
                received_at=committed_metadata["received_at"],
            )

        relative_dir = Path("vision") / "raw" / received.date().isoformat()
        relative_image = relative_dir / f"{visual_id}.jpg"
        relative_metadata = relative_dir / f"{visual_id}.json"
        image_path = self.root / relative_image
        metadata_path = self.root / relative_metadata
        daily_directory_created = not image_path.parent.exists()
        image_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(image_path.parent, 0o700)
        if daily_directory_created:
            self._fsync_directory(image_path.parent.parent)

        metadata: dict[str, Any] = {
            "visual_id": visual_id,
            "idempotency_key": idempotency_key,
            "received_at": received.isoformat(),
            "received_epoch": received.timestamp(),
            "capture_estimate": received.isoformat(),
            "capture_time_basis": "noa-request-receive-time",
            "client_reported_time": client_time,
            "source": "brilliant-frame-via-noa",
            "trigger": "noa-tap-query",
            "sha256": image_sha256,
            "num_bytes": len(image),
            "path": str(relative_image),
            "metadata_path": str(relative_metadata),
            "frame_audio_sha256": frame_audio_sha256,
            "frame_audio_num_bytes": len(frame_audio),
        }
        self._validate_metadata_paths(metadata, metadata_path)
        committed_metadata = self._commit_evidence(image_path, metadata_path, image, metadata)
        duplicate = self._insert_metadata(committed_metadata)
        if not duplicate:
            self._audit(
                "visual_ingested",
                visual_id=visual_id,
                sha256=committed_metadata["sha256"],
                num_bytes=committed_metadata["num_bytes"],
                source=committed_metadata["source"],
                trigger=committed_metadata["trigger"],
                capture_time_basis=committed_metadata["capture_time_basis"],
            )
        return VisionIngestResult(
            visual_id=committed_metadata["visual_id"],
            duplicate=duplicate,
            sha256=committed_metadata["sha256"],
            received_at=committed_metadata["received_at"],
        )

    def _commit_evidence(
        self,
        image_path: Path,
        metadata_path: Path,
        image: bytes,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if metadata_path.exists():
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            self._validate_metadata_paths(existing, metadata_path)
            if existing.get("idempotency_key") != metadata["idempotency_key"]:
                raise ValueError("visual evidence path contains a different idempotency key")
            if self._request_identity(existing) != self._request_identity(metadata):
                raise ValueError("Idempotency-Key was reused with different retained provenance")
            if not image_path.exists() or self._digest(image_path.read_bytes()) != metadata["sha256"]:
                raise ValueError("committed visual evidence does not match its metadata")
            return existing

        image_temporary = image_path.with_suffix(".jpg.tmp")
        metadata_temporary = metadata_path.with_suffix(".json.tmp")
        self._write_bytes(
            metadata_temporary,
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        self._write_bytes(image_temporary, image)
        self._fsync_directory(image_path.parent)
        os.replace(image_temporary, image_path)
        self._fsync_directory(image_path.parent)
        os.replace(metadata_temporary, metadata_path)
        self._fsync_directory(image_path.parent)
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

    def _validate_deletion_tombstone(
        self,
        tombstone: dict[str, Any],
        tombstone_path: Path,
    ) -> tuple[Path, Path]:
        visual_id = tombstone.get("visual_id")
        if not isinstance(visual_id, str) or not re.fullmatch(r"frame-[0-9a-f]{32}", visual_id):
            raise ValueError("visual deletion id is invalid")
        expected_tombstone = (self.deletion_root / f"{visual_id}.json").resolve()
        if tombstone_path.is_symlink() or tombstone_path.resolve() != expected_tombstone:
            raise ValueError("visual deletion tombstone path is invalid")
        digest = tombstone.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("visual deletion SHA-256 is invalid")
        image_path = self._resolve_evidence_path(str(tombstone["path"]))
        metadata_path = self._resolve_evidence_path(str(tombstone["metadata_path"]))
        if (
            image_path.parent != metadata_path.parent
            or image_path.name != f"{visual_id}.jpg"
            or metadata_path.name != f"{visual_id}.json"
            or not image_path.is_relative_to(self.visual_root.resolve())
        ):
            raise ValueError("visual deletion evidence paths are invalid")
        return image_path, metadata_path

    def _write_deletion_tombstone(self, row: Any) -> Path:
        visual_id = str(row["visual_id"])
        tombstone_path = self.deletion_root / f"{visual_id}.json"
        tombstone = {
            "visual_id": visual_id,
            "path": str(row["path"]),
            "metadata_path": str(row["metadata_path"]),
            "sha256": str(row["sha256"]),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        if tombstone_path.exists():
            existing = json.loads(tombstone_path.read_text(encoding="utf-8"))
            self._validate_deletion_tombstone(existing, tombstone_path)
            if any(existing.get(field) != tombstone[field] for field in ("visual_id", "path", "metadata_path", "sha256")):
                raise ValueError("visual deletion tombstone conflicts with the archive row")
            return tombstone_path

        temporary = tombstone_path.with_suffix(".json.tmp")
        self._write_bytes(
            temporary,
            json.dumps(tombstone, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        os.replace(temporary, tombstone_path)
        self._fsync_directory(self.deletion_root)
        return tombstone_path

    def _finalize_deletion_tombstone(self, tombstone_path: Path) -> dict[str, Any]:
        tombstone = json.loads(tombstone_path.read_text(encoding="utf-8"))
        image_path, metadata_path = self._validate_deletion_tombstone(tombstone, tombstone_path)
        visual_id = str(tombstone["visual_id"])
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT sha256 FROM visual_events WHERE visual_id=?",
                (visual_id,),
            ).fetchone()
            if row is not None and row["sha256"] != tombstone["sha256"]:
                conn.rollback()
                raise ValueError("visual deletion tombstone conflicts with the archive row")
            for path in (metadata_path, image_path):
                if path.exists():
                    path.unlink()
            self._fsync_directory(image_path.parent)
            conn.execute("DELETE FROM visual_events WHERE visual_id=?", (visual_id,))
            conn.commit()
        tombstone_path.unlink()
        self._fsync_directory(self.deletion_root)
        return tombstone

    def _reconcile_deletions(self) -> None:
        for tombstone_path in self.deletion_root.glob("frame-*.json"):
            try:
                tombstone = self._finalize_deletion_tombstone(tombstone_path)
                self._audit(
                    "visual_deleted_recovered",
                    visual_id=tombstone["visual_id"],
                    sha256=tombstone["sha256"],
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, sqlite3.Error):
                # A valid tombstone remains durable until deletion can finish safely.
                continue

    def _reconcile_interrupted_commits(self) -> None:
        for metadata_temporary in self.visual_root.rglob("*.json.tmp"):
            try:
                metadata = json.loads(metadata_temporary.read_text(encoding="utf-8"))
                metadata_path = metadata_temporary.with_suffix("")
                image_path = self._validate_metadata_paths(metadata, metadata_path)
                image_temporary = image_path.with_suffix(".jpg.tmp")
                candidate = image_path if image_path.exists() else image_temporary
                if (
                    not candidate.exists()
                    or candidate.stat().st_size != metadata["num_bytes"]
                    or self._digest(candidate.read_bytes()) != metadata["sha256"]
                ):
                    continue
                if candidate == image_temporary:
                    os.replace(image_temporary, image_path)
                    self._fsync_directory(image_path.parent)
                os.replace(metadata_temporary, metadata_path)
                self._fsync_directory(image_path.parent)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
                # Preserve malformed or unverifiable temporary evidence for inspection.
                continue

    def _reconcile_sidecars(self) -> None:
        for metadata_path in self.visual_root.rglob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                image_path = self._validate_metadata_paths(metadata, metadata_path)
                if (
                    not image_path.exists()
                    or image_path.stat().st_size != metadata["num_bytes"]
                    or self._digest(image_path.read_bytes()) != metadata["sha256"]
                ):
                    continue
                self._insert_metadata(metadata)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, sqlite3.Error):
                # Preserve evidence and continue; operator tooling can inspect it.
                continue

    def _insert_metadata(self, metadata: dict[str, Any]) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT visual_id,sha256,num_bytes,client_reported_time,
                       frame_audio_sha256,frame_audio_num_bytes,source,trigger
                FROM visual_events WHERE idempotency_key=?
                """,
                (metadata["idempotency_key"],),
            ).fetchone()
            if existing is not None:
                if self._request_identity(existing) != self._request_identity(metadata):
                    conn.rollback()
                    raise ValueError("Idempotency-Key was reused with different retained provenance")
                conn.rollback()
                return True
            conn.execute(
                """
                INSERT INTO visual_events(
                    visual_id,idempotency_key,received_at,received_epoch,capture_estimate,
                    capture_time_basis,client_reported_time,source,trigger,sha256,num_bytes,
                    path,metadata_path,frame_audio_sha256,frame_audio_num_bytes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    metadata["visual_id"],
                    metadata["idempotency_key"],
                    metadata["received_at"],
                    metadata["received_epoch"],
                    metadata["capture_estimate"],
                    metadata["capture_time_basis"],
                    metadata.get("client_reported_time"),
                    metadata["source"],
                    metadata["trigger"],
                    metadata["sha256"],
                    metadata["num_bytes"],
                    metadata["path"],
                    metadata["metadata_path"],
                    metadata.get("frame_audio_sha256"),
                    metadata.get("frame_audio_num_bytes", 0),
                ),
            )
            conn.commit()
        return False

    def get(self, visual_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM visual_events WHERE visual_id=?", (visual_id,)).fetchone()
        if row is None:
            raise KeyError(visual_id)
        return dict(row)

    def status(self) -> dict[str, int]:
        with self._connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM visual_events").fetchone()[0])
            raw_bytes = int(
                conn.execute("SELECT COALESCE(SUM(num_bytes),0) FROM visual_events").fetchone()[0]
            )
        return {"visual_events": count, "visual_raw_bytes": raw_bytes}

    def delete(self, visual_id: str) -> bool:
        if not re.fullmatch(r"frame-[0-9a-f]{32}", visual_id):
            return False
        with self._ingest_lock:
            with self._process_archive_lock():
                tombstone_path = self.deletion_root / f"{visual_id}.json"
                if tombstone_path.exists():
                    tombstone = self._finalize_deletion_tombstone(tombstone_path)
                    self._audit(
                        "visual_deleted_recovered",
                        visual_id=visual_id,
                        sha256=tombstone["sha256"],
                    )
                    return True
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT visual_id,path,metadata_path,sha256 FROM visual_events WHERE visual_id=?",
                        (visual_id,),
                    ).fetchone()
                if row is None:
                    return False
                tombstone_path = self._write_deletion_tombstone(row)
                tombstone = self._finalize_deletion_tombstone(tombstone_path)
        self._audit("visual_deleted", visual_id=visual_id, sha256=tombstone["sha256"])
        return True

    def _audit(self, action: str, **fields: Any) -> None:
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), "action": action, **fields}
        path = self.audit_root / "events.jsonl"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
