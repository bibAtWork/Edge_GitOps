#!/usr/bin/env python3
"""Fail closed before a PR worker receives an RCA handoff."""

import json
import re
import sys
from pathlib import Path


def validate(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    incident_id = doc.get("incident_id", "")
    if not re.fullmatch(r"(?:screener|manual)-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6,8}", incident_id):
        raise ValueError("Invalid incident ID")
    if doc.get("schema_version") != 1 or doc.get("stage") != "rca":
        raise ValueError("A version 1 RCA handoff is required")
    status = doc.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("RCA status is required")
    if status in ("healthy", "resolved", "uncertain"):
        raise ValueError("RCA has no confirmed active problem for a PR")
    if doc.get("pr_candidate") is not True or not isinstance(doc.get("proposed_change"), str):
        raise ValueError("RCA does not propose a code change")
    if len(doc["proposed_change"].strip()) < 20:
        raise ValueError("Proposed change is too vague")
    if not any(isinstance(h, dict) and h.get("evidence") for h in doc.get("hypotheses", [])):
        raise ValueError("No supported causal hypothesis")
    if not doc.get("report_path") or not doc.get("summary"):
        raise ValueError("RCA report reference and summary are required")
    if not Path("/handoff/rca-report.md").is_file():
        raise ValueError("RCA report is not mounted")
    return incident_id


if __name__ == "__main__":
    try:
        print(validate(sys.argv[1]))
    except (IndexError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"PR gate rejected handoff: {exc}", file=sys.stderr)
        sys.exit(1)
