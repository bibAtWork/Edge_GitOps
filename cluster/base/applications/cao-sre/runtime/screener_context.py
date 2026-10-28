#!/usr/bin/env python3
"""Bounded, read-only evidence collector for the screener SRE crew."""

import argparse
import datetime as dt
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

NAMESPACE = "real-estate-screener"
API = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
PORT = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
HEALTH = "http://screener-web.real-estate-screener.svc:3000/health/checks"
INCIDENTS = Path(os.environ.get("CAO_INCIDENT_DIR", "/home/cao/incidents"))


def now():
    return dt.datetime.now(dt.timezone.utc)


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def redact(value):
    text = str(value)[:5000]
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\b(password|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    return text


def kube_get(path, raw=False):
    if not TOKEN.exists():
        raise RuntimeError("Kubernetes service account token is unavailable")
    context = ssl.create_default_context(cafile=CA)
    url = f"https://{API}:{PORT}{path}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN.read_text().strip()}"})
    with urllib.request.urlopen(request, timeout=8, context=context) as response:
        data = response.read(400_000)
    return data.decode("utf-8", "replace") if raw else json.loads(data)


def resources(group, plural):
    prefix = "/api/v1" if not group else f"/apis/{group}"
    return kube_get(f"{prefix}/namespaces/{NAMESPACE}/{plural}").get("items", [])


def health_check():
    try:
        with urllib.request.urlopen(HEALTH, timeout=5) as response:
            body = response.read(1200).decode("utf-8", "replace")
            return {"http_status": response.status, "body": redact(body)}
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "body": redact(exc.read(1200).decode("utf-8", "replace"))}
    except Exception as exc:
        return {"observer_error": redact(exc)}


def owner_name(item, kind):
    for owner in item.get("metadata", {}).get("ownerReferences", []):
        if owner.get("kind") == kind:
            return owner.get("name", "")
    return ""


def slim_deployment(item):
    meta, spec, status = (item.get(key, {}) for key in ("metadata", "spec", "status"))
    return {
        "name": meta.get("name"), "created": meta.get("creationTimestamp"),
        "generation": meta.get("generation"), "observed_generation": status.get("observedGeneration"),
        "desired": spec.get("replicas", 1), "available": status.get("availableReplicas", 0),
        "images": [c.get("image") for c in spec.get("template", {}).get("spec", {}).get("containers", [])],
        "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"),
                        "transition": c.get("lastTransitionTime")} for c in status.get("conditions", [])],
    }


def slim_pod(item):
    meta, status = item.get("metadata", {}), item.get("status", {})
    return {
        "name": meta.get("name"), "created": meta.get("creationTimestamp"),
        "owner_job": owner_name(item, "Job"), "phase": status.get("phase"),
        "reason": status.get("reason"),
        "ready": any(c.get("type") == "Ready" and c.get("status") == "True"
                     for c in status.get("conditions", [])),
        "restarts": sum(c.get("restartCount", 0) for c in status.get("containerStatuses", [])),
    }


def slim_job(item):
    meta, status = item.get("metadata", {}), item.get("status", {})
    return {
        "name": meta.get("name"), "cronjob": owner_name(item, "CronJob"),
        "created": meta.get("creationTimestamp"), "started": status.get("startTime"),
        "completed": status.get("completionTime"), "succeeded": status.get("succeeded", 0),
        "failed": status.get("failed", 0), "active": status.get("active", 0),
    }


def slim_cnpg(item):
    meta, status = item.get("metadata", {}), item.get("status", {})
    return {
        "name": meta.get("name"), "phase": status.get("phase"),
        "ready_instances": status.get("readyInstances", 0),
        "current_primary": status.get("currentPrimary"),
        "conditions": [{"type": c.get("type"), "status": c.get("status"),
                        "reason": c.get("reason")} for c in status.get("conditions", [])],
    }


def error_logs(pods):
    web = sorted((p for p in pods if p["name"] and p["name"].startswith("screener-web-")),
                 key=lambda p: p.get("created") or "", reverse=True)
    troubled = sorted((p for p in pods if p["phase"] == "Failed" or p["restarts"] > 0),
                     key=lambda p: p.get("created") or "", reverse=True)
    selected = web[:1] + [p for p in troubled if p not in web[:1]]
    result = []
    for pod in selected[:3]:
        name = pod["name"]
        path = f"/api/v1/namespaces/{NAMESPACE}/pods/{urllib.parse.quote(name)}/log?tailLines=80&limitBytes=16000"
        try:
            lines = kube_get(path, raw=True).splitlines()
            matches = [redact(line[:500]) for line in lines
                       if re.search(r"error|failed|exception|panic|timeout", line, re.I)]
            result.append({"pod": name, "error_lines": matches[-12:]})
        except Exception as exc:
            result.append({"pod": name, "observer_error": redact(exc)})
    return result


