#!/usr/bin/env python3
"""Validate, build, optionally stable-sign, and retain Manfred APKs."""
from __future__ import annotations

import argparse
import base64
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Mapping

REPO = Path(__file__).resolve().parents[1]
MATERIALIZER = REPO / "apps/manfred-companion/tool/materialize_android.sh"
DEFAULT_STABLE_OUTPUT = Path.home() / "artifacts/manfred-companion/app-debug.apk"
DEFAULT_EPHEMERAL_OUTPUT = Path.home() / "artifacts/manfred-companion/app-debug-ephemeral.apk"
DEFAULT_CERT_SHA256 = "466ffc0979de2eb69e1a2859377388b244f5f333291c04d555c7b27e3c43bab5"
SIGNING_KEYS = (
    "MANFRED_ANDROID_KEYSTORE_BASE64",
    "MANFRED_ANDROID_KEYSTORE_PASSWORD",
    "MANFRED_ANDROID_KEY_PASSWORD",
    "MANFRED_ANDROID_KEY_ALIAS",
)


def run(
    label: str,
    command: tuple[str, ...],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    print(f"MANFRED_ANDROID_VALIDATION_START {label}", flush=True)
    completed = subprocess.run(command, cwd=cwd, env=env, check=False)
    if completed.returncode != 0:
        print(
            f"MANFRED_ANDROID_VALIDATION_ERROR check={label!r} exit_code={completed.returncode}",
            file=sys.stderr,
        )
    return completed.returncode


def signing_config(environ: Mapping[str, str]) -> dict[str, str] | None:
    present = {key: environ.get(key, "") for key in SIGNING_KEYS}
    populated = [key for key, value in present.items() if value]
    if not populated:
        return None
    if len(populated) != len(SIGNING_KEYS):
        missing = sorted(set(SIGNING_KEYS) - set(populated))
        raise RuntimeError("partial Manfred signing configuration; missing: " + ", ".join(missing))
    present["MANFRED_ANDROID_CERT_SHA256"] = DEFAULT_CERT_SHA256
    return present


def scrubbed_validation_environment(environ: Mapping[str, str]) -> dict[str, str]:
    clean = dict(environ)
    for key in (*SIGNING_KEYS, "MANFRED_ANDROID_CERT_SHA256"):
        clean.pop(key, None)
    return clean


def find_apksigner(environ: Mapping[str, str]) -> Path:
    on_path = shutil.which("apksigner", path=environ.get("PATH"))
    if on_path:
        return Path(on_path)
    android_home = environ.get("ANDROID_HOME") or environ.get("ANDROID_SDK_ROOT")
    if not android_home:
        raise RuntimeError("ANDROID_HOME or ANDROID_SDK_ROOT is required for stable signing")
    candidates = [Path(path) for path in glob.glob(str(Path(android_home) / "build-tools/*/apksigner"))]
    if not candidates:
        raise RuntimeError("apksigner was not found under Android build-tools")

    def version_key(path: Path) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", path.parent.name))

    return max(candidates, key=version_key)


def decode_keystore(value: str) -> bytes:
    normalized = "".join(str(value).split())
    return base64.b64decode(normalized, validate=True)


def stable_sign(apk: Path, config: Mapping[str, str], workdir: Path, environ: Mapping[str, str]) -> None:
    apksigner = find_apksigner(environ)
    keystore = workdir / "manfred-companion-signing.p12"
    ephemeral = workdir / "app-debug-ephemeral.apk"
    try:
        keystore.write_bytes(decode_keystore(config["MANFRED_ANDROID_KEYSTORE_BASE64"]))
        keystore.chmod(0o600)
        apk.replace(ephemeral)
        sign_env = dict(os.environ)
        sign_env.update(config)
        subprocess.run(
            (
                str(apksigner),
                "sign",
                "--ks",
                str(keystore),
                "--ks-type",
                "PKCS12",
                "--ks-key-alias",
                config["MANFRED_ANDROID_KEY_ALIAS"],
                "--ks-pass",
                "env:MANFRED_ANDROID_KEYSTORE_PASSWORD",
                "--key-pass",
                "env:MANFRED_ANDROID_KEY_PASSWORD",
                "--out",
                str(apk),
                str(ephemeral),
            ),
            check=True,
            env=sign_env,
            stdout=subprocess.DEVNULL,
        )
        verified = subprocess.run(
            (str(apksigner), "verify", "--verbose", "--print-certs", str(apk)),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
        marker = "certificate SHA-256 digest:"
        digests = {
            line.rsplit(marker, 1)[1].strip().replace(":", "").lower()
            for line in verified.splitlines()
            if marker in line
        }
        if digests != {config["MANFRED_ANDROID_CERT_SHA256"]}:
            raise RuntimeError("stable signer certificate digest did not match the pinned certificate")
    finally:
        keystore.unlink(missing_ok=True)
        ephemeral.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-stable-signing",
        action="store_true",
        help="fail unless the stable signing environment is complete and the pinned certificate verifies",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    flutter = shutil.which("flutter")
    if flutter is None:
        print("MANFRED_ANDROID_VALIDATION_ERROR Flutter stable is required on PATH", file=sys.stderr)
        return 2
    try:
        config = signing_config(os.environ)
        validation_env = scrubbed_validation_environment(os.environ)
        if args.require_stable_signing and config is None:
            raise RuntimeError("stable signing is required but no Manfred signing environment is configured")
        with tempfile.TemporaryDirectory(prefix="manfred-companion-") as raw:
            root = Path(raw)
            project = root / "manfred_companion"
            checks = (
                ("materialize", ("bash", str(MATERIALIZER), str(project)), REPO),
                ("analyze", (flutter, "analyze"), project),
                ("test", (flutter, "test"), project),
                (
                    "native EyeVue tests",
                    ("bash", "./gradlew", "--no-daemon", ":app:testDebugUnitTest"),
                    project / "android",
                ),
                (
                    "debug APK build",
                    (flutter, "build", "apk", "--debug", "--target-platform", "android-arm64"),
                    project,
                ),
            )
            for label, command, cwd in checks:
                exit_code = run(label, command, cwd, env=validation_env)
                if exit_code != 0:
                    return exit_code
                if label == "native EyeVue tests":
                    reports = list(project.glob("build/app/test-results/testDebugUnitTest/TEST-*.xml"))
                    totals = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
                    for report in reports:
                        result = ET.parse(report).getroot()
                        for name in totals:
                            totals[name] += int(result.attrib.get(name, "0"))
                    if not reports or totals["tests"] == 0:
                        raise RuntimeError("Native EyeVue test reports were not produced")
                    print("MANFRED_NATIVE_TESTS " + " ".join(f"{key}={value}" for key, value in totals.items()), flush=True)
            apk = project / "build/app/outputs/flutter-apk/app-debug.apk"
            if not apk.is_file():
                raise RuntimeError("Flutter reported success but app-debug.apk is missing")
            stable_signed = config is not None
            if config is not None:
                stable_sign(apk, config, root, os.environ)
            selected_output = args.output or (
                DEFAULT_STABLE_OUTPUT if stable_signed else DEFAULT_EPHEMERAL_OUTPUT
            )
            output = selected_output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(apk, output)
        print(
            "MANFRED_ANDROID_VALIDATION_OK "
            f"checks={len(checks)} stable_signed={str(stable_signed).lower()} output={output}"
        )
        if not stable_signed:
            print("MANFRED_ANDROID_ARTIFACT_EPHEMERAL do_not_install_over_stable_build=true")
        return 0
    except Exception as exc:
        print(f"MANFRED_ANDROID_VALIDATION_ERROR {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
