"""Offline checks for signal selection and repeat suppression."""

import datetime as dt
import io
import json
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from threading import Thread
from unittest.mock import MagicMock, patch

from alert_webhook import Handler, active_alerts, apply_notification
from crew_controller import failure_kind, reconcile
from gate import decision, with_alerts
from metrics import snapshot as metrics_snapshot
from native_learning import report_outcome
from recovery import complete_observation, observe, register, verified_prior_causes
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

    def test_persistent_failure_creates_one_incident(self):
        signal = [{"key": "web-functionality", "detail": "HTTP 503"}]
        first, state = decision(signal, {}, 10000)
        second, state = decision(signal, state, 10600)
        third, state = decision(signal, state, 11200)
        retrigger, _ = decision(signal, state, 14201)
        self.assertIsNone(first)
        self.assertEqual(second, signal[0])
        self.assertIsNone(third)
        self.assertIsNone(retrigger)

    def test_grafana_alert_starts_once_and_rearms_after_resolution(self):
        alert = "RealEstateScreenerFunctionalityFailed"
        signal = with_alerts([{"key": "web-functionality", "detail": "HTTP 503"}], {alert: {}})
        first, state = decision(signal, {}, 10000)
        repeated, state = decision(signal, state, 15000)
        self.assertEqual(first["confirmed_alert"], alert)
        self.assertIsNone(repeated)
        _, state = decision([], state, 15100)
        transient, state = decision(signal, state, 15150)
        self.assertIsNone(transient)
        _, state = decision([], state, 15160)
        _, state = decision([], state, 15170)
        new_episode, _ = decision(signal, state, 15200)
        self.assertEqual(new_episode["key"], "web-functionality")

    def test_unavailable_alert_groups_correlated_health_failure(self):
        signals = [{"key": "web-unavailable", "detail": "no replicas"},
                   {"key": "web-functionality", "detail": "HTTP 503"},
                   {"key": "database-unready", "detail": "no ready instance"}]
        enriched = with_alerts(signals, {
            "RealEstateScreenerUnavailable": {},
            "RealEstateScreenerFunctionalityFailed": {},
        })
        self.assertEqual([s["key"] for s in enriched], ["web-unavailable"])
        self.assertEqual(enriched[0]["confirmed_alert"], "RealEstateScreenerUnavailable")

    def test_alert_webhook_allowlist_resolution_and_expiry(self):
        payload = {"alerts": [
            {"status": "firing", "fingerprint": "one", "labels": {
                "alertname": "RealEstateScreenerUnavailable",
                "url": "untrusted"}},
            {"status": "firing", "labels": {"alertname": "FalcoCriticalEvent"}},
        ]}
        state = apply_notification({}, payload, 10000)
        self.assertEqual(set(state), {"RealEstateScreenerUnavailable"})
        self.assertNotIn("untrusted", json.dumps(state))
        self.assertEqual(active_alerts(state, 10000 + 8 * 60 * 60 + 1), {})
        stale_resolution = {"alerts": [{"status": "resolved", "fingerprint": "old",
                                         "labels": {"alertname": "RealEstateScreenerUnavailable"}}]}
        self.assertEqual(apply_notification(state, stale_resolution, 10100), state)
        payload["alerts"][0]["status"] = "resolved"
        self.assertEqual(apply_notification(state, payload, 10200), {})

    def test_alert_webhook_requires_bearer_and_rejects_malformed_body(self):
        with patch("alert_webhook.TOKEN", "test-only-token"), patch(
            "alert_webhook.read_state", return_value={}
        ), patch("alert_webhook.save_state") as saved:
            server = HTTPServer(("127.0.0.1", 0), Handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/alerts"
                body = json.dumps({"alerts": [{"status": "firing", "fingerprint": "one",
                    "labels": {"alertname": "RealEstateScreenerUnavailable"}}]}).encode()
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=2)
                self.assertEqual(unauthorized.exception.code, 401)
                unauthorized.exception.close()
                saved.assert_not_called()
                request = urllib.request.Request(url, data=body,
                    headers={"Authorization": "Bearer test-only-token"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 204)
                self.assertIn("RealEstateScreenerUnavailable", saved.call_args.args[0])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

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
        _, state = decision([], state, 11300)
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

    def test_stale_quota_retry_is_quarantined_without_starting_agent(self):
        record = {"state": "waiting", "run_id": "crew-test", "attempts": 1,
                  "incident_id": "screener-20261031T185000Z-aabbccdd",
                  "packet_path": "/home/cao/incidents/old.json", "next_retry_at": 100}
        with patch("crew_controller.time.time", return_value=101), patch(
            "crew_controller.validate_packet", side_effect=ValueError("Packet is stale")
        ), patch("crew_controller.subprocess.Popen") as spawned:
            reconcile(record)
        self.assertEqual((record["state"], record["reason"]),
                         ("needs_review", "stale_incident"))
        spawned.assert_not_called()

    def test_runtime_limit_cancels_live_workflow(self):
        record = {"state": "running", "run_id": "crew-test", "attempts": 1,
                  "first_started_at": 100, "started_at": 900}
        with patch("crew_controller.time.time", return_value=1300), patch(
            "crew_controller.run_status", return_value={"state": "running"}
        ), patch("crew_controller.command", return_value=MagicMock(returncode=0)) as command:
            reconcile(record)
        command.assert_called_once_with("workflow", "cancel", "crew-test", timeout=30)
        self.assertEqual((record["state"], record["reason"]),
                         ("needs_review", "runtime_limit"))

    def test_runtime_limit_prevents_late_retry(self):
        record = {"state": "waiting", "run_id": "crew-test", "attempts": 1,
                  "first_started_at": 100, "started_at": 900, "next_retry_at": 1200}
        with patch("crew_controller.time.time", return_value=1300), patch(
            "crew_controller.resume"
        ) as resume:
            reconcile(record)
        resume.assert_not_called()
        self.assertEqual((record["state"], record["reason"]),
                         ("needs_review", "runtime_limit"))

    def test_missing_journal_does_not_release_live_run(self):
        record = {"state": "running", "run_id": "crew-test", "attempts": 1,
                  "first_started_at": 100, "started_at": 100}
        with patch("crew_controller.time.time", return_value=200), patch(
            "crew_controller.run_status", return_value=None
        ):
            reconcile(record)
        self.assertEqual((record["state"], record["reason"]),
                         ("running", "journal_unavailable"))

    def test_native_outcome_is_redacted_and_replay_does_not_duplicate(self):
        incident = "screener-20261031T185000Z-aabbccdd"
        handoff = {"summary": "Auth failure token=sensitive", "root_cause_confirmed": True}
        with patch("native_learning.urllib.request.urlopen", side_effect=[
            io.BytesIO(b'{"outcomes":[]}'), io.BytesIO(b'{"id":1}'),
        ]) as opened:
            report_outcome(incident, handoff)
        posted = json.loads(opened.call_args_list[1].args[0].data)
        self.assertEqual(posted["session_name"], incident)
        self.assertEqual(posted["agent_profile"], "screener-rca")
        self.assertTrue(posted["success"])
        self.assertNotIn("sensitive", posted["friction_notes"])
        with patch("native_learning.urllib.request.urlopen", return_value=io.BytesIO(
            json.dumps({"outcomes": [{"task_label": f"{incident}: rca"}]}).encode()
        )) as opened:
            report_outcome(incident, handoff)
        self.assertEqual(opened.call_count, 1)

    def test_prior_cause_requires_confirmed_rca_and_two_healthy_observations(self):
        incident = "screener-20261031T185000Z-aabbccdd"
        packet = {"signal": {"key": "database-unready"}}
        handoff = {"incident_id": incident, "stage": "rca",
                   "root_cause_confirmed": True,
                   "hypotheses": [{"cause": "database restart", "evidence": "ready replicas recovered"}]}
        with patch("recovery.STATE") as state_dir, patch("recovery.HANDOFFS") as handoffs, patch(
            "recovery.validate_packet", return_value=packet
        ), patch("recovery.validate_handoff", return_value=handoff), patch(
            "recovery.save"
        ) as saved:
            state_path = state_dir.__truediv__.return_value
            state_path.exists.return_value = False
            register("packet", "handoff", incident)
            record = saved.call_args.args[1]
            state_dir.glob.return_value = [state_path]
            state_path.read_text.side_effect = lambda **_: json.dumps(record)
            saved.side_effect = lambda _path, value: record.update(value)
            self.assertEqual(verified_prior_causes("database-unready"), [])
            observe({"database-unready"})
            observe(set())
            self.assertEqual(verified_prior_causes("database-unready"), [])
            observe(set())
            self.assertEqual(verified_prior_causes("database-unready")[0]["cause"],
                             "database restart")
            self.assertTrue(handoffs.__truediv__.called)

    def test_recovery_does_not_count_blind_application_observations(self):
        self.assertFalse(complete_observation({"health": {"http_status": 200},
                                                "ingestion": {"http_status": 503}}))
        self.assertFalse(complete_observation({"health": {"observer_error": "timeout"},
                                                "ingestion": {"http_status": 200}}))
        self.assertTrue(complete_observation({"health": {"http_status": 200},
                                               "ingestion": {"http_status": 200}}))

    def test_metrics_do_not_expose_incident_ids(self):
        with patch("metrics.QUEUE") as queue, patch("metrics.GATE_STATUS") as gate_status:
            record = queue.glob.return_value = [MagicMock()]
            record[0].read_text.return_value = json.dumps(
                {"state": "needs_review", "incident_id": "private-incident"})
            gate_status.read_text.side_effect = OSError("absent")
            text = metrics_snapshot()
            self.assertIn('cao_sre_crew_records{state="needs_review"} 1', text)
            self.assertNotIn("private-incident", text)

    def test_pr_gate_rejects_healthy_rca(self):
        handoff = {"schema_version": 1, "stage": "rca",
                   "incident_id": "manual-20261028T210907Z-aafeb5",
                   "summary": "No active incident", "report_path": "/reports/rca.md",
                   "hypotheses": [{"evidence": "healthy checks"}],
                   "pr_candidate": False}
        with patch("pr_gate.Path") as mocked_path:
            mocked_path.return_value.read_text.return_value = json.dumps(handoff)
            with self.assertRaisesRegex(ValueError, "does not propose"):
                validate_pr_candidate("rca.json")


if __name__ == "__main__":
    unittest.main()
