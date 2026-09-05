#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_android_local.py"


def load_module():
    spec = importlib.util.spec_from_file_location("validate_manfred_android_local", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManfredAndroidLocalValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_empty_signing_environment_is_explicitly_ephemeral(self):
        self.assertIsNone(self.module.signing_config({}))

    def test_partial_signing_environment_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "partial Manfred signing configuration"):
            self.module.signing_config({"MANFRED_ANDROID_KEY_ALIAS": "testing"})

    def test_complete_signing_environment_uses_immutable_certificate_pin(self):
        values = {key: "value" for key in self.module.SIGNING_KEYS}
        values["MANFRED_ANDROID_CERT_SHA256"] = "AA:BB"
        config = self.module.signing_config(values)
        assert config is not None
        self.assertEqual(
            config["MANFRED_ANDROID_CERT_SHA256"],
            self.module.DEFAULT_CERT_SHA256,
        )

    def test_validation_subprocess_environment_never_contains_signing_material(self):
        values = {key: f"secret-{key}" for key in self.module.SIGNING_KEYS}
        values["MANFRED_ANDROID_CERT_SHA256"] = "override"
        values["PATH"] = "/safe/path"
        clean = self.module.scrubbed_validation_environment(values)
        self.assertEqual(clean["PATH"], "/safe/path")
        for key in (*self.module.SIGNING_KEYS, "MANFRED_ANDROID_CERT_SHA256"):
            self.assertNotIn(key, clean)

    def test_latest_android_build_tools_signer_is_selected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            older = root / "build-tools/34.0.0/apksigner"
            newer = root / "build-tools/35.0.1/apksigner"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.touch()
            newer.touch()
            selected = self.module.find_apksigner({"ANDROID_HOME": str(root)})
        self.assertEqual(selected, newer)

    def test_apksigner_on_path_is_supported_without_android_home(self):
        with tempfile.TemporaryDirectory() as raw:
            signer = Path(raw) / "apksigner"
            signer.touch()
            signer.chmod(0o755)
            selected = self.module.find_apksigner({"PATH": raw})
        self.assertEqual(selected, signer)

    def test_ephemeral_default_cannot_overwrite_stable_default(self):
        self.assertNotEqual(
            self.module.DEFAULT_EPHEMERAL_OUTPUT,
            self.module.DEFAULT_STABLE_OUTPUT,
        )

    def test_wrapped_base64_keystore_is_accepted_but_invalid_data_is_rejected(self):
        import base64

        encoded = base64.b64encode(b"keystore-bytes").decode("ascii")
        wrapped = encoded[:8] + "\n  " + encoded[8:] + "\n"
        self.assertEqual(self.module.decode_keystore(wrapped), b"keystore-bytes")
        with self.assertRaises(ValueError):
            self.module.decode_keystore("not base64!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
