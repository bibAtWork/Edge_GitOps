"""Read-only infrastructure triage and RCA with native CAO feedback."""

import re
import sys
from pathlib import Path

from cao_workflow import emit_output, get_inputs, step

sys.path.insert(0, "/config")
from infra_context import validate_packet
from screener_context import validate_handoff
from native_learning import report_outcome

INPUTS = {"incident_id": {"type": "string", "required": True},
          "packet_path": {"type": "string", "required": True}}
ROOT = Path("/home/cao")


def checked_handoff(path, incident_id, stage):
    document = validate_handoff(path)
    if document["incident_id"] != incident_id or document["stage"] != stage:
        raise ValueError("Infrastructure handoff has wrong incident or stage")
    report = Path(document["report_path"]).resolve()
    if not report.is_relative_to(ROOT / "reports") or not report.is_file():
        raise ValueError("Infrastructure report is missing or outside the reports directory")
    return document


inputs = get_inputs()
incident_id, packet_path = inputs["incident_id"], inputs["packet_path"]
if not re.fullmatch(r"infra-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", incident_id):
    raise ValueError("Invalid infrastructure incident ID")
validate_packet(packet_path, incident_id)
triage = ROOT / "handoffs" / f"{incident_id}.json"
rca_path = ROOT / "handoffs" / f"{incident_id}-rca.json"

step("codex", "infra-triage-codex",
     f"Investigate incident {incident_id}. Read and validate {packet_path}. "
     "Write the read-only triage report and handoff for this incident. "
     "Do not change production or open a PR.",
     recovery="idempotent", step_id="triage", timeout=420)
checked_handoff(triage, incident_id, "triage")
step("opencode_cli", "infra-rca",
     f"Validate and investigate {triage}. Recheck current state with read-only commands. "
     f"Write RCA report and handoff to {rca_path}. Distinguish cause from correlation. "
     "Do not change production or open a PR.",
     recovery="idempotent", step_id="rca", timeout=600)
rca = checked_handoff(rca_path, incident_id, "rca")
report_outcome(incident_id, rca, workflow_name="infra_crew", agent_profile="infra-rca")
step("opencode_cli", "retrospector",
     f"Review native CAO outcomes for session {incident_id}, workflow infra_crew, "
     "agent infra-rca. Store at most one reusable, evidence-based lesson if it "
     "changes future investigation methods; otherwise store none. Do not read "
     "incident files, logs, secrets, or production resources.",
     recovery="idempotent", step_id="retrospect", timeout=120)
emit_output({"incident_id": incident_id, "triage_handoff": str(triage),
             "rca_handoff": str(rca_path), "pr_stage": "requires_operator"})
