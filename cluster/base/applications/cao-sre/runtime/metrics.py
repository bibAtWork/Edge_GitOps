#!/usr/bin/env python3
"""Low-cardinality metrics for the deterministic SRE gate and durable queue."""

import json
import os
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

QUEUE = Path(os.environ.get("CAO_CREW_QUEUE", "/home/cao/crew-queue"))
GATE_STATUS = Path(os.environ.get("CAO_GATE_STATUS", "/home/cao/gate-status.json"))
STATES = ("queued", "running", "waiting", "completed", "needs_review")


def snapshot():
    counts = Counter()
    latest_completion = 0
    for path in QUEUE.glob("screener-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        state = record.get("state")
        if state in STATES:
            counts[state] += 1
        if state == "completed":
            latest_completion = max(latest_completion, int(record.get("completed_at") or 0))
    try:
        gate = json.loads(GATE_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        gate = {}
    lines = [
        "# HELP cao_sre_gate_last_success_timestamp_seconds Last successful gate observation.",
        "# TYPE cao_sre_gate_last_success_timestamp_seconds gauge",
        f"cao_sre_gate_last_success_timestamp_seconds {int(gate.get('last_success') or 0)}",
        "# HELP cao_sre_gate_last_attempt_timestamp_seconds Last gate observation attempt.",
        "# TYPE cao_sre_gate_last_attempt_timestamp_seconds gauge",
        f"cao_sre_gate_last_attempt_timestamp_seconds {int(gate.get('last_attempt') or 0)}",
        "# HELP cao_sre_crew_records Number of durable incident records by state.",
        "# TYPE cao_sre_crew_records gauge",
    ]
    lines.extend(f'cao_sre_crew_records{{state="{state}"}} {counts[state]}' for state in STATES)
    lines.extend((
        "# HELP cao_sre_crew_last_completion_timestamp_seconds Last successful crew completion.",
        "# TYPE cao_sre_crew_last_completion_timestamp_seconds gauge",
        f"cao_sre_crew_last_completion_timestamp_seconds {latest_completion}",
    ))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        data = snapshot().encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format, *_args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9890), Handler).serve_forever()
