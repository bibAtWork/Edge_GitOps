"""CAO executes scheduled flow scripts directly, so they need an interpreter."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CAO = ROOT / "cluster/base/applications/cao-sre"


class CaoFlowScriptTests(unittest.TestCase):
    def test_scheduled_scripts_have_interpreter(self):
        for flow in (CAO / "flows").glob("*.md"):
            match = re.search(r"^script: /config/([\w.-]+)$", flow.read_text(), re.M)
            if match is None:
                continue
            with self.subTest(flow=flow.name):
                script = CAO / "runtime" / match.group(1)
                self.assertTrue(script.is_file())
                self.assertTrue(script.read_bytes().startswith(b"#!/usr/bin/env python3\n"))


if __name__ == "__main__":
    unittest.main()
