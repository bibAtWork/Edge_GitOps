"""Verify RCA causes against later complete deterministic observations.

This stores only recovery counters. CAO owns outcomes and agent lessons; the
evidence for a verified prior cause is read from its original RCA handoff.
"""

import json
import os
import re
from pathlib import Path

from screener_context import redact, validate_handoff, validate_packet

STATE = Path(os.environ.get("CAO_RECOVERY_DIR", "/home/cao/recovery"))
HANDOFFS = Path("/home/cao/handoffs")
INCIDENT = re.compile(r"screener-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\Z")


def complete_observation(snapshot):
    """Do not count an absent signal while either application probe is blind."""
    return (snapshot.get("health", {}).get("http_status") == 200
            and snapshot.get("ingestion", {}).get("http_status") == 200)


def save(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def register(packet_path, handoff_path, incident_id):
    """Track only a supported cause; registration is safe on workflow replay."""
    if not INCIDENT.fullmatch(incident_id):
        raise ValueError("Invalid incident ID")
    packet = validate_packet(packet_path, incident_id)
    handoff = validate_handoff(handoff_path)
    if handoff["incident_id"] != incident_id or handoff["stage"] != "rca":
        raise ValueError("RCA handoff does not match the incident")
    supported = any(isinstance(h, dict) and h.get("cause") and h.get("evidence")
                    for h in handoff["hypotheses"])
    if handoff.get("root_cause_confirmed") is not True or not supported:
        return None
    signal_key = packet.get("signal", {}).get("key")
    if not isinstance(signal_key, str) or not signal_key:
        raise ValueError("Incident has no signal key")
    path = STATE / f"{incident_id}.json"
    if not path.exists():
        save(path, {"incident_id": incident_id, "signal_key": signal_key,
                    "healthy_observations": 0, "recovered": False})
    return path


def observe(active_keys):
    """Call after a complete gate observation, never after a collection error."""
    for path in STATE.glob("screener-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("recovered"):
            continue
        count = 0 if record.get("signal_key") in active_keys else min(
            2, int(record.get("healthy_observations") or 0) + 1)
        record["healthy_observations"] = count
        record["recovered"] = count >= 2
        save(path, record)


def verified_prior_causes(signal_key):
    """Return up to three causes whose symptom stayed absent twice afterward."""
    causes = []
    for path in STATE.glob("screener-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            incident_id = record.get("incident_id", "")
            if (not record.get("recovered") or record.get("signal_key") != signal_key
                    or not INCIDENT.fullmatch(incident_id)):
                continue
            handoff = validate_handoff(HANDOFFS / f"{incident_id}-rca.json")
            if (handoff["incident_id"] != incident_id or handoff["stage"] != "rca"
                    or handoff.get("root_cause_confirmed") is not True):
                continue
            hypothesis = next((h for h in handoff["hypotheses"]
                               if isinstance(h, dict) and h.get("cause") and h.get("evidence")), None)
            if hypothesis:
                causes.append({"incident_id": incident_id,
                               "cause": redact(str(hypothesis["cause"]))[:250],
                               "evidence": redact(str(hypothesis["evidence"]))[:300]})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(causes, key=lambda item: item["incident_id"], reverse=True)[:3]
