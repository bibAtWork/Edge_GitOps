"""Regression cases for CVE identity changes, ownership and missing scan history."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "cve_inventory", ROOT / "cluster/base/infrastructure/30-image-cve-alerts/cve_inventory.py")
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)
IMAGE = json.dumps(["apps", "docker.io", "library/example", "sha256:abc"], separators=(",", ":"))


def fixtures(kind="ReplicaSet"):
    obj = {"metadata": {"namespace": "apps", "name": "example", "uid": "current"},
           "spec": {"replicas": 1, "template": {"spec": {
               "containers": [{"name": "app", "image": "example:1.0"}]}}}, "status": {}}
    if kind == "Pod":
        obj["spec"] = obj["spec"]["template"]["spec"]
        obj["status"]["phase"] = "Running"
    if kind == "CronJob":
        obj["spec"] = {"jobTemplate": {"spec": obj["spec"]}}
    report = {"metadata": {"namespace": "apps", "labels": {
        inventory.PREFIX + "kind": kind, inventory.PREFIX + "name": "example",
        "trivy-operator.container.name": "app"},
        "ownerReferences": [{"kind": kind, "name": "example", "uid": "current"}]},
        "report": {"registry": {"server": "docker.io"},
                   "artifact": {"repository": "library/example", "digest": "sha256:abc", "tag": "1.0"},
                   "vulnerabilities": [{"vulnerabilityID": "CVE-A", "severity": "CRITICAL",
                                        "fixedVersion": ""}]}}
    return report, {("apps", kind, "example"): obj}, obj


class CVEInventory(unittest.TestCase):
    def test_inventory_alerts_evaluate_within_their_confirmation_windows(self):
        release = yaml.safe_load((ROOT / "cluster/base/infrastructure/04-grafana/helmrelease.yaml").read_text(encoding="utf-8"))
        groups = release["spec"]["values"]["alerting"]["rules.yaml"]["groups"]
        wanted = {"ImageNewCriticalCVE", "ImageCVEFixAvailable", "ImageCVEInventoryStale"}
        covered = set()
        for group in groups:
            for rule in group["rules"]:
                if rule["title"] in wanted:
                    covered.add(rule["title"])
                    # A 1m confirmation in a 15m evaluation group takes two
                    # evaluations, defeating prompt changes and stale detection.
                    self.assertEqual(group["interval"], "1m", rule["title"])
        self.assertEqual(covered, wanted)

    def seed(self, findings=None):
        return inventory.advance({IMAGE: findings or {"CVE-A": ["Critical", False]}}, None, 1000)[0]

    def test_first_inventory_is_quiet_but_digest_is_populated(self):
        state, summary, _, _ = inventory.advance({IMAGE: {"CVE-A": ["Critical", False]}}, None, 1000)
        self.assertEqual(state["events"], [])
        self.assertEqual(list(summary.values()), [1])

    def test_packages_and_report_copies_deduplicate(self):
        report, workloads, _ = fixtures()
        duplicate = deepcopy(report)
        duplicate["report"]["vulnerabilities"] *= 3
        duplicate["report"]["vulnerabilities"][0]["fixedVersion"] = "1.1"
        images, excluded = inventory.inventory([report, duplicate], workloads)
        self.assertEqual(images[IMAGE], {"CVE-A": ["Critical", True]})
        self.assertEqual(excluded, 0)
        state = inventory.advance(images, None, 1000)[0]
        self.assertEqual(inventory.advance(images, state, 1300)[0]["events"], [])

    def test_inactive_replicaset_and_completed_manual_job_are_excluded(self):
        for kind in ("ReplicaSet", "Job", "Pod", "CronJob"):
            report, workloads, obj = fixtures(kind)
            if kind == "ReplicaSet":
                obj["spec"]["replicas"] = 0
            elif kind == "Job":
                obj["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
            elif kind == "Pod":
                obj["status"]["phase"] = "Failed"
            else:
                obj["spec"]["suspend"] = True
            with self.subTest(kind=kind):
                self.assertEqual(inventory.inventory([report], workloads), ({}, 1))

    def test_declared_cronjob_remains_in_scope_without_running_pod(self):
        report, workloads, _ = fixtures("CronJob")
        self.assertIn(IMAGE, inventory.inventory([report], workloads)[0])

    def test_reused_names_and_old_container_versions_are_excluded(self):
        report, workloads, obj = fixtures()
        obj["metadata"]["uid"] = "replacement"
        self.assertEqual(inventory.inventory([report], workloads), ({}, 1))
        obj["metadata"]["uid"] = "current"
        obj["spec"]["template"]["spec"]["containers"][0]["image"] = "example:2.0"
        self.assertEqual(inventory.inventory([report], workloads), ({}, 1))

    def test_digest_pins_and_init_containers_match(self):
        report, workloads, obj = fixtures()
        obj["spec"]["template"]["spec"] = {"initContainers": [
            {"name": "app", "image": "docker.io/library/example:1.0@sha256:abc"}]}
        self.assertIn(IMAGE, inventory.inventory([report], workloads)[0])

    def test_new_cve_is_detected_even_when_total_count_does_not_rise(self):
        state = inventory.advance({IMAGE: {"CVE-B": ["Critical", False]}}, self.seed(), 1300)[0]
        self.assertEqual([(e["kind"], e["count"]) for e in state["events"]], [("new_critical", 1)])

    def test_image_with_no_previous_series_is_detected(self):
        other = IMAGE.replace("sha256:abc", "sha256:def")
        state = inventory.advance({other: {"CVE-A": ["Critical", True]}}, self.seed(), 1300)[0]
        self.assertEqual(state["events"][0]["kind"], "new_critical")

    def test_missing_reports_retain_history_without_realert_on_return(self):
        state = inventory.advance({}, self.seed(), 1300)[0]
        self.assertIn(IMAGE, state["images"])
        images = {IMAGE: {"CVE-A": ["Critical", False]}}
        self.assertEqual(inventory.advance(images, state, 1600)[0]["events"], [])

    def test_severity_upgrade_and_new_fix_are_separate_changes(self):
        state = self.seed({"CVE-A": ["High", False], "CVE-B": ["High", False]})
        images = {IMAGE: {"CVE-A": ["Critical", True], "CVE-B": ["High", True]}}
        state = inventory.advance(images, state, 1300)[0]
        self.assertEqual(sorted((e["kind"], e["count"]) for e in state["events"]),
                         [("new_critical", 1), ("new_fix", 1)])
        self.assertEqual(len(inventory.advance(images, state, 1600)[0]["events"]), 2)

    def test_resolved_then_reintroduced_finding_is_new(self):
        state = inventory.advance({IMAGE: {}}, self.seed(), 1300)[0]
        state = inventory.advance({IMAGE: {"CVE-A": ["Critical", False]}}, state, 1600)[0]
        self.assertEqual(state["events"][0]["kind"], "new_critical")

    def test_state_survives_metrics_delivery_failure_and_replays_events(self):
        images = {IMAGE: {"CVE-B": ["Critical", False]}}
        committed = inventory.advance(images, self.seed(), 1300)[0]
        state, summary, cleared, expired = inventory.advance(images, committed, 1600)
        text = inventory.metrics(state, summary, cleared, expired, 1600, 1, 0)
        self.assertIn('event_id="' + committed["events"][0]["id"] + '"', text)
        self.assertEqual(len(state["events"]), 1)

    def test_expired_events_and_removed_images_emit_zero(self):
        images = {IMAGE: {"CVE-B": ["Critical", False]}}
        committed = inventory.advance(images, self.seed(), 1300)[0]
        state, summary, cleared, expired = inventory.advance({}, committed, 1300 + inventory.EVENT_TTL)
        text = inventory.metrics(state, summary, cleared, expired, 8500, 0, 0)
        self.assertEqual(state["events"], [])
        self.assertTrue(any(line.startswith('cve_inventory_event{') and line.endswith(' 0')
                            for line in text.splitlines()))
        self.assertTrue(any(line.startswith('cve_inventory_image_findings{') and line.endswith(' 0')
                            for line in text.splitlines()))

    def test_corrupt_state_and_malformed_current_report_fail(self):
        with self.assertRaises(ValueError):
            inventory.advance({}, {"schema": 2}, 1300)
        report, workloads, _ = fixtures()
        report["report"]["vulnerabilities"] = None
        with self.assertRaises(ValueError):
            inventory.inventory([report], workloads)

    def test_retention_and_size_budget_are_bounded(self):
        self.assertEqual(inventory.advance({}, self.seed(), 1001 + inventory.RETENTION)[0]["images"], {})
        previous = self.seed()
        with patch.object(inventory, "MAX_STATE_BYTES", 10):
            with self.assertRaises(ValueError):
                inventory.advance({}, previous, 1300)
        self.assertIn(IMAGE, previous["images"])

    def test_prometheus_labels_escape_quotes_backslashes_and_newlines(self):
        text = inventory.metric("example", {"label": 'a"b\\c\nd'}, 1)
        self.assertEqual(text, 'example{label="a\\"b\\\\c\\nd"} 1')


if __name__ == "__main__":
    unittest.main()
