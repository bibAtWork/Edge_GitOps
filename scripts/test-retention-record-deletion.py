#!/usr/bin/env python3
"""Exercise the real retention deletion block with controlled rclone outcomes."""

from pathlib import Path
import os
import shutil
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "cluster/base/infrastructure/37-backup-system/deferred/workflow-templates/retention.yaml"


class RetentionRecordDeletion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which("sh")
        if not cls.shell:
            raise RuntimeError("A POSIX sh is required for the retention regression test")
        document = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        records = next(t for t in document["spec"]["templates"] if t["name"] == "records")
        source = records["container"]["args"][0]
        cls.source = source
        # Include the conditional exactly as deployed. The following purge is a
        # separate existing best-effort operation, not the record deletion.
        cls.block = source[source.index('if rclone deletefile "$F"; then'):
                           source.index('rclone purge "$PREFIX/$APP/$RP"')]

    def run_deletion(self, status):
        source = '''set -eu
F='vault:recovery/recovery-points/example/rp.json'
RP=rp
rclone() {
  [ "$1" = deletefile ] && [ "$2" = "$F" ] || exit 99
  echo "remote diagnostic" >&2
  return "$RCLONE_TEST_EXIT"
}
'''
        env = dict(os.environ, RCLONE_TEST_EXIT=str(status))
        return subprocess.run(
            [self.shell, "-s"], input=source + self.block + '\necho cleanup-continues\n',
            capture_output=True, text=True, env=env, check=False,
        )

    def test_records_shell_syntax(self):
        result = subprocess.run(
            [self.shell, "-n"], input=self.source,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_success_and_already_absent_continue(self):
        for status in (0, 4):
            with self.subTest(status=status):
                result = self.run_deletion(status)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("cleanup-continues", result.stdout)
                if status == 4:
                    self.assertIn("record already absent", result.stdout)

    def test_other_failures_stop_and_preserve_exit_status_and_diagnostics(self):
        # Includes directory/bucket absence (3), transient failures (5), fatal
        # failures (7), limits and interruptions. None is a missing record.
        for status in (1, 2, 3, 5, 6, 7, 8, 9, 10, 137, 143):
            with self.subTest(status=status):
                result = self.run_deletion(status)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertNotIn("cleanup-continues", result.stdout)
                self.assertIn("remote diagnostic", result.stderr)
                self.assertIn(f"record deletion failed (rclone exit {status})", result.stderr)


if __name__ == "__main__":
    unittest.main()
