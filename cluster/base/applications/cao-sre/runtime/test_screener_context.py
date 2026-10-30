"""Offline checks for signal selection and repeat suppression."""

import datetime as dt
import json
import unittest
from unittest.mock import patch

from crew_controller import failure_kind, reconcile
from gate import decision
from pr_gate import validate as validate_pr_candidate
from screener_context import (NAMESPACE, REQUIRED_OBSERVATIONS, detect_signals,
                              now, redact, validate_handoff, validate_packet)


class ScreenerSignalTests(unittest.TestCase):
    def test_recovered_collector_failure_is_not_a_live_signal(self):
        snapshot = {
            "health": {"http_status": 200},
            "deployments": [{"name": "screener-web", "desired": 1, "available": 1}],
            "cnpg": [{"name": "screener-pg", "ready_instances": 1}],
            "cronjobs": [{"name": "screener-collector", "last_success": "2026-10-27T22:00:00Z"}],
            "jobs": [
                {"name": "old", "cronjob": "screener-collector", "created": "2026-10-27T21:30:00Z", "failed": 1},
                {"name": "new", "cronjob": "screener-collector", "created": "2026-10-27T22:00:00Z", "succeeded": 1},
            ],
        }
        self.assertEqual(detect_signals(snapshot, dt.datetime(2026, 10, 27, 22, 10, tzinfo=dt.timezone.utc)), [])

    def test_persistent_failure_and_cooldown(self):
        signal = [{"key": "web-functionality", "detail": "HTTP 503"}]
        first, state = decision(signal, {}, 10000)
        second, state = decision(signal, state, 10600)
        third, state = decision(signal, state, 11200)
        retrigger, _ = decision(signal, state, 14201)
        self.assertIsNone(first)
        self.assertEqual(second, signal[0])
        self.assertIsNone(third)
        self.assertEqual(retrigger, signal[0])

    def test_quality_incident_requires_confirmed_source_or_exact_plot_mismatch(self):
        snapshot = {
            "health": {"http_status": 200},
            "deployments": [{"name": "screener-web", "desired": 1, "available": 1}],
            "cnpg": [{"name": "screener-pg", "ready_instances": 1}],
            "cronjobs": [], "jobs": [],
            "ingestion": {"http_status": 200, "quality": {
                "searches": [{"name": "Waldenbuch", "provider": "immowelt", "degraded": True,
                              "reason": "two_empty_runs"}],
                "plot": {"labeled": 40, "missing": 2},
            }},
        }
        signals = detect_signals(snapshot)
        self.assertEqual({signal["key"] for signal in signals},
                         {"search-yield:immowelt:Waldenbuch", "plot-extraction-mismatch"})
        snapshot["ingestion"]["quality"]["searches"][0]["degraded"] = False
        snapshot["ingestion"]["quality"]["plot"]["missing"] = 1
        self.assertEqual(detect_signals(snapshot), [])

    def test_missing_quality_endpoint_is_visible_after_activation(self):
        snapshot = {
            "health": {"http_status": 200},
            "ingestion": {"http_status": 404},
            "deployments": [{"name": "screener-web", "desired": 1, "available": 1}],
            "cnpg": [{"name": "screener-pg", "ready_instances": 1}],
            "cronjobs": [], "jobs": [],
        }
        self.assertEqual([s["key"] for s in detect_signals(snapshot)], ["ingestion-quality-unavailable"])

    def test_resolution_clears_previous_incident(self):
        signal = [{"key": "database-unready", "detail": "no ready instance"}]
        _, state = decision(signal, {}, 10000)
        _, state = decision(signal, state, 10600)
        _, state = decision([], state, 11200)
        new_first, state = decision(signal, state, 11800)
        self.assertIsNone(new_first)
        self.assertEqual(state["database-unready"]["seen"], 1)

    def test_redaction(self):
        self.assertNotIn("sensitive", redact("Authorization: Bearer sensitive password=sensitive token=sensitive"))

    def test_handoff_validation_rejects_incomplete_document(self):
        with patch("screener_context.Path") as mocked_path:
            mocked_path.return_value.read_text.return_value = '{"schema_version":1,"incident_id":"test"}'
            with self.assertRaises(ValueError):
                validate_handoff("handoff.json")

    def test_packet_requires_successful_api_receipts(self):
        observed = now().isoformat()
        packet = {"schema_version": 1, "namespace": NAMESPACE, "incident_id": "test",
                  "observed_at": observed,
                  "tool_receipts": [{"method": "GET", "target": target,
                                     "status": 200, "observed_at": observed}
                                    for target in REQUIRED_OBSERVATIONS]}
        with patch("screener_context.Path") as mocked_path:
            mocked_path.return_value.read_text.return_value = json.dumps(packet)
            self.assertEqual(validate_packet("packet.json", "test")["incident_id"], "test")
            packet["tool_receipts"] = packet["tool_receipts"][1:]
            mocked_path.return_value.read_text.return_value = json.dumps(packet)
            with self.assertRaisesRegex(ValueError, "Missing collector receipt"):
                validate_packet("packet.json", "test")

    def test_recovery_classifies_known_failures_only(self):
        self.assertEqual(failure_kind({"error": "usage limit reached"}), "quota")
        self.assertEqual(failure_kind({"state": "failed"}, "Quota exceeded for metric: free_tier_requests"), "quota")
        self.assertEqual(failure_kind({"error": "connection reset"}), "connection")
        self.assertIsNone(failure_kind({"error": "invalid handoff"}))

    def test_unknown_workflow_failure_stops_for_review(self):
        record = {"state": "running", "run_id": "crew-test", "attempts": 1,
                  "started_at": 1}
        with patch("crew_controller.run_status", return_value={"state": "failed",
                                                            "error": "invalid handoff"}):
            reconcile(record)
        self.assertEqual(record["state"], "needs_review")

    def test_quota_failure_waits_for_window_before_resume(self):
        record = {"state": "running", "run_id": "crew-test", "attempts": 1,
                  "started_at": 1}
        with patch("crew_controller.time.time", return_value=100), patch(
            "crew_controller.run_status", return_value={"state": "failed", "error": "usage limit reached"}
        ):
            reconcile(record)
        self.assertEqual(record["state"], "waiting")
        self.assertEqual(record["next_retry_at"], 100 + 5 * 60 * 60)

    def test_pr_gate_rejects_healthy_rca(self):
        handoff = {"schema_version": 1, "stage": "rca",
                   "incident_id": "manual-20261028T210907Z-aafeb5",
                   "status": "healthy",
                   "summary": "No active incident", "report_path": "/reports/rca.md",
                   "hypotheses": [{"evidence": "healthy checks"}],
                   "pr_candidate": False}
        with patch("pr_gate.Path") as mocked_path:
            mocked_path.return_value.read_text.return_value = json.dumps(handoff)
            with self.assertRaisesRegex(ValueError, "no confirmed active problem"):
                validate_pr_candidate("rca.json")

    def test_pr_gate_rejects_missing_status_rca(self):
        handoff = {"schema_version": 1, "stage": "rca",
                   "incident_id": "manual-20261028T210907Z-aafeb5",
                   "summary": "Active incident", "report_path": "/reports/rca.md",
                   "hypotheses": [{"evidence": "failing checks"}],
                   "pr_candidate": True, "proposed_change": "Fix the PR gate missing status validation check"}
        with patch("pr_gate.Path") as mocked_path:
            mocked_path.return_value.read_text.return_value = json.dumps(handoff)
            with self.assertRaisesRegex(ValueError, "status is required"):
                validate_pr_candidate("rca.json")

    def test_pr_gate_rejects_blank_status_rca(self):
        handoff = {"schema_version": 1, "stage": "rca",
                   "incident_id": "manual-20261028T210907Z-aafeb5",
                   "status": "   ",
                   "summary": "Active incident", "report_path": "/reports/rca.md",
                   "hypotheses": [{"evidence": "failing checks"}],
                   "pr_candidate": True, "proposed_change": "Fix the PR gate missing status validation check"}
        with patch("pr_gate.Path") as mocked_path:
            mocked_path.return_value.read_text.return_value = json.dumps(handoff)
            with self.assertRaisesRegex(ValueError, "status is required"):
                validate_pr_candidate("rca.json")


if __name__ == "__main__":
    unittest.main()
