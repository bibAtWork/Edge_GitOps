#!/usr/bin/env python3
"""Receive allowlisted Grafana alerts for the read-only SRE crews."""

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATE = Path(os.environ.get("CAO_ALERT_STATE", "/home/cao/alert-state.json"))
TOKEN = os.environ.get("CAO_ALERT_WEBHOOK_TOKEN", "")
SCREENER = frozenset(("RealEstateScreenerUnavailable", "RealEstateScreenerFunctionalityFailed"))
INFRA = frozenset(("PlatformCanarySyntheticFailed", "RecoveryPointStale",
                   "RecoveryOffsiteStale", "LonghornDiskNotReady"))
ALLOWED = SCREENER | INFRA
MAX_BYTES = 16_384
MAX_AGE = 8 * 60 * 60  # The contact point repeats firing notifications every six hours.


def active_alerts(state, timestamp):
    if not isinstance(state, dict):
        return {}
    return {key: value for key, value in state.items()
            if isinstance(value, dict) and value.get("alertname", key) in ALLOWED
            and isinstance(value.get("updated_at"), int)
            and 0 <= timestamp - value["updated_at"] <= MAX_AGE}


def apply_notification(state, payload, timestamp):
    """Keep only allowlisted state; never persist untrusted annotations or URLs."""
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(alerts, list) or len(alerts) > 10:
        raise ValueError("Expected at most ten alerts")
    current = active_alerts(state, timestamp)
    for alert in alerts:
        if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
            raise ValueError("Invalid alert")
        name = alert["labels"].get("alertname")
        if name not in ALLOWED:
            continue
        fingerprint = alert.get("fingerprint", "")
        if not isinstance(fingerprint, str) or len(fingerprint) > 128:
            raise ValueError("Invalid fingerprint")
        if name in INFRA and not fingerprint:
            raise ValueError("Missing infrastructure fingerprint")
        labels = alert["labels"]
        application = labels.get("application", "")
        dataset = labels.get("dataset", "")
        if any(not isinstance(value, str) or len(value) > 80 or
               not all(c.isalnum() or c in "._-" for c in value)
               for value in (application, dataset)):
            raise ValueError("Invalid resource labels")
        if name.startswith("Recovery") and (not application or not dataset):
            # No target identity means the crew cannot establish what is stale.
            continue
        key = name if name in SCREENER else f"{name}:{fingerprint}"
        if alert.get("status") == "firing":
            current[key] = {"fingerprint": fingerprint, "updated_at": timestamp,
                            "alertname": name, "application": application,
                            "dataset": dataset}
        elif alert.get("status") == "resolved":
            # A delayed resolved notification must not clear a newer firing.
            if current.get(key, {}).get("fingerprint") == fingerprint:
                current.pop(key, None)
        else:
            raise ValueError("Invalid alert status")
    return current


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, STATE)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/alerts":
            self.send_error(404)
            return
        if not TOKEN:
            self.send_error(503)
            return
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {TOKEN}"):
            self.send_error(401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BYTES:
                raise ValueError("Invalid body length")
            payload = json.loads(self.rfile.read(length))
            save_state(apply_notification(read_state(), payload, int(time.time())))
        except (ValueError, UnicodeDecodeError):
            self.send_error(400)
            return
        except OSError:
            self.send_error(503)
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format, *_args):
        # Grafana payloads and credentials must never enter pod logs.
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9891), Handler).serve_forever()
