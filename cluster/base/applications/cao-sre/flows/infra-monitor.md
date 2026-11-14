---
name: infra-monitor
schedule: "3-59/10 * * * *"
agent_profile: infra-triage-codex
provider: codex
script: /config/infra_gate.py
---

The deterministic infrastructure gate enqueues only allowlisted firing alerts.
This flow does not directly launch an agent.
