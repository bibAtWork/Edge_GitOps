"""Small adapter from validated RCA handoffs to CAO's native outcome API."""

import json
import urllib.parse
import urllib.request

from screener_context import redact

OUTCOMES_URL = "http://127.0.0.1:9889/outcomes"


def report_outcome(incident_id, handoff):
    """Record one native CAO outcome, including after a journal replay."""
    label = f"{incident_id}: rca"
    query = urllib.parse.urlencode({"session_name": incident_id, "limit": 50})
    with urllib.request.urlopen(f"{OUTCOMES_URL}?{query}", timeout=5) as response:
        existing = json.load(response)["outcomes"]
    if any(item.get("task_label") == label for item in existing):
        return
    confirmed = handoff.get("root_cause_confirmed") is True
    note = redact(str(handoff.get("summary", ""))).replace("\n", " ")[:600]
    payload = json.dumps({"session_name": incident_id, "task_label": label,
                          "success": confirmed, "workflow_name": "screener_crew",
                          "agent_profile": "screener-rca", "friction_notes": note}).encode("utf-8")
    request = urllib.request.Request(OUTCOMES_URL, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        json.load(response)