def collect():
    deployments = resources("apps/v1", "deployments")
    replicasets = resources("apps/v1", "replicasets")
    pods_raw = resources("", "pods")
    jobs = resources("batch/v1", "jobs")
    cronjobs = resources("batch/v1", "cronjobs")
    events = resources("", "events")
    cnpg = resources("postgresql.cnpg.io/v1", "clusters")
    pods = [slim_pod(p) for p in pods_raw]
    return {
        "schema_version": 1, "namespace": NAMESPACE, "observed_at": stamp(now()),
        "health": health_check(),
        "deployments": [slim_deployment(d) for d in deployments],
        "replicasets": [{"name": r.get("metadata", {}).get("name"),
                         "created": r.get("metadata", {}).get("creationTimestamp"),
                         "deployment": owner_name(r, "Deployment"),
                         "revision": r.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision"),
                         "images": [c.get("image") for c in r.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])]}
                        for r in replicasets][-15:],
        "pods": sorted(pods, key=lambda p: p.get("created") or "")[-80:],
        "jobs": sorted((slim_job(j) for j in jobs), key=lambda j: j.get("created") or "")[-80:],
        "cronjobs": [{"name": c.get("metadata", {}).get("name"),
                      "schedule": c.get("spec", {}).get("schedule"),
                      "last_schedule": c.get("status", {}).get("lastScheduleTime"),
                      "last_success": c.get("status", {}).get("lastSuccessfulTime")}
                     for c in cronjobs],
        "cnpg": [slim_cnpg(c) for c in cnpg],
        "events": [{"time": e.get("lastTimestamp") or e.get("eventTime"),
                    "reason": e.get("reason"), "object": e.get("involvedObject", {}).get("name"),
                    "message": redact(e.get("message", "")[:500])}
                   for e in sorted(events, key=lambda x: x.get("lastTimestamp") or x.get("eventTime") or "")[-25:]],
        "logs": error_logs(pods),
    }


def detect_signals(snapshot, reference=None):
    reference = reference or now()
    signals = []
    web = next((d for d in snapshot.get("deployments", []) if d.get("name") == "screener-web"), None)
    if web is None:
        signals.append({"key": "web-missing", "detail": "screener-web Deployment is missing"})
    elif web.get("available", 0) < web.get("desired", 1):
        signals.append({"key": "web-unavailable", "detail": "screener-web has fewer available than desired replicas"})
    health = snapshot.get("health", {})
    if "http_status" in health and health["http_status"] != 200:
        signals.append({"key": "web-functionality", "detail": f"/health/checks returned HTTP {health['http_status']}"})
    if not any(c.get("name") == "screener-pg" for c in snapshot.get("cnpg", [])):
        signals.append({"key": "database-missing", "detail": "screener-pg CloudNativePG cluster is missing"})
    for cluster in snapshot.get("cnpg", []):
        if cluster.get("name") == "screener-pg" and cluster.get("ready_instances", 0) < 1:
            signals.append({"key": "database-unready", "detail": "screener-pg has no ready instance"})
    jobs = snapshot.get("jobs", [])
    for cron in snapshot.get("cronjobs", []):
        name = cron.get("name")
        related = sorted((j for j in jobs if j.get("cronjob") == name),
                         key=lambda j: j.get("created") or "")
        if (related and related[-1].get("failed", 0) > 0
                and not related[-1].get("succeeded", 0) and not related[-1].get("active", 0)):
            signals.append({"key": f"cronjob-failed:{name}", "detail": f"Latest {name} Job {related[-1]['name']} failed"})
        if name == "screener-collector":
            last = parse_time(cron.get("last_success"))
            if last and (reference - last).total_seconds() > 75 * 60:
                signals.append({"key": "collector-stale", "detail": "Collector has not succeeded for over 75 minutes"})
            elif not last:
                scheduled = parse_time(cron.get("last_schedule"))
                if scheduled and (reference - scheduled).total_seconds() > 75 * 60:
                    signals.append({"key": "collector-no-success", "detail": "Collector has no successful run despite prior schedules"})
    return signals


def write_packet(snapshot, incident_id, signal):
    INCIDENTS.mkdir(parents=True, exist_ok=True)
    packet = {**snapshot, "incident_id": incident_id, "signal": signal,
              "capabilities": ["read-only Kubernetes", "app health", "redacted error logs"]}
    path = INCIDENTS / f"{incident_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def validate_handoff(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ("schema_version", "incident_id", "stage", "status", "summary", "hypotheses", "recommended_checks", "report_path")
    if document.get("schema_version") != 1 or any(k not in document for k in required):
        raise ValueError("Handoff is missing required version 1 fields")
    if document["stage"] not in ("triage", "rca") or not isinstance(document["hypotheses"], list):
        raise ValueError("Handoff stage or hypotheses are invalid")
    if not isinstance(document["recommended_checks"], list):
        raise ValueError("Recommended checks must be an array")
    return document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-handoff")
    args = parser.parse_args()
    if args.validate_handoff:
        validate_handoff(args.validate_handoff)
        print("valid")
        return
    snapshot = collect()
    incident_id = f"manual-{now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    path = write_packet(snapshot, incident_id, {"key": "manual", "detail": "Operator-requested investigation"})
    print(path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"context collection failed: {redact(exc)}", file=sys.stderr)
        sys.exit(1)
