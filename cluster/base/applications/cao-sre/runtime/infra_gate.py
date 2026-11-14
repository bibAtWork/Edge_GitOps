#!/usr/bin/env python3
"""Enqueue one read-only infrastructure investigation per firing episode."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from alert_webhook import INFRA, active_alerts, read_state
from crew_controller import enqueue
from gate import decision
from infra_context import collect, write_packet

STATE = Path(os.environ.get("CAO_INFRA_GATE_STATE", "/home/cao/infra-gate-state.json"))
STATUS = Path(os.environ.get("CAO_INFRA_GATE_STATUS", "/home/cao/infra-gate-status.json"))


def main():
    timestamp = int(time.time())
    status = {"last_attempt": timestamp}
    try:
        firing = active_alerts(read_state(), timestamp)
        signals = [{"key": key, "confirmed_alert": value["alertname"], "alert": value}
                   for key, value in sorted(firing.items())
                   if value.get("alertname") in INFRA]
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        selected, state = decision(signals, state, timestamp)
        if selected:
            digest = hashlib.sha256(selected["key"].encode()).hexdigest()[:8]
            incident_id = f"infra-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(timestamp))}-{digest}"
            packet = write_packet(collect(selected["alert"]), incident_id)
            enqueue(incident_id, packet)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, STATE)
        status["last_success"] = timestamp
    except Exception as exc:
        print(f"infra gate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    except (OSError, ValueError):
        previous = {}
    previous.update(status)
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(previous), encoding="utf-8")
    os.replace(temporary, STATUS)
    print(json.dumps({"execute": False, "output": {}}))


if __name__ == "__main__":
    main()
