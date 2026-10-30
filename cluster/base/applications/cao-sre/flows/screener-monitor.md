---
name: screener-monitor
schedule: "*/10 * * * *"
agent_profile: screener-triage-codex
provider: codex
script: /config/gate.py
---

The deterministic gate enqueues confirmed symptoms for the crew workflow.
This flow does not directly launch an agent.
