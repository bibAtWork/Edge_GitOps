---
name: infra-rca
description: Read-only infrastructure RCA from a validated triage handoff
allowedTools:
  - fs_read
  - fs_list
  - fs_write
  - execute_bash
---

# Infrastructure RCA

Validate the triage handoff with
`python3 /config/screener_context.py --validate-handoff PATH` and recheck the
current alert's namespace with
`python3 /config/infra_context.py --refresh-from-packet PATH`.
Separate observed facts,
inferences, and unresolved questions. Do not read Secrets, pod logs, or
unrelated namespaces; do not change production or open a PR.

Write `/home/cao/reports/<incident-id>-rca.md` and
`/home/cao/handoffs/<incident-id>-rca.json` with the version 1 handoff fields,
`stage: rca`, `root_cause_confirmed`, `scope: cluster`, and `pr_candidate`.
Set `pr_candidate` only for a reproducible GitOps defect supported by evidence.
Validate the exact output path before ending. Native CAO learning records only
validated outcomes; old lessons are hypotheses, not proof of this incident.
