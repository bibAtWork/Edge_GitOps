#!/usr/bin/env python3
"""Reconcile scheduled SRE crew runs using CAO's durable workflow journal."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from screener_context import validate_packet

QUEUE = Path(os.environ.get("CAO_CREW_QUEUE", "/home/cao/crew-queue"))
WORKFLOW = "/home/cao/.aws/cli-agent-orchestrator/workflows/screener_crew.py"
MAX_ATTEMPTS = 3
TRANSIENT_DELAY = 5 * 60
QUOTA_DELAY = 5 * 60 * 60
RUN_DEADLINE = 45 * 60
INCIDENT_PATTERN = re.compile(r"screener-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\Z")


def save(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def enqueue(incident_id, packet_path):
    if not INCIDENT_PATTERN.fullmatch(incident_id):
        raise ValueError("Invalid scheduled incident ID")
    validate_packet(packet_path, incident_id, max_age_seconds=900)
    path = QUEUE / f"{incident_id}.json"
    if path.exists():
        return path
    save(path, {"incident_id": incident_id, "packet_path": str(packet_path),
                "state": "queued", "attempts": 0, "run_id": None,
                "next_retry_at": 0, "created_at": int(time.time())})
    return path


def command(*args, timeout=30):
    return subprocess.run(("cao", *args), text=True, capture_output=True,
                          timeout=timeout, check=False)


def run_status(run_id):
    result = command("workflow", "status", run_id, "--json")
    if result.returncode:
        return None
    return json.loads(result.stdout)


def failure_kind(status):
    details = json.dumps(status, ensure_ascii=True).lower()
    if any(marker in details for marker in ("rate limit", "usage limit", "five-hour",
                                           "5h limit", "quota exceeded", "resets at")):
        return "quota"
    if any(marker in details for marker in ("connection reset", "disconnected", "websocket",
                                           "transport error", "network error", "timed out")):
        return "connection"
    return None


def start(record):
    incident_id = record["incident_id"]
    packet_path = Path(record["packet_path"])
    validate_packet(packet_path, incident_id, max_age_seconds=900)
    run_id = f"crew-{incident_id}"
    result = command("workflow", "run", WORKFLOW, "--run-id", run_id,
                     "--input", f"incident_id={incident_id}",
                     "--input", f"packet_path={packet_path}", "--detach", "--json")
    if result.returncode and run_status(run_id) is None:
        raise RuntimeError(f"Workflow submission failed: {result.stderr[-500:]}")
    record.update({"run_id": run_id, "packet_path": str(packet_path),
                   "state": "running", "attempts": 1,
                   "started_at": int(time.time()), "next_retry_at": 0})


def resume(record):
    if record["attempts"] >= MAX_ATTEMPTS:
        record.update({"state": "needs_review", "reason": "retry_limit"})
        return
    log_path = QUEUE / f"{record['incident_id']}.resume.log"
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(("cao", "workflow", "resume", record["run_id"], "--json"),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    record.update({"state": "running", "attempts": record["attempts"] + 1,
                   "started_at": int(time.time()), "next_retry_at": 0})


def reconcile(record):
    current = int(time.time())
    if record["state"] in ("completed", "needs_review"):
        return
    if record["state"] == "queued":
        start(record)
        return
    if record["state"] == "waiting" and current >= record.get("next_retry_at", 0):
        resume(record)
        return
    if record["state"] != "running":
        return
    status = run_status(record["run_id"])
    if status is None:
        record.update({"state": "needs_review", "reason": "journal_unavailable"})
        return
    state = status.get("state")
    if state == "completed":
        record["state"] = "completed"
        record["completed_at"] = current
    elif state in ("failed", "cancelled"):
        kind = failure_kind(status)
        if kind and record["attempts"] < MAX_ATTEMPTS:
            record.update({"state": "waiting", "reason": kind,
                           "next_retry_at": current + (QUOTA_DELAY if kind == "quota" else TRANSIENT_DELAY)})
        else:
            record.update({"state": "needs_review", "reason": kind or state})
    elif current - record.get("started_at", current) > RUN_DEADLINE:
        # An unknown live outcome must not spawn a duplicate writer.
        record.update({"state": "needs_review", "reason": "running_past_deadline"})


def main():
    import fcntl  # Linux pod only; helpers remain testable on Windows.
    QUEUE.mkdir(parents=True, exist_ok=True)
    with (QUEUE / ".controller.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"execute": False, "output": {}}))
            return
        paths = sorted(QUEUE.glob("screener-*.json"))
        active = 0
        for path in paths:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("state") == "running":
                reconcile(record)
                save(path, record)
            if record.get("state") == "running":
                active += 1
        for path in paths:
            if active:
                break
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("state") not in ("queued", "waiting"):
                continue
            try:
                reconcile(record)
            except Exception as exc:
                record.update({"state": "needs_review", "reason": type(exc).__name__})
                print(f"{path.name}: {exc}", file=sys.stderr)
            save(path, record)
            if record.get("state") == "running":
                active = 1
    print(json.dumps({"execute": False, "output": {}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"crew reconciliation failed: {exc}", file=sys.stderr)
        sys.exit(1)
