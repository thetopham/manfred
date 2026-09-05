#!/usr/bin/env python3
"""Pull validated Manfred Ears Wiki exports from 7090.

The 7090 owns canonical raw audio, transcripts, events, and SQLite state.
Demerzel receives only the bounded ``wiki-inbox/YYYY-MM-DD.md`` scratch
exports consumed by the nightly Hermes conversation collector.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from datetime import date, datetime, timezone
from dataclasses import dataclass
from pathlib import Path

MAX_EXPORT_BYTES = 1_048_576
POLICY_MARKER = "Local scratch input for daily LLM Wiki distillation"
DEFAULT_SOURCE = os.getenv("MANFRED_EXPORT_SOURCE", "7090:/home/matt/.hermes/manfred-ears/wiki-inbox/")
DEFAULT_DESTINATION = Path(os.getenv("MANFRED_BRAIN_INBOX_DIR", str(Path.home() / ".hermes/manfred-ears/wiki-inbox"))).expanduser()
SSH_COMMAND = (
    "/usr/bin/ssh -i " + shlex.quote(os.getenv("MANFRED_EXPORT_SSH_KEY", str(Path.home() / ".ssh/demerzel_fleet"))) + " "
    "-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "
    "-o ServerAliveInterval=10 -o ServerAliveCountMax=2"
)


class ExportValidationError(RuntimeError):
    """The remote scratch export did not satisfy the bounded contract."""


@dataclass(frozen=True)
class ValidatedExport:
    payload: bytes
    sha256: str
    mtime_ns: int


def validate_export(path: Path) -> ValidatedExport:
    if path.is_symlink() or not path.is_file():
        raise ExportValidationError(f"not a regular file: {path.name}")
    if path.stat().st_size > MAX_EXPORT_BYTES:
        raise ExportValidationError(f"export exceeds {MAX_EXPORT_BYTES} bytes: {path.name}")
    try:
        observed = date.fromisoformat(path.stem)
    except ValueError as exc:
        raise ExportValidationError(f"invalid export date filename: {path.name}") from exc
    if path.name != f"{observed.isoformat()}.md":
        raise ExportValidationError(f"non-canonical export filename: {path.name}")
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExportValidationError(f"export is not UTF-8: {path.name}") from exc
    expected_heading = f"# Wearable transcript context — {observed.isoformat()}"
    if not text.startswith(expected_heading + "\n"):
        raise ExportValidationError(f"invalid export heading: {path.name}")
    if POLICY_MARKER not in text:
        raise ExportValidationError(f"missing local-scratch policy marker: {path.name}")
    return ValidatedExport(payload, hashlib.sha256(payload).hexdigest(), path.stat().st_mtime_ns)


def load_validated_exports(stage: Path, *, allow_empty: bool = False) -> dict[str, ValidatedExport]:
    unexpected = [p.name for p in stage.iterdir() if p.is_dir() or p.suffix != ".md"]
    if unexpected:
        raise ExportValidationError(f"unexpected staged entries: {', '.join(sorted(unexpected))}")
    exports = {p.name: validate_export(p) for p in sorted(stage.glob("*.md"))}
    if not exports and not allow_empty:
        raise ExportValidationError("remote Wiki inbox was empty; preserving the last good mirror")
    return exports


def _ensure_directory(path: Path) -> None:
    if path.is_symlink():
        raise ExportValidationError(f"destination must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _atomic_write(
    path: Path, payload: bytes, mode: int = 0o600, *, mtime_ns: int | None = None
) -> None:
    _ensure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def validate_existing_mirror(destination: Path) -> None:
    _ensure_directory(destination)
    for path in sorted(destination.glob("*.md")):
        validate_export(path)


def _exchange_directories(left: Path, right: Path) -> None:
    """Atomically exchange two same-filesystem directory entries on Linux."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ExportValidationError("renameat2 is unavailable; refusing non-transactional publish")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{left} <-> {right}")


def publish_staged_mirror(candidate: Path, destination: Path) -> None:
    if candidate.is_symlink() or not candidate.is_dir():
        raise ExportValidationError("candidate mirror must be a regular directory")
    if destination.is_symlink() or not destination.is_dir():
        raise ExportValidationError("destination mirror must be a regular directory")
    status = candidate / "wiki-sync-status.json"
    if status.is_symlink() or not status.is_file():
        raise ExportValidationError("candidate mirror is missing its regular status file")
    _exchange_directories(candidate, destination)


def pull_exports(source: str, stage: Path, *, link_destination: Path | None = None) -> None:
    command = [
        "/usr/bin/rsync",
        "--recursive",
        "--times",
        "--omit-dir-times",
        "--no-links",
        "--checksum",
        "--max-size=1m",
        "--timeout=30",
        "--info=SKIP2",
        "--chmod=F600,D700",
        "-e",
        SSH_COMMAND,
        source,
        f"{stage}/",
    ]
    if link_destination is not None:
        command.insert(command.index("-e"), f"--link-dest={link_destination.resolve()}")
    result = subprocess.run(command, check=True, timeout=90, capture_output=True, text=True)
    diagnostics = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if diagnostics:
        raise ExportValidationError(f"rsync skipped or warned about a remote export: {diagnostics}")


def sync(source: str, destination: Path, *, allow_empty: bool = False) -> dict[str, object]:
    _ensure_directory(destination)
    validate_existing_mirror(destination)
    with tempfile.TemporaryDirectory(
        prefix=".wiki-inbox-sync-", dir=destination.parent, ignore_cleanup_errors=True
    ) as raw_stage:
        stage = Path(raw_stage)
        pull_exports(source, stage, link_destination=destination)
        exports = load_validated_exports(stage, allow_empty=allow_empty)
        status = {
            "schema": 1,
            "source": source,
            "destination": str(destination),
            "canonical_archive_host": "7090",
            "mirror_host": "demerzel",
            "last_success_utc": datetime.now(timezone.utc).isoformat(),
            "export_count": len(exports),
            "files": {name: export.sha256 for name, export in exports.items()},
        }
        _atomic_write(
            stage / "wiki-sync-status.json",
            (json.dumps(status, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        publish_staged_mirror(stage, destination)

    with contextlib.suppress(OSError):
        (destination.parent / "wiki-sync-status.json").unlink()
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    status = sync(args.source, args.destination.expanduser(), allow_empty=args.allow_empty)
    print(f"MANFRED_WIKI_SYNC_OK count={status['export_count']} destination={status['destination']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
