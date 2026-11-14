#!/usr/bin/env python3
"""CAO scheduled-flow gate: stable symptoms only, one incident per tick."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from screener_context import collect, detect_signals, write_packet
from crew_controller import enqueue
from recovery import complete_observation, observe, verified_prior_causes
from alert_webhook import active_alerts, read_state

STATE = Path(os.environ.get("CAO_GATE_STATE", "/home/cao/monitor-state.json"))
STATUS = Path(os.environ.get("CAO_GATE_STATUS", "/home/cao/gate-status.json"))


def decision(signals, state, timestamp):
    present = {s["key"] for s in signals}
    for key in list(state):
        if key not in present:
            state[key]["clear_seen"] = state[key].get("clear_seen", 0) + 1
            if state[key]["clear_seen"] >= 2:
                del state[key]
    selected = None
    for signal in signals:
        key = signal["key"]
        entry = state.setdefault(key, {"seen": 0, "sent_at": 0, "clear_seen": 0})
        entry["clear_seen"] = 0
        entry["seen"] += 1
        required = 1 if signal.get("confirmed_alert") else 2
        if entry["seen"] >= required and not entry["sent_at"] and selected is None:
            selected = signal
            entry["sent_at"] = timestamp
    return selected, state


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, STATE)


def with_alerts(signals, firing):
    """Corroborate Grafana with fresh cluster evidence and avoid duplicate causes."""
    signals = [dict(signal) for signal in signals]
    if "RealEstateScreenerUnavailable" in firing:
        web = next((s for s in signals if s["key"] in ("web-missing", "web-unavailable")), None)
        if web is None:
            # A healthy web Deployment with no scrape is itself worth triage.
            web = {"key": "metric-scrape-unavailable",
                   "detail": "Grafana reports the screener metrics scrape unavailable"}
            signals.insert(0, web)
        web["confirmed_alert"] = "RealEstateScreenerUnavailable"
        # One service outage should not create separate health or DB incidents.
        # Their evidence remains in the packet for the agent to inspect.
        signals = [s for s in signals
                   if s["key"] not in ("web-functionality", "database-missing", "database-unready")]
    elif "RealEstateScreenerFunctionalityFailed" in firing:
        functionality = next((s for s in signals if s["key"] == "web-functionality"), None)
        if functionality is not None:
            functionality["confirmed_alert"] = "RealEstateScreenerFunctionalityFailed"
    return signals


def main():
    timestamp = int(time.time())
    try:
        status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    except (OSError, ValueError):
        status = {}
    status["last_attempt"] = timestamp
    try:
        snapshot = collect()
        signals = detect_signals(snapshot)
        firing = active_alerts(read_state(), timestamp)
        signals = with_alerts(signals, firing)
        if complete_observation(snapshot):
            observe({signal["key"] for signal in signals})
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        selected, state = decision(signals, state, timestamp)
        if selected:
            digest = hashlib.sha256(selected["key"].encode()).hexdigest()[:8]
            incident_id = f"screener-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(timestamp))}-{digest}"
            path = write_packet(snapshot, incident_id, selected,
                                prior=verified_prior_causes(selected["key"]))
            enqueue(incident_id, path)
        save_state(state)
        status["last_success"] = timestamp
        result = {"execute": False, "output": {}}
    except Exception as exc:
        print(f"gate observation failed: {exc}", file=sys.stderr)
        result = {"execute": False, "output": {}}
    finally:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATUS.with_suffix(".tmp")
        temporary.write_text(json.dumps(status), encoding="utf-8")
        os.replace(temporary, STATUS)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
