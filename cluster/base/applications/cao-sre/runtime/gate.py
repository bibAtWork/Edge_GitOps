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
from feedback import observe_recovery, prior_lessons

STATE = Path(os.environ.get("CAO_GATE_STATE", "/home/cao/monitor-state.json"))
STATUS = Path(os.environ.get("CAO_GATE_STATUS", "/home/cao/gate-status.json"))
COOLDOWN_SECONDS = 60 * 60


def decision(signals, state, timestamp):
    present = {s["key"] for s in signals}
    state = {key: value for key, value in state.items() if key in present}
    selected = None
    for signal in signals:
        key = signal["key"]
        entry = state.setdefault(key, {"seen": 0, "sent_at": 0})
        entry["seen"] += 1
        if entry["seen"] >= 2 and timestamp - entry["sent_at"] >= COOLDOWN_SECONDS and selected is None:
            selected = signal
            entry["sent_at"] = timestamp
    return selected, state


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, STATE)


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
        observe_recovery({signal["key"] for signal in signals})
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        selected, state = decision(signals, state, timestamp)
        if selected:
            digest = hashlib.sha256(selected["key"].encode()).hexdigest()[:8]
            incident_id = f"screener-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(timestamp))}-{digest}"
            path = write_packet(snapshot, incident_id, selected,
                                prior=prior_lessons(selected["key"]))
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
