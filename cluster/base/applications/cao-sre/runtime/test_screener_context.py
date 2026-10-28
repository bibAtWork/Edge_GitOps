"""Offline checks for signal selection and repeat suppression."""

import datetime as dt
import unittest
from unittest.mock import patch

from gate import decision
from screener_context import detect_signals, redact, validate_handoff


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


if __name__ == "__main__":
    unittest.main()
