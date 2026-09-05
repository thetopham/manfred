#!/usr/bin/env python3
"""Plan, copy, or verify Manfred runtime files; never activate services or move data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import venv

REPO = Path(__file__).resolve().parents[1]
ROLE_UNITS = {
    "none": (),
    "demerzel": ("manfred-ears-forward.service", "manfred-ears-forward.socket", "manfred-ears-wiki-sync.service", "manfred-ears-wiki-sync.timer"),
    "manfred-data-plane": ("manfred-ears-receiver.service", "manfred-ears-operator.service", "manfred-vision-receiver.service", "manfred-chat-mirror-receiver.service"),
}


def source_files() -> list[Path]:
    files = [REPO / name for name in ("pyproject.toml", "requirements.txt", "requirements-parakeet.txt", "README.md", "docs/ears.md")]
    for directory in ("manfred_ears", "deploy"):
        files.extend(path for path in (REPO / directory).rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"runtime source is not a regular file: {path}")
    return sorted(files)


def safe_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError(f"runtime path must not traverse symlinks: {path}")
    return path


def atomic_write(target: Path, payload: bytes, mode: int = 0o600) -> None:
    target = safe_path(target)
    if target.exists() and not target.is_file():
        raise RuntimeError(f"runtime target is not a regular file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if hasattr(os, "getuid") and target.parent.stat().st_uid != os.getuid():
        raise RuntimeError(f"runtime directory has another owner: {target.parent}")
    fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def values(args: argparse.Namespace) -> dict[str, str]:
    result = {
        "MANFRED_RUNTIME_ROOT": str(safe_path(args.runtime_root)),
        "MANFRED_STATE_DIR": str(safe_path(args.state_dir)),
        "MANFRED_ENV_FILE": str(safe_path(args.env_file)),
        "MANFRED_BRAIN_INBOX_DIR": str(safe_path(args.brain_inbox_dir)),
        "MANFRED_BRAIN_INBOX_PARENT": str(safe_path(args.brain_inbox_dir).parent),
        "MANFRED_EXPORT_SSH_KEY": str(safe_path(args.export_ssh_key)),
        "MANFRED_EXPORT_SOURCE": args.export_source,
        "MANFRED_RECEIVER_HOST": args.receiver_host,
        "MANFRED_VISION_HOST": args.vision_host,
        "MANFRED_CHAT_MIRROR_HOST": args.chat_mirror_host,
        "MANFRED_FORWARD_LISTEN": args.forward_listen,
        "MANFRED_FORWARD_TARGET": args.forward_target,
    }
    # Unit values are deliberately single safe tokens, never shell fragments.
    for key, value in result.items():
        if not re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
            raise RuntimeError(f"{key} contains unsupported whitespace or special characters")
    return result


def render_unit(name: str, args: argparse.Namespace) -> bytes:
    text = (REPO / "deploy/systemd" / name).read_text(encoding="utf-8")
    for key, value in values(args).items():
        text = text.replace(f"@{key}@", value)
    if re.search(r"@[A-Z_]+@", text):
        raise RuntimeError(f"unresolved unit placeholder: {name}")
    return text.encode("utf-8")


def launcher(args: argparse.Namespace) -> bytes:
    runtime = safe_path(args.runtime_root)
    return ("#!/bin/sh\nexec /usr/bin/env " + shlex.quote(f"PYTHONPATH={runtime}") + " " + shlex.quote(str(runtime / ".venv/bin/python")) + ' -m manfred_ears "$@"\n').encode()


def plan(args: argparse.Namespace) -> dict:
    runtime = safe_path(args.runtime_root)
    values(args)
    return {
        "schema": 1, "role": args.role, "runtime_root": str(runtime),
        "source_files": [str(path.relative_to(REPO)) for path in source_files()],
        "units": [str(safe_path(args.user_unit_dir) / name) for name in ROLE_UNITS[args.role]],
        "launcher": str(safe_path(args.bin_dir) / "manfred"),
        "state_dir": str(args.state_dir), "brain_inbox_dir": str(args.brain_inbox_dir),
        "environment_file": str(args.env_file),
        "environment_file_modified": False, "data_moved": False, "services_activated": False,
        "install_runtime_dependencies": args.install_runtime_deps,
    }


def apply(args: argparse.Namespace) -> None:
    plan(args)
    runtime = safe_path(args.runtime_root)
    for source in source_files():
        atomic_write(runtime / source.relative_to(REPO), source.read_bytes())
    python = runtime / ".venv/bin/python"
    if not args.skip_venv and not python.is_file():
        safe_path(runtime / ".venv")
        venv.EnvBuilder(with_pip=True, clear=False).create(runtime / ".venv")
    if args.install_runtime_deps:
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "-r", str(runtime / "requirements.txt")], check=True)
    atomic_write(safe_path(args.bin_dir) / "manfred", launcher(args), 0o700)
    for name in ROLE_UNITS[args.role]:
        atomic_write(safe_path(args.user_unit_dir) / name, render_unit(name, args))
    # Secrets, archives, SQLite state, and service lifecycle are deliberately untouched.


def check(args: argparse.Namespace) -> None:
    plan(args)
    runtime = safe_path(args.runtime_root)
    for source in source_files():
        target = safe_path(runtime / source.relative_to(REPO))
        if not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
            raise RuntimeError(f"installed runtime source differs: {target}")
    binary = safe_path(args.bin_dir) / "manfred"
    if binary.is_symlink() or not binary.is_file() or binary.read_bytes() != launcher(args):
        raise RuntimeError("installed Manfred launcher differs")
    for name in ROLE_UNITS[args.role]:
        target = safe_path(args.user_unit_dir) / name
        if target.is_symlink() or not target.is_file() or target.read_bytes() != render_unit(name, args):
            raise RuntimeError(f"installed unit differs: {name}")
    if not args.skip_venv:
        command = [str(runtime / ".venv/bin/python"), "-m", "manfred_ears", "--help"]
        env = dict(os.environ, PYTHONPATH=str(runtime))
        subprocess.run(command, cwd=runtime, env=env, check=True, timeout=30, stdout=subprocess.DEVNULL)
    if args.install_runtime_deps:
        subprocess.run([str(runtime / ".venv/bin/python"), "-c", "import fastapi, uvicorn, faster_whisper"], check=True, timeout=60)


def build_parser() -> argparse.ArgumentParser:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply", "check"))
    parser.add_argument("--role", choices=tuple(ROLE_UNITS), default="none")
    parser.add_argument("--runtime-root", type=Path, default=Path(os.getenv("MANFRED_RUNTIME_ROOT", str(home / ".local/share/manfred"))))
    parser.add_argument("--state-dir", type=Path, default=Path(os.getenv("MANFRED_STATE_DIR", str(home / ".hermes/manfred-ears"))))
    parser.add_argument("--env-file", type=Path, default=home / ".config/manfred-ears/manfred-ears.env")
    parser.add_argument("--bin-dir", type=Path, default=home / ".local/bin")
    parser.add_argument("--user-unit-dir", type=Path, default=home / ".config/systemd/user")
    parser.add_argument("--brain-inbox-dir", type=Path, default=Path(os.getenv("MANFRED_BRAIN_INBOX_DIR", str(home / ".hermes/manfred-ears/wiki-inbox"))))
    parser.add_argument("--export-source", default=os.getenv("MANFRED_EXPORT_SOURCE", "7090:/home/matt/.hermes/manfred-ears/wiki-inbox/"))
    parser.add_argument("--export-ssh-key", type=Path, default=Path(os.getenv("MANFRED_EXPORT_SSH_KEY", str(home / ".ssh/demerzel_fleet"))))
    parser.add_argument("--receiver-host", default=os.getenv("MANFRED_RECEIVER_HOST", "100.112.32.64"))
    parser.add_argument("--vision-host", default=os.getenv("MANFRED_VISION_HOST", "100.112.32.64"))
    parser.add_argument("--chat-mirror-host", default=os.getenv("MANFRED_CHAT_MIRROR_HOST", "100.112.32.64"))
    parser.add_argument("--forward-listen", default="100.126.233.3:8787")
    parser.add_argument("--forward-target", default="100.112.32.64:8787")
    parser.add_argument("--install-runtime-deps", action="store_true")
    parser.add_argument("--skip-venv", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.mode == "plan":
            print(json.dumps(plan(args), indent=2))
        else:
            (apply if args.mode == "apply" else check)(args)
            print(json.dumps({"status": "ok", "mode": args.mode, "role": args.role}))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"MANFRED_RUNTIME_ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
