---
name: screener-crew-controller
schedule: "*/5 * * * *"
agent_profile: screener-triage-codex
provider: codex
script: /config/crew_controller.py
---

This flow only runs its deterministic controller script. The script always
returns execute=false; CAO must never launch an agent from this flow.
