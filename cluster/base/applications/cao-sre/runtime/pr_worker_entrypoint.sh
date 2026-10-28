#!/bin/sh
set -eu

incident_id=$(python3 /config/pr_gate.py /handoff/rca.json)
mkdir -p /home/cao /workspace
export GH_PROMPT_DISABLED=1
gh repo clone bibAtWork/Edge_GitOps /workspace/repo -- --branch ops/talos_linux --single-branch
cd /workspace/repo
git config user.name "cao-sre-pr-worker"
git config user.email "cao-sre-pr-worker@users.noreply.github.com"
cao install /config/screener-pr-draft.md
cao-server --host 127.0.0.1 --port 9889 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
attempt=0
until cao profile list >/dev/null 2>&1 && cao session list >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "PR worker CAO server did not become ready" >&2
    exit 1
  fi
  sleep 2
done
cao launch --agents screener-pr-draft --provider opencode_cli \
  --session-name "pr-$incident_id" --headless --auto-approve --async \
  --working-directory /workspace/repo \
  "Review /handoff/rca.json for incident $incident_id. Follow your profile. Open only a draft PR if code evidence supports the proposed change."
deadline=$(($(date +%s) + 1800))
while [ "$(date +%s)" -lt "$deadline" ]; do
  status=$(cao session status "cao-pr-$incident_id" 2>&1 || true)
  case "$status" in
    *"Status:   completed"*) printf '%s\n' "$status"; exit 0 ;;
    *"Status:   error"*|*"Status:   failed"*) printf '%s\n' "$status" >&2; exit 1 ;;
  esac
  sleep 10
done
echo "PR worker timed out; inspect CAO session before retrying" >&2
exit 124
