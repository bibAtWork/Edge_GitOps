"""Regression cases for the upgrade Job's kubectl version-skew guard."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kubectl_image_compat as guard  # noqa: E402


class KubectlImageCompatibility(unittest.TestCase):
    def test_current_image_supports_both_sides_of_136_to_137(self):
        self.assertEqual(guard.compatibility({"v1.34.11", "v1.35.8", "v1.36.4"},
                                             "v1.36.3", "v1.37.1"), ["v1.36.4"])

    def test_current_image_cannot_authorize_138_upgrade(self):
        with self.assertRaisesRegex(ValueError, "within one minor of both"):
            guard.compatibility({"v1.34.11", "v1.35.8", "v1.36.4"},
                                "v1.37.1", "v1.38.0")

    def test_binary_must_report_the_version_in_its_name(self):
        output = 'BINARY:/usr/bin/kubectl1.36.4\n{"clientVersion":{"gitVersion":"v1.36.4"}}\n'
        self.assertEqual(guard.bundled_versions(output), {"v1.36.4"})
        with self.assertRaisesRegex(ValueError, "disagree"):
            guard.bundled_versions(output.replace('"gitVersion":"v1.36.4"',
                                                  '"gitVersion":"v1.35.8"'))

    def test_live_gate_images_track_controller_job_image(self):
        self.assertEqual(guard.normalized(guard.configured_image()),
                         "rancher/kuberlr-kubectl:v8.1.2")


if __name__ == "__main__":
    unittest.main()
