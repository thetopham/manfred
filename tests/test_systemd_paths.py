#!/usr/bin/env python3
"""Unit templates must belong to the independent Manfred runtime."""
from pathlib import Path
import unittest

UNITS = Path(__file__).resolve().parents[1] / "deploy/systemd"


class ManfredSystemdPathTests(unittest.TestCase):
    def test_manfred_units_do_not_depend_on_a_brain_checkout_or_runtime(self):
        for path in UNITS.iterdir():
            text = path.read_text()
            self.assertNotIn("llm-brain", text, path.name)
            self.assertNotIn("/wiki/ingestion/", text, path.name)
            self.assertNotIn("demerzel-idle-compute", text, path.name)
            if "WorkingDirectory=" in text:
                self.assertIn("WorkingDirectory=@MANFRED_RUNTIME_ROOT@", text)
            if "ReadWritePaths=" in text:
                self.assertTrue("@MANFRED_STATE_DIR@" in text or "@MANFRED_BRAIN_INBOX_PARENT@" in text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
