#!/usr/bin/env python3
"""Validate Manfred source and isolated tests without touching live evidence."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    for folder in ("manfred_ears", "deploy", "scripts", "tests"):
        for path in (REPO / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
                if any(module.split(".")[0] in {"llm_brain", "demerzel_idle", "demerzel_fleet"} for module in modules):
                    raise RuntimeError(f"cross-repository private import: {path}")
    materializer = REPO / "apps/manfred-companion/tool/materialize_android.sh"
    subprocess.run(["bash", "-n", str(materializer)], check=True)
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], cwd=REPO, env=env, check=False)
    if completed.returncode:
        return completed.returncode
    print("MANFRED_VALIDATION_OK python_tests=true shell_syntax=true private_imports=false android_device_test=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
