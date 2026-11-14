"""Offline contracts for bounded infrastructure alert routing."""

import json
import unittest
from unittest.mock import patch

from alert_webhook import active_alerts, apply_notification
from crew_controller import packet_validator
from gate import decision
from infra_context import collect, queries_for, validate_packet


class InfraAlertTests(unittest.TestCase):
    def test_recovery_identity_and_resolve_are_per_dataset(self):
        def notice(dataset, status="firing"):
            return {"alerts": [{"status": status, "fingerprint": dataset,
                                "labels": {"alertname": "RecoveryPointStale",
                                           "application": "immich", "dataset": dataset}}]}
        state = apply_notification({}, notice("postgres"), 100)
        state = apply_notification(state, notice("photos"), 101)
        self.assertEqual(len(state), 2)
        self.assertEqual(len(active_alerts(state, 102)), 2)
        state = apply_notification(state, notice("postgres", "resolved"), 103)
        self.assertEqual(len(state), 1)
        self.assertEqual(next(iter(state.values()))["dataset"], "photos")

    def test_recovery_without_scope_is_not_enqueued(self):
        alert = {"status": "firing", "fingerprint": "one",
                 "labels": {"alertname": "RecoveryOffsiteStale"}}
        self.assertEqual(apply_notification({}, {"alerts": [alert]}, 100), {})

    def test_unrelated_alert_is_ignored(self):
        alert = {"status": "firing", "fingerprint": "one",
                 "labels": {"alertname": "FalcoCriticalEvent"}}
        self.assertEqual(apply_notification({}, {"alerts": [alert]}, 100), {})

    def test_one_incident_per_firing_episode(self):
        signal = [{"key": "LonghornDiskNotReady:disk-a",
                   "confirmed_alert": "LonghornDiskNotReady"}]
        selected, state = decision(signal, {}, 100)
        repeated, state = decision(signal, state, 200)
        self.assertIsNotNone(selected)
        self.assertIsNone(repeated)
        decision([], state, 300)
        decision([], state, 400)
        rearmed, _ = decision(signal, state, 500)
        self.assertIsNotNone(rearmed)

    def test_collector_uses_targeted_workflow_and_redacts_spec(self):
        alert = {"alertname": "RecoveryPointStale", "application": "immich",
                 "dataset": "postgres", "fingerprint": "one"}
        self.assertIn("recovery-point-immich", queries_for(alert)[0][0])
        def get(path, receipts, max_bytes):
            self.assertEqual(max_bytes, 4_000_000)
            receipts.append({"method": "GET", "target": path, "status": 200})
            return {"items": [{"metadata": {"name": "safe"},
                               "spec": {"secret": "must-not-copy"},
                               "status": {"phase": "Failed", "message": "must-not-copy"}}]}
        with patch("infra_context.kube_get", side_effect=get):
            packet = collect(alert)
        self.assertNotIn("must-not-copy", json.dumps(packet))
        self.assertEqual(packet["resources"]["workflows"][0]["status"]["phase"], "Failed")
        packet["incident_id"] = "infra-20261114T200000Z-12345678"
        with patch("infra_context.Path") as path_type:
            path_type.return_value.read_text.return_value = json.dumps(packet)
            self.assertEqual(validate_packet("packet.json", packet["incident_id"]), packet)
        self.assertIs(packet_validator(packet["incident_id"]), validate_packet)


if __name__ == "__main__":
    unittest.main()
