"""Version review rules match Renovate for critical and application updates."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_renovate_labels_only_immich_and_paperless_majors_for_review(self):
        config = json.loads((MODULE_PATH.parents[1] / "renovate.json").read_text(encoding="utf-8"))
        rule = next(rule for rule in config["packageRules"] if rule.get("description", "").startswith(
            "Major-only Immich and Paperless manifests:"))
        self.assertEqual(rule["matchUpdateTypes"], ["major"])
        self.assertEqual(rule["addLabels"], ["manual-review"])
        paths = ("cluster/base/infrastructure/16-immich/helmrelease.yaml",
                 "cluster/base/infrastructure/17-paperless-ngx/deployment.yaml")
        for path in paths:
            self.assertFalse(any(pattern.search(path) for pattern in self.patterns))
            self.assertTrue(any(pattern.search(path) for pattern in GATE.major_only_file_patterns()))

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

    def test_alerting_grafana_minor_requires_review(self):
        file = "cluster/base/infrastructure/04-grafana/helmrelease.yaml"
        before = {"kind": "HelmRelease", "metadata": {"name": "grafana"},
                  "spec": {"chart": {"spec": {"version": "10.2.0"}}}}
        after = {"kind": "HelmRelease", "metadata": {"name": "grafana"},
                 "spec": {"chart": {"spec": {"version": "10.3.0"}}}}
        self.assertEqual(len(self.changes(file, before, after)), 1)

    def test_resource_metrics_minor_requires_review(self):
        file = "cluster/base/infrastructure/29-metrics-server/helmrelease.yaml"
        before = {"kind": "HelmRelease", "metadata": {"name": "metrics-server"},
                  "spec": {"values": {"image": {"tag": "v0.8.1"}}}}
        after = {"kind": "HelmRelease", "metadata": {"name": "metrics-server"},
                 "spec": {"values": {"image": {"tag": "v0.9.1"}}}}
        self.assertEqual(len(self.changes(file, before, after)), 1)

    def test_immich_chart_and_app_minor_allowed_but_major_requires_review(self):
        file = "cluster/base/infrastructure/16-immich/helmrelease.yaml"
        before = {"kind": "HelmRelease", "metadata": {"name": "immich"},
                  "spec": {"chart": {"spec": {"version": "0.13.4"}},
                           "values": {"controllers": {"main": {"containers": {
                               "main": {"image": {"tag": "v3.3.6"}}}}}}}}
        after = {"kind": "HelmRelease", "metadata": {"name": "immich"},
                 "spec": {"chart": {"spec": {"version": "0.14.0"}},
                          "values": {"controllers": {"main": {"containers": {
                              "main": {"image": {"tag": "v3.4.0"}}}}}}}}
        self.assertEqual(self.changes(file, before, after), [])
        after["spec"]["values"]["controllers"]["main"]["containers"]["main"]["image"]["tag"] = "v4.0.0"
        self.assertEqual(len(self.changes(file, before, after)), 1)
        after["spec"]["chart"]["spec"]["version"] = "1.0.0"
        self.assertEqual(len(self.changes(file, before, after)), 2)

    def test_immich_vectorchord_minor_allowed_but_major_requires_review(self):
        file = "cluster/base/infrastructure/16-immich/deferred/postgres-cluster.yaml"
        before = {"kind": "Cluster", "metadata": {"name": "immich-pg"},
                  "spec": {"imageName": "ghcr.io/tensorchord/cloudnative-vectorchord:17-1.1.1"}}
        after = {"kind": "Cluster", "metadata": {"name": "immich-pg"},
                 "spec": {"imageName": "ghcr.io/tensorchord/cloudnative-vectorchord:17-1.2.0"}}
        self.assertEqual(self.changes(file, before, after), [])
        after["spec"]["imageName"] = "ghcr.io/tensorchord/cloudnative-vectorchord:17-2.0.0"
        self.assertEqual(len(self.changes(file, before, after)), 1)
        after["spec"]["imageName"] = "ghcr.io/tensorchord/cloudnative-vectorchord:18-1.2.0"
        self.assertEqual(len(self.changes(file, before, after)), 1)

    def test_paperless_minor_allowed_but_major_requires_review(self):
        file = "cluster/base/infrastructure/17-paperless-ngx/deployment.yaml"
        before = {"kind": "Deployment", "metadata": {"name": "paperless-ngx"},
                  "spec": {"template": {"spec": {"containers": [
                      {"image": "ghcr.io/paperless-ngx/paperless-ngx:3.4.2"}]}}}}
        after = {"kind": "Deployment", "metadata": {"name": "paperless-ngx"},
                 "spec": {"template": {"spec": {"containers": [
                     {"image": "ghcr.io/paperless-ngx/paperless-ngx:3.5.0"}]}}}}
        self.assertEqual(self.changes(file, before, after), [])
        after["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/paperless-ngx/paperless-ngx:4.0.0"
        self.assertEqual(len(self.changes(file, before, after)), 1)

    def test_gate_refuses_critical_minor_before_manual_label_arrives(self):
        change = {"file": "cluster/base/infrastructure/01-seaweedfs/helmrelease.yaml",
                  "path": "HelmRelease/seaweedfs.spec.chart.spec.version",
                  "old": "4.48.0", "new": "4.51.0"}
        verdict = GATE.evaluate({"critical_changes": [change]}, "unused", "unused",
                                ["minor-update"])
        self.assertFalse(verdict["merge"])
        self.assertIn("requires review under the version policy", verdict["blockers"][0])

    def test_vectorchord_image_gate_orders_minor_and_blocks_major(self):
        old = "ghcr.io/tensorchord/cloudnative-vectorchord:17-1.1.1"
        pair = {"old": old, "new": "ghcr.io/tensorchord/cloudnative-vectorchord:17-1.2.0",
                "kind": "static", "path": "Cluster/immich-pg.spec.imageName"}
        with patch.object(GATE, "scan_image", return_value=0.0), \
                patch.object(GATE, "better_candidate", return_value=(True, "no better tag")):
            verdict = GATE.evaluate({"pairs": [pair]}, "unused", "unused", ["minor-update"])
            self.assertTrue(verdict["merge"])
            pair["new"] = "ghcr.io/tensorchord/cloudnative-vectorchord:17-2.0.0"
            verdict = GATE.evaluate({"pairs": [pair]}, "unused", "unused", ["minor-update"])
            self.assertFalse(verdict["merge"])


if __name__ == "__main__":
    unittest.main()
