#!/bin/sh
set -eu

mkdir -p /home/cao/reports /home/cao/handoffs /home/cao/incidents
cao install /config/screener-triage.md
cao install /config/screener-rca.md

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
cao schedule remove screener-monitor >/dev/null 2>&1 || true
cao schedule add /config/screener-monitor.md
wait "$server_pid"
