"""Bounded Kubernetes evidence for infrastructure alert investigations."""

import datetime as dt
import argparse
import json
import os
import urllib.parse
from pathlib import Path

from alert_webhook import INFRA
from screener_context import kube_get, now, stamp

INCIDENTS = Path(os.environ.get("CAO_INCIDENT_DIR", "/home/cao/incidents"))
TARGETS = {
    "PlatformCanarySyntheticFailed": ("platform-canary", (
        ("/apis/batch/v1/namespaces/platform-canary/jobs?limit=30", "jobs"),
        ("/api/v1/namespaces/platform-canary/pods?limit=30", "pods"))),
    "RecoveryPointStale": ("backup-system", ()),
    "RecoveryOffsiteStale": ("backup-system", ()),
    "LonghornDiskNotReady": ("longhorn-system", (
        ("/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes?limit=30", "nodes"),)),
}


def queries_for(alert):
    name = alert["alertname"]
    if name in ("RecoveryPointStale", "RecoveryOffsiteStale"):
        application = alert.get("application", "")
        if not application or not all(c.isalnum() or c in "-_" for c in application):
            raise ValueError("Recovery alert has no safe application")
        prefix = "recovery-point" if name == "RecoveryPointStale" else "promote"
        selector = urllib.parse.quote(f"workflows.argoproj.io/cron-workflow={prefix}-{application}", safe="")
        return ((f"/apis/argoproj.io/v1alpha1/namespaces/backup-system/workflows"
                 f"?labelSelector={selector}&limit=15", "workflows"),)
    return TARGETS[name][1]


def slim_status(kind, status):
    if kind == "nodes":
        disks = status.get("diskStatus") or {}
        def conditions(value):
            return [{field: condition.get(field)
                     for field in ("type", "status", "reason")}
                    for condition in value[:12] if isinstance(condition, dict)]
        return {"conditions": conditions(status.get("conditions") or []),
                "diskStatus": {name: {"conditions": conditions(disk.get("conditions") or []),
                                      "storageAvailable": disk.get("storageAvailable"),
                                      "storageMaximum": disk.get("storageMaximum")}
                               for name, disk in list(disks.items())[:12]}}
    return {key: status.get(key) for key in ("phase", "succeeded", "failed",
                                            "active", "startTime", "completionTime",
                                            "startedAt", "finishedAt") if key in status}


def collect(alert):
    name = alert.get("alertname")
    if name not in INFRA:
        raise ValueError("Unsupported infrastructure alert")
    namespace, _ = TARGETS[name]
    queries = queries_for(alert)
    receipts, resources = [], {}
    for path, kind in queries:
        items = kube_get(path, receipts=receipts, max_bytes=4_000_000).get("items", [])
        # Never copy specs, annotations, log bodies, or Secret values into an
        # agent packet. Status and resource identity are enough to start RCA.
        resources[kind] = [{"name": item.get("metadata", {}).get("name"),
                            "created": item.get("metadata", {}).get("creationTimestamp"),
                            "status": slim_status(kind, item.get("status", {}))}
                           for item in items[:30]]
    return {"schema_version": 1, "namespace": namespace,
            "observed_at": stamp(now()), "alert": {
                "alertname": name, "application": alert.get("application", ""),
                "dataset": alert.get("dataset", ""),
                "fingerprint": alert.get("fingerprint", "")},
            "resources": resources, "tool_receipts": receipts}


def write_packet(snapshot, incident_id):
    INCIDENTS.mkdir(parents=True, exist_ok=True)
    packet = dict(snapshot, incident_id=incident_id,
                  capabilities=["read-only Kubernetes resource status"])
    path = INCIDENTS / f"{incident_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def validate_packet(path, incident_id=None, max_age_seconds=None):
    packet = json.loads(Path(path).read_text(encoding="utf-8"))
    alert = packet.get("alert", {})
    name = alert.get("alertname")
    if packet.get("schema_version") != 1 or name not in TARGETS:
        raise ValueError("Unexpected infrastructure packet schema or alert")
    namespace, _ = TARGETS[name]
    queries = queries_for(alert)
    if packet.get("namespace") != namespace:
        raise ValueError("Alert and namespace disagree")
    if incident_id is not None and packet.get("incident_id") != incident_id:
        raise ValueError("Incident ID mismatch")
    observed = dt.datetime.fromisoformat(packet["observed_at"].replace("Z", "+00:00"))
    if max_age_seconds is not None and abs((now() - observed).total_seconds()) > max_age_seconds:
        raise ValueError("Infrastructure packet is stale")
    for target, _ in queries:
        if not any(r.get("method") == "GET" and r.get("target") == target and
                   r.get("status") == 200 for r in packet.get("tool_receipts", [])):
            raise ValueError(f"Missing successful receipt for {target}")
    return packet


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate-packet")
    group.add_argument("--refresh-from-packet")
    args = parser.parse_args()
    if args.validate_packet:
        validate_packet(args.validate_packet)
        print("valid")
    else:
        original = validate_packet(args.refresh_from_packet)
        print(json.dumps(collect(original["alert"]), indent=2))
