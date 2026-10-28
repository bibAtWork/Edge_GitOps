---
name: screener-monitor
schedule: "*/10 * * * *"
agent_profile: screener-triage
provider: opencode_cli
script: /config/gate.py
---

Investigate incident [[incident_id]] for real-estate-screener. Read the
timestamped incident packet at [[packet_path]]. Signal: [[signal]]. Produce
the triage report and version 1 handoff specified by your profile.
