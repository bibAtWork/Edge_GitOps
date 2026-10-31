"""Critical Renovate updates may auto-merge only within a patch line."""

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "pr-image-gate.py"
SPEC = importlib.util.spec_from_file_location("pr_image_gate_critical", MODULE_PATH)
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


class CriticalPatchPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patterns = GATE.critical_file_patterns()

    def changes(self, file, before, after):
        return GATE.critical_version_changes(file, [before], [after], self.patterns)

    def test_critical_chart_minor_requires_review_even_without_label(self):
        file = "cluster/base/infrastructure/01-seaweedfs/helmrelease.yaml"
        before = {"kind": "HelmRelease", "metadata": {"name": "seaweedfs"},
                  "spec": {"chart": {"spec": {"version": "4.48.0"}}}}
        after = {"kind": "HelmRelease", "metadata": {"name": "seaweedfs"},
                 "spec": {"chart": {"spec": {"version": "4.51.0"}}}}
        self.assertEqual(len(self.changes(file, before, after)), 1)
        after["spec"]["chart"]["spec"]["version"] = "4.48.1"
        self.assertEqual(self.changes(file, before, after), [])

    def test_critical_image_minor_requires_review(self):
        file = "cluster/base/infrastructure/05-cilium/helmrelease.yaml"
        before = {"kind": "HelmRelease", "metadata": {"name": "cilium"},
                  "spec": {"values": {"image": {"tag": "v1.20.4"}}}}
        after = {"kind": "HelmRelease", "metadata": {"name": "cilium"},
                 "spec": {"values": {"image": {"tag": "v1.21.0"}}}}
        self.assertEqual(len(self.changes(file, before, after)), 1)
        after["spec"]["values"]["image"]["tag"] = "v1.20.5"
        self.assertEqual(self.changes(file, before, after), [])

    def test_postgresql_two_part_maintenance_release_is_patch(self):
        file = "cluster/base/applications/lakehouse/deferred/postgres.yaml"
        image = "ghcr.io/cloudnative-pg/postgresql:16.15-minimal-bookworm"
        before = {"kind": "Cluster", "metadata": {"name": "lakehouse-pg"},
                  "spec": {"imageName": image + "@sha256:old"}}
        after = {"kind": "Cluster", "metadata": {"name": "lakehouse-pg"},
                 "spec": {"imageName": image.replace("16.15", "16.16") + "@sha256:new"}}
        self.assertEqual(self.changes(file, before, after), [])
        after["spec"]["imageName"] = image.replace("16.15", "17.0")
        self.assertEqual(len(self.changes(file, before, after)), 1)
        after["spec"]["imageName"] = image + "@sha256:new"
        self.assertEqual(len(self.changes(file, before, after)), 1)

    def test_admin_talos_minor_requires_review(self):
        file = "bootstrap/ansible/group_vars/all.yml"
        before = {"talosctl_version": "1.14.4"}
        after = {"talosctl_version": "1.15.0"}
        self.assertEqual(len(self.changes(file, before, after)), 1)
        after["talosctl_version"] = "1.14.5"
        self.assertEqual(self.changes(file, before, after), [])

    def test_noncritical_grafana_minor_is_not_blocked(self):
        file = "cluster/base/infrastructure/04-grafana/helmrelease.yaml"
        before = {"kind": "HelmRelease", "metadata": {"name": "grafana"},
                  "spec": {"chart": {"spec": {"version": "10.2.0"}}}}
        after = {"kind": "HelmRelease", "metadata": {"name": "grafana"},
                 "spec": {"chart": {"spec": {"version": "10.3.0"}}}}
        self.assertEqual(self.changes(file, before, after), [])

    def test_gate_refuses_critical_minor_before_manual_label_arrives(self):
        change = {"file": "cluster/base/infrastructure/01-seaweedfs/helmrelease.yaml",
                  "path": "HelmRelease/seaweedfs.spec.chart.spec.version",
                  "old": "4.48.0", "new": "4.51.0"}
        verdict = GATE.evaluate({"critical_changes": [change]}, "unused", "unused",
                                ["minor-update"])
        self.assertFalse(verdict["merge"])
        self.assertIn("outside a patch release", verdict["blockers"][0])


if __name__ == "__main__":
    unittest.main()
