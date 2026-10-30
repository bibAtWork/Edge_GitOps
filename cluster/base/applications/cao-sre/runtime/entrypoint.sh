#!/bin/sh
set -eu

mkdir -p /home/cao/reports /home/cao/handoffs /home/cao/incidents
mkdir -p /home/cao/crew-queue
mkdir -p /home/cao/.codex
mkdir -p /home/cao/.config/opencode
cp /config/opencode.json /home/cao/.config/opencode/opencode.json
cp /config/codex-sre.config.toml /home/cao/.codex/sre_observer.config.toml
chmod 0600 /home/cao/.codex/sre_observer.config.toml
python3 /config/render_profiles.py
cao install /home/cao/agent-profiles/screener-triage.md
cao install /home/cao/agent-profiles/screener-rca.md
cao install /home/cao/agent-profiles/screener-triage-codex.md
cao install /home/cao/agent-profiles/screener-rca-codex.md
cp /config/screener_crew.py /home/cao/.aws/cli-agent-orchestrator/workflows/screener_crew.py
cao-server --host 0.0.0.0 --port 9889 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true' EXIT INT TERM

attempt=0
until cao schedule list >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo 'CAO server did not become ready' >&2
    exit 1
  fi
  sleep 2
done
cao workflow validate /home/cao/.aws/cli-agent-orchestrator/workflows/screener_crew.py
cao schedule remove screener-monitor >/dev/null 2>&1 || true
cao schedule add /config/screener-monitor.md
cao schedule remove screener-crew-controller >/dev/null 2>&1 || true
cao schedule add /config/screener-crew-controller.md
wait "$server_pid"
