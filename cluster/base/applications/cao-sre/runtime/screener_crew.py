"""Durable CAO triage -> RCA workflow. The PR stage requires an operator."""

import re
import sys
from pathlib import Path

from cao_workflow import emit_output, get_inputs, step

sys.path.insert(0, "/config")
from screener_context import validate_handoff, validate_packet

INPUTS = {
    "incident_id": {"type": "string", "required": True},
    "packet_path": {"type": "string", "required": True},
}

ROOT = Path("/home/cao")


def checked_handoff(path, incident_id, stage):
    document = validate_handoff(path)
    if document["incident_id"] != incident_id or document["stage"] != stage:
        raise ValueError(f"{stage} handoff has wrong incident or stage")
    report = Path(document["report_path"]).resolve()
    if not report.is_relative_to(ROOT / "reports") or not report.is_file():
        raise ValueError(f"{stage} report is missing or outside the reports directory")
    return document


inputs = get_inputs()
incident_id = inputs["incident_id"]
if not re.fullmatch(r"screener-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", incident_id):
    raise ValueError("Scheduled incident ID is invalid")
packet_path = inputs["packet_path"]
validate_packet(packet_path, incident_id)

triage_path = ROOT / "handoffs" / f"{incident_id}.json"
rca_path = ROOT / "handoffs" / f"{incident_id}-rca.json"

step(
    "opencode_cli",
    "screener-triage",
    f"Investigate incident {incident_id}. Read the packet at {packet_path}. "
    f"Write the report and triage handoff for this exact incident ID. "
    "Do not modify production or open a PR.",
    recovery="idempotent",
    step_id="triage",
    timeout=1800,
)
checked_handoff(triage_path, incident_id, "triage")

step(
    "codex",
    "screener-rca-codex",
    f"Validate and investigate triage handoff {triage_path}. Recheck current state. "
    f"Write the RCA report and RCA handoff to {rca_path}. "
    "If evidence does not establish a root cause, say so. "
    "Do not modify production or open a PR.",
    recovery="idempotent",
    step_id="rca",
    timeout=1800,
)
try:
    checked_handoff(rca_path, incident_id, "rca")
except (OSError, ValueError) as exc:
    step(
        "codex",
        "screener-rca-codex",
        f"The previous RCA turn ended without valid artifacts: {exc}. "
        f"Create /home/cao/reports/{incident_id}-rca.md and {rca_path} now. "
        "Write the version 1 JSON handoff with stage rca, status, summary, "
        "hypotheses, recommended_checks, report_path, and pr_candidate=false "
        "unless evidence supports a code change. Run the handoff validator "
        "and check both files exist before ending your turn.",
        recovery="idempotent",
        step_id="rca-artifact-repair",
        timeout=1800,
    )
    checked_handoff(rca_path, incident_id, "rca")
emit_output({"incident_id": incident_id, "triage_handoff": str(triage_path),
             "rca_handoff": str(rca_path), "pr_stage": "requires_operator"})
