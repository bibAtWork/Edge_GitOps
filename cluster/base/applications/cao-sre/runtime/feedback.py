#!/usr/bin/env python3
"""Bounded incident lessons; confirmation requires later deterministic recovery."""

import json
import os
from pathlib import Path

from screener_context import redact, validate_handoff, validate_packet

LESSONS = Path(os.environ.get("CAO_LESSONS_DIR", "/home/cao/lessons"))


def short(value, length):
    return redact(value).replace("\n", " ")[:length]


def save(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def record_candidate(packet_path, handoff_path, incident_id):
    packet = validate_packet(packet_path, incident_id)
    handoff = validate_handoff(handoff_path)
    if handoff["incident_id"] != incident_id or handoff["stage"] != "rca":
        raise ValueError("RCA handoff does not match the incident")
    hypothesis = next((h for h in handoff["hypotheses"]
                       if isinstance(h, dict) and h.get("evidence") and h.get("cause")), {})
    confirmed = handoff.get("root_cause_confirmed") is True and bool(hypothesis)
    scope = handoff.get("scope") if handoff.get("scope") in ("application", "cluster") else "application"
    path = LESSONS / f"{incident_id}.json"
    if path.exists():
        return path
    save(path, {
        "schema_version": 1, "incident_id": incident_id,
        "signal_key": packet["signal"]["key"], "scope": scope,
        "summary": short(handoff["summary"], 350),
        "cause": short(hypothesis.get("cause", ""), 250),
        "evidence": short(hypothesis.get("evidence", ""), 300),
        "recommended_checks": [short(check, 180) for check in handoff["recommended_checks"][:3]],
        "confirmed_cause": confirmed, "recovery_observations": 0,
        "recovered": False, "observation_time": packet["observed_at"],
    })
    return path


def observe_recovery(active_keys):
    """Call only after a successful complete gate observation."""
    for path in LESSONS.glob("screener-*.json"):
        try:
            lesson = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if lesson.get("recovered"):
            continue
        count = 0 if lesson.get("signal_key") in active_keys else min(
            2, int(lesson.get("recovery_observations") or 0) + 1)
        lesson["recovery_observations"] = count
        lesson["recovered"] = count >= 2
        save(path, lesson)


def prior_lessons(signal_key):
    """Return at most three confirmed, recovered summaries for matching symptoms."""
    matches = []
    for path in LESSONS.glob("screener-*.json"):
        try:
            lesson = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (lesson.get("signal_key") == signal_key and lesson.get("confirmed_cause")
                and lesson.get("recovered") and lesson.get("cause") and lesson.get("evidence")):
            matches.append({key: lesson[key] for key in
                            ("incident_id", "scope", "cause", "evidence")})
    return sorted(matches, key=lambda item: item["incident_id"], reverse=True)[:3]
