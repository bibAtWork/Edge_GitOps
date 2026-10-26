"""Keep the filer metadata restore image compatible with its database."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.15-minimal-bookworm@sha256:3181bfd1304928fb237d45d57fe61f05abd0e995d3e01e3cd383c3e5637057f3"


def source(path):
    full_path = ROOT / path
    if str(full_path).startswith("C:"):
        full_path = Path("\\\\?\\" + str(full_path))
    return full_path.read_text()


class FilerPgImage(unittest.TestCase):
    def test_live_cluster_and_aws_restore_drill_use_scanned_image(self):
        cluster = yaml.safe_load(source("cluster/base/infrastructure/01-seaweedfs/deferred/filer-metadata-pg-cluster.yaml"))
        self.assertEqual(cluster["spec"]["imageName"], IMAGE)
        drill = source("cluster/base/infrastructure/37-backup-system/deferred/workflow-templates/drill-filer-aws.yaml")
        self.assertIn(f"imageName: {IMAGE}", drill)


if __name__ == "__main__":
    unittest.main()
