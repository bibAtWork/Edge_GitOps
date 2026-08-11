#!/usr/bin/env python3
"""
cluster-health.py — Functional verification suite for the Edge_GitOps cluster.

Tests backup integrity, storage health, core components, and application
readiness. Use for continuous monitoring and as a pre-update gate.

Usage:
  python3 scripts/cluster-health.py                     # monitoring checks (all groups)
  python3 scripts/cluster-health.py --mode pre-update   # stricter; triggers fresh backup if needed
  python3 scripts/cluster-health.py --group backup etcd # run specific group(s) only
  python3 scripts/cluster-health.py --json              # machine-readable output

Requirements: kubectl (and velero CLI for the backup trigger in pre-update mode).
Run from any machine that has a working kubeconfig for the cluster.
"""

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple


# ── Types ──────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    group: str
    name: str
    passed: bool
    severity: str   # "critical" | "warning" | "info"
    message: str
    detail: str = ""


# ── Kubectl wrapper ─────────────────────────────────────────────────────────────

class Cluster:
    """Thin wrapper around kubectl. All calls are read-only except trigger_backup()."""

    def __init__(self, kubeconfig: Optional[str] = None, timeout: int = 30):
        self._base = ["kubectl"]
        if kubeconfig:
            self._base += ["--kubeconfig", kubeconfig]
        self._timeout = timeout

    def _run(self, args: List[str], *, input: Optional[str] = None) -> str:
        cmd = self._base + args
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=self._timeout, input=input,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"exit {r.returncode}")
        return r.stdout

    def get_json(self, *args: str) -> dict:
        return json.loads(self._run(["get", "-o", "json"] + list(args)))

    def items(self, *args: str) -> list:
        return self.get_json(*args).get("items", [])

    def exists(self, *args: str) -> bool:
        try:
            self.get_json(*args)
            return True
        except Exception:
            return False

    def ready_replicas(self, kind: str, name: str, namespace: str) -> Tuple[int, int]:
        """Return (ready, desired) for a Deployment or StatefulSet."""
        obj = self.get_json(kind, name, "-n", namespace)
        desired = obj.get("spec", {}).get("replicas", 1)
        ready = obj.get("status", {}).get("readyReplicas", 0)
        return ready, desired


# ── Helpers ─────────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_since(t: Optional[datetime]) -> float:
    if t is None:
        return float("inf")
    return (utcnow() - t).total_seconds() / 3600


def workload_ok(obj: dict) -> Tuple[bool, str]:
    """Return (ok, message) for a Deployment or StatefulSet."""
    desired = obj.get("spec", {}).get("replicas", 1)
    status = obj.get("status", {})
    ready = status.get("readyReplicas", 0)
    available = status.get("availableReplicas", ready)
    ok = ready >= desired > 0
    return ok, f"{ready}/{desired} ready"


# ── Check groups ────────────────────────────────────────────────────────────────

def check_flux(cl: Cluster) -> List[Result]:
    results: List[Result] = []

    # ── Flux controller pods ──────────────────────────────────────────────────
    expected = {
        "source-controller",
        "kustomize-controller",
        "helm-controller",
        "notification-controller",
    }
    try:
        pods = cl.items("pods", "-n", "flux-system")
        running_names = {
            p["metadata"]["name"]
            for p in pods
            if p.get("status", {}).get("phase") == "Running"
        }
        for ctrl in sorted(expected):
            found = any(ctrl in n for n in running_names)
            results.append(Result(
                "flux", f"controller/{ctrl}", found, "critical",
                f"{ctrl}: {'running' if found else 'NOT FOUND / not running'}",
            ))
    except Exception as exc:
        results.append(Result("flux", "controllers", False, "critical", f"Cannot list pods in flux-system: {exc}"))

    # ── Kustomizations ────────────────────────────────────────────────────────
    try:
        kustomizations = cl.items("kustomizations", "-A")
        for ks in kustomizations:
            ns = ks["metadata"]["namespace"]
            name = ks["metadata"]["name"]
            conditions = ks.get("status", {}).get("conditions", [])
            ready_cond = next((c for c in conditions if c["type"] == "Ready"), None)
            is_ready = ready_cond is not None and ready_cond.get("status") == "True"
            msg = ready_cond.get("message", "no status") if ready_cond else "no Ready condition"
            results.append(Result(
                "flux", f"kustomization/{ns}/{name}", is_ready, "critical",
                f"{'Ready' if is_ready else 'NOT Ready'}: {msg[:120]}",
            ))
    except Exception as exc:
        results.append(Result("flux", "kustomizations", False, "critical", f"Cannot list kustomizations: {exc}"))

    # ── HelmReleases ──────────────────────────────────────────────────────────
    try:
        hrs = cl.items("helmreleases", "-A")
        failed, not_ready = [], []
        for hr in hrs:
            ns = hr["metadata"]["namespace"]
            name = hr["metadata"]["name"]
            conditions = hr.get("status", {}).get("conditions", [])
            ready_cond = next((c for c in conditions if c["type"] == "Ready"), None)
            if ready_cond is None:
                continue
            if ready_cond.get("status") == "False":
                reason = ready_cond.get("reason", "")
                label = f"{ns}/{name}"
                if any(w in reason.lower() for w in ("failed", "upgrade", "install", "error")):
                    failed.append(label)
                else:
                    not_ready.append(label)

        results.append(Result(
            "flux", "helmreleases/none-failed",
            len(failed) == 0, "critical",
            f"{len(hrs)} HelmReleases total, {len(failed)} in Failed state",
            detail=", ".join(failed),
        ))
        results.append(Result(
            "flux", "helmreleases/all-ready",
            len(not_ready) == 0 and len(failed) == 0, "warning",
            f"{len(hrs) - len(failed) - len(not_ready)}/{len(hrs)} HelmReleases Ready",
            detail=", ".join(not_ready),
        ))
    except Exception as exc:
        results.append(Result("flux", "helmreleases", False, "critical", f"Cannot list helmreleases: {exc}"))

    return results


def check_storage(cl: Cluster) -> List[Result]:
    results: List[Result] = []

    # ── SeaweedFS components ──────────────────────────────────────────────────
    seaweed_components = [
        ("master",         "app.kubernetes.io/name=seaweedfs-master"),
        ("volume",         "app.kubernetes.io/name=seaweedfs-volume"),
        ("filer",          "app.kubernetes.io/name=seaweedfs-filer"),
        ("csi-controller", "app.kubernetes.io/component=csi-driver"),
    ]
    for component, label in seaweed_components:
        try:
            pods = cl.items("pods", "-n", "seaweedfs", "-l", label)
            if not pods:
                # Seaweedfs helm chart may use different label keys; fall back to all pods
                all_pods = cl.items("pods", "-n", "seaweedfs")
                pods = [p for p in all_pods if component in p["metadata"]["name"]]
            running = sum(1 for p in pods if p.get("status", {}).get("phase") == "Running")
            ok = running >= 1
            results.append(Result(
                "storage", f"seaweedfs/{component}", ok, "critical",
                f"seaweedfs-{component}: {running}/{len(pods)} pods running",
            ))
        except Exception as exc:
            results.append(Result("storage", f"seaweedfs/{component}", False, "critical", str(exc)))

    # ── SeaweedFS S3 endpoint reachable (via BSL probe below; also check pod ready) ──
    try:
        pods = cl.items("pods", "-n", "seaweedfs")
        filer_pods = [p for p in pods if "filer" in p["metadata"]["name"]]
        all_containers_ready = all(
            all(cs.get("ready", False) for cs in p.get("status", {}).get("containerStatuses", []))
            for p in filer_pods
        )
        results.append(Result(
            "storage", "seaweedfs/filer-containers-ready",
            all_containers_ready and len(filer_pods) > 0, "critical",
            f"SeaweedFS filer containers ready: {all_containers_ready}",
        ))
    except Exception as exc:
        results.append(Result("storage", "seaweedfs/filer-containers-ready", False, "critical", str(exc)))

    # ── local-path-provisioner ───────────────────────────────────────────────
    try:
        pods = cl.items("pods", "-n", "local-path-storage")
        running = sum(1 for p in pods if p.get("status", {}).get("phase") == "Running")
        results.append(Result(
            "storage", "local-path-provisioner", running >= 1, "critical",
            f"local-path-provisioner: {running}/{len(pods)} pods running",
        ))
    except Exception as exc:
        results.append(Result("storage", "local-path-provisioner", False, "warning", str(exc)))

    return results


def check_backup(cl: Cluster, *, pre_update: bool = False) -> List[Result]:
    results: List[Result] = []

    # ── Velero pods ───────────────────────────────────────────────────────────
    try:
        pods = cl.items("pods", "-n", "velero")
        velero_main = [p for p in pods if "node-agent" not in p["metadata"]["name"] and "velero" in p["metadata"]["name"]]
        node_agents = [p for p in pods if "node-agent" in p["metadata"]["name"]]

        v_running = sum(1 for p in velero_main if p.get("status", {}).get("phase") == "Running")
        results.append(Result(
            "backup", "velero/server",
            v_running >= 1, "critical",
            f"velero server: {v_running}/{len(velero_main)} pods running",
        ))

        na_running = sum(1 for p in node_agents if p.get("status", {}).get("phase") == "Running")
        results.append(Result(
            "backup", "velero/node-agent",
            na_running >= 1, "warning",
            f"velero node-agent: {na_running}/{len(node_agents)} pods running",
        ))
    except Exception as exc:
        results.append(Result("backup", "velero/pods", False, "critical", f"Cannot list velero pods: {exc}"))

    # ── Backup Storage Locations ──────────────────────────────────────────────
    try:
        bsls = cl.items("backupstoragelocations", "-n", "velero")
        expected_bsls = {"seaweedfs-local": "critical", "aws-s3": "warning"}
        found = {b["metadata"]["name"]: b for b in bsls}

        for bsl_name, severity in expected_bsls.items():
            if bsl_name not in found:
                results.append(Result(
                    "backup", f"bsl/{bsl_name}", False, severity,
                    f"BSL {bsl_name} not found",
                ))
                continue
            bsl = found[bsl_name]
            phase = bsl.get("status", {}).get("phase", "Unknown")
            last_validated = parse_time(bsl.get("status", {}).get("lastValidationTime"))
            age_str = f" (validated {hours_since(last_validated):.1f}h ago)" if last_validated else ""
            results.append(Result(
                "backup", f"bsl/{bsl_name}",
                phase == "Available", severity,
                f"BSL {bsl_name}: {phase}{age_str}",
            ))
    except Exception as exc:
        results.append(Result("backup", "bsl", False, "critical", f"Cannot list BSLs: {exc}"))

    # ── Velero schedules ──────────────────────────────────────────────────────
    try:
        schedules = cl.items("schedules", "-n", "velero")
        sched_map = {s["metadata"]["name"]: s for s in schedules}
        required = ["daily-local", "weekly-offsite", "monthly-offsite"]

        for sched_name in required:
            if sched_name not in sched_map:
                results.append(Result(
                    "backup", f"schedule/{sched_name}", False, "critical",
                    f"Velero schedule '{sched_name}' not found",
                ))
                continue
            sched = sched_map[sched_name]
            paused = sched.get("spec", {}).get("paused", False)
            last_run = parse_time(sched.get("status", {}).get("lastBackupTime"))
            last_str = f", last ran {hours_since(last_run):.1f}h ago" if last_run else ", never ran"
            results.append(Result(
                "backup", f"schedule/{sched_name}",
                not paused, "critical",
                f"Schedule '{sched_name}': {'PAUSED' if paused else 'active'}{last_str}",
            ))
    except Exception as exc:
        results.append(Result("backup", "schedules", False, "critical", f"Cannot list schedules: {exc}"))

    # ── Recent backup: status and age ─────────────────────────────────────────
    try:
        backups = cl.items("backups", "-n", "velero")
        backups.sort(
            key=lambda b: b.get("metadata", {}).get("creationTimestamp", ""),
            reverse=True,
        )
        completed = [b for b in backups if b.get("status", {}).get("phase") in ("Completed", "PartiallyFailed")]
        failed_recent = [b for b in backups[:5] if b.get("status", {}).get("phase") == "Failed"]

        if not completed:
            results.append(Result(
                "backup", "recent-backup/exists", False, "critical",
                "No completed backups found in Velero",
            ))
        else:
            latest = completed[0]
            latest_name = latest["metadata"]["name"]
            phase = latest.get("status", {}).get("phase", "Unknown")
            created = parse_time(latest.get("metadata", {}).get("creationTimestamp"))
            age_h = hours_since(created)

            results.append(Result(
                "backup", "recent-backup/status",
                phase == "Completed", "critical",
                f"Latest backup '{latest_name}': {phase}",
            ))

            # Threshold: 26 h for monitoring (daily schedule + 2 h slack), 2 h for pre-update
            threshold_h = 2.0 if pre_update else 26.0
            threshold_label = "2h" if pre_update else "26h (daily + 2h slack)"
            results.append(Result(
                "backup", "recent-backup/age",
                age_h <= threshold_h, "critical",
                f"Latest backup is {age_h:.1f}h old — threshold: {threshold_label}",
                detail=latest_name,
            ))

        if failed_recent:
            names = ", ".join(b["metadata"]["name"] for b in failed_recent)
            results.append(Result(
                "backup", "recent-backup/no-failures",
                False, "warning",
                f"{len(failed_recent)} failed backup(s) among the 5 most recent",
                detail=names,
            ))
    except Exception as exc:
        results.append(Result("backup", "recent-backup", False, "critical", f"Cannot list backups: {exc}"))

    return results


def check_etcd(cl: Cluster) -> List[Result]:
    results: List[Result] = []
    ns = "talos-backup"

    # ── CronJob exists and is not suspended ───────────────────────────────────
    try:
        cj = cl.get_json("cronjob", "talos-backup", "-n", ns)
    except Exception as exc:
        results.append(Result(
            "etcd", "cronjob/exists", False, "critical",
            f"CronJob talos-backup not found in namespace '{ns}': {exc}",
        ))
        return results

    suspended = cj.get("spec", {}).get("suspend", False)
    results.append(Result(
        "etcd", "cronjob/not-suspended",
        not suspended, "critical",
        f"talos-backup CronJob: {'SUSPENDED — etcd snapshots will not be taken!' if suspended else 'active'}",
    ))

    last_scheduled = parse_time(cj.get("status", {}).get("lastScheduleTime"))
    # Schedule is every 6 h; allow 7 h before warning
    results.append(Result(
        "etcd", "cronjob/schedule-recent",
        hours_since(last_scheduled) <= 7.0, "warning",
        f"Last scheduled: {hours_since(last_scheduled):.1f}h ago (schedule: every 6h)",
    ))

    # ── Last Job: succeeded and age ───────────────────────────────────────────
    try:
        jobs = cl.items("jobs", "-n", ns)
        jobs.sort(
            key=lambda j: parse_time(j.get("status", {}).get("startTime")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        if not jobs:
            results.append(Result("etcd", "last-job/exists", False, "warning",
                                  "No talos-backup jobs found yet (CronJob may not have fired)"))
            return results

        latest = jobs[0]
        succeeded = latest.get("status", {}).get("succeeded", 0) >= 1
        job_failed = latest.get("status", {}).get("failed", 0)
        completion = parse_time(latest.get("status", {}).get("completionTime"))
        start = parse_time(latest.get("status", {}).get("startTime"))
        age_h = hours_since(completion or start)

        results.append(Result(
            "etcd", "last-job/succeeded",
            succeeded, "critical",
            f"Last talos-backup job: {'succeeded' if succeeded else f'FAILED (failure count: {job_failed})'}",
        ))

        # Allow 8 h (6 h schedule + 2 h slack)
        results.append(Result(
            "etcd", "last-job/age",
            age_h <= 8.0, "warning",
            f"Last etcd backup completed {age_h:.1f}h ago (threshold: 8h)",
        ))
    except Exception as exc:
        results.append(Result("etcd", "last-job", False, "warning", f"Cannot inspect talos-backup jobs: {exc}"))

    return results


def check_certs(cl: Cluster) -> List[Result]:
    results: List[Result] = []

    # ── cert-manager pods ─────────────────────────────────────────────────────
    try:
        pods = cl.items("pods", "-n", "cert-manager")
        cm_pods = [p for p in pods if "cert-manager" in p["metadata"]["name"]]
        running = sum(1 for p in cm_pods if p.get("status", {}).get("phase") == "Running")
        results.append(Result(
            "certs", "cert-manager/pods",
            running >= 1, "critical",
            f"cert-manager: {running}/{len(cm_pods)} pods running",
        ))
    except Exception as exc:
        results.append(Result("certs", "cert-manager/pods", False, "critical", str(exc)))

    # ── ClusterIssuers ────────────────────────────────────────────────────────
    try:
        issuers = cl.items("clusterissuers")
        for issuer in issuers:
            name = issuer["metadata"]["name"]
            conditions = issuer.get("status", {}).get("conditions", [])
            ready_cond = next((c for c in conditions if c["type"] == "Ready"), None)
            is_ready = ready_cond is not None and ready_cond.get("status") == "True"
            msg = ready_cond.get("message", "?") if ready_cond else "no status"
            results.append(Result(
                "certs", f"clusterissuer/{name}",
                is_ready, "warning",
                f"ClusterIssuer/{name}: {'Ready' if is_ready else f'NOT Ready — {msg[:100]}'}",
            ))
    except Exception as exc:
        results.append(Result("certs", "clusterissuers", False, "warning", str(exc)))

    # ── Certificate expiry ────────────────────────────────────────────────────
    try:
        certs = cl.items("certificates", "-A")
        now = utcnow()
        expired, expiring = [], []

        for cert in certs:
            ns = cert["metadata"]["namespace"]
            name = cert["metadata"]["name"]
            not_after = parse_time(cert.get("status", {}).get("notAfter"))
            if not_after is None:
                continue
            days_left = (not_after - now).days
            label = f"{ns}/{name} ({days_left}d)"
            if days_left < 0:
                expired.append(f"{ns}/{name} (expired {-days_left}d ago)")
            elif days_left < 14:
                expiring.append(label)

        results.append(Result(
            "certs", "certificates/not-expired",
            len(expired) == 0, "critical",
            f"{'No expired certificates' if not expired else f'{len(expired)} EXPIRED certificate(s)'}",
            detail=", ".join(expired),
        ))
        results.append(Result(
            "certs", "certificates/expiry-14d-warning",
            len(expiring) == 0, "warning",
            f"{'No certificates expiring within 14 days' if not expiring else f'{len(expiring)} expiring soon'}",
            detail=", ".join(expiring),
        ))
    except Exception as exc:
        results.append(Result("certs", "certificates", False, "warning", str(exc)))

    return results


def check_network(cl: Cluster) -> List[Result]:
    results: List[Result] = []

    # ── Cilium DaemonSet ──────────────────────────────────────────────────────
    try:
        pods = cl.items("pods", "-n", "kube-system", "-l", "k8s-app=cilium")
        if not pods:
            pods = cl.items("pods", "-n", "kube-system", "-l", "app.kubernetes.io/name=cilium")
        total = len(pods)
        running = sum(1 for p in pods if p.get("status", {}).get("phase") == "Running")
        ok = running >= 1 and running == total
        results.append(Result(
            "network", "cilium/daemonset",
            ok, "critical",
            f"Cilium: {running}/{total} pods running",
        ))
    except Exception as exc:
        results.append(Result("network", "cilium/daemonset", False, "critical", str(exc)))

    # ── Default-deny cluster-wide policies ───────────────────────────────────
    for policy in ("default-deny-ingress", "default-deny-egress"):
        present = cl.exists("ciliumclusterwidenetworkpolicies", policy)
        results.append(Result(
            "network", f"policy/{policy}",
            present, "critical",
            f"CiliumClusterwideNetworkPolicy '{policy}': {'present' if present else 'MISSING — no default deny!'}",
        ))

    # ── SeaweedFS network protection ──────────────────────────────────────────
    present = cl.exists("ciliumnetworkpolicies", "allow-seaweedfs-internal", "-n", "seaweedfs")
    results.append(Result(
        "network", "policy/allow-seaweedfs-internal",
        present, "critical",
        f"allow-seaweedfs-internal (port 8333 ingress): {'present' if present else 'MISSING'}",
    ))

    return results


def check_apps(cl: Cluster) -> List[Result]:
    results: List[Result] = []

    # Each entry: (display_name, namespace, kind, resource_name, severity)
    workloads = [
        ("VictoriaMetrics",    "monitoring",    "deployments",   "vmsingle-vmstack-victoria-metrics-k8s-stack", "warning"),
        ("Grafana",            "monitoring",    "deployments",   "grafana",                                     "warning"),
        ("OTel agent",         "monitoring",    "daemonsets",    "otel-agent",                                  "warning"),
        ("OTel gateway",       "monitoring",    "deployments",   "otel-collector-gateway",                      "warning"),
        ("Velero",             "velero",        "deployments",   "velero",                                      "critical"),
        ("cert-manager",       "cert-manager",  "deployments",   "cert-manager",                                "critical"),
        ("cert-manager-cainjector", "cert-manager", "deployments", "cert-manager-cainjector",                  "warning"),
        ("external-dns",       "external-dns",  "deployments",   "external-dns",                               "warning"),
        ("Zot registry",       "zot",           "statefulsets",  "zot",                                        "warning"),
        ("Trivy operator",     "trivy-system",  "deployments",   "trivy-operator",                             "warning"),
        ("Paperless-NGX",      "paperless",     "deployments",   "paperless-ngx",                              "warning"),
        ("Paperless Valkey",   "paperless",     "deployments",   "paperless-valkey",                           "warning"),
    ]

    for display, ns, kind, name, severity in workloads:
        try:
            obj = cl.get_json(kind, name, "-n", ns)

            if kind == "daemonsets":
                desired = obj.get("status", {}).get("desiredNumberScheduled", 1)
                ready = obj.get("status", {}).get("numberReady", 0)
                ok = ready >= desired > 0
                msg = f"{ready}/{desired} pods ready"
            else:
                desired = obj.get("spec", {}).get("replicas", 1)
                ready = obj.get("status", {}).get("readyReplicas", 0)
                ok = ready >= desired > 0
                msg = f"{ready}/{desired} replicas ready"

            results.append(Result("apps", f"{ns}/{name}", ok, severity, f"{display}: {msg}"))
        except RuntimeError as exc:
            # Resource not found is a warning, not a failure (e.g. 3-node has no Immich)
            err = str(exc)
            if "not found" in err.lower():
                results.append(Result("apps", f"{ns}/{name}", False, "info", f"{display}: not deployed"))
            else:
                results.append(Result("apps", f"{ns}/{name}", False, severity, f"{display}: {err[:120]}"))

    # Immich: check both server and postgresql
    for name, display in [("immich-server", "Immich server"), ("immich-postgresql", "Immich PostgreSQL")]:
        try:
            # Immich server is a Deployment, postgresql is a StatefulSet
            kind = "deployments" if "server" in name else "statefulsets"
            obj = cl.get_json(kind, name, "-n", "immich")
            desired = obj.get("spec", {}).get("replicas", 1)
            ready = obj.get("status", {}).get("readyReplicas", 0)
            ok = ready >= desired > 0
            results.append(Result("apps", f"immich/{name}", ok, "warning", f"{display}: {ready}/{desired} ready"))
        except RuntimeError as exc:
            err = str(exc)
            results.append(Result(
                "apps", f"immich/{name}",
                False, "info" if "not found" in err.lower() else "warning",
                f"{display}: {'not deployed' if 'not found' in err.lower() else err[:120]}",
            ))

    return results


# ── Pre-update: trigger a fresh backup ──────────────────────────────────────────

def trigger_backup(cl: Cluster) -> Result:
    """Create a Velero backup in the seaweedfs-local BSL and wait up to 10 min."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_name = f"pre-update-{ts}"

    manifest = json.dumps({
        "apiVersion": "velero.io/v1",
        "kind": "Backup",
        "metadata": {"name": backup_name, "namespace": "velero"},
        "spec": {
            "storageLocation": "seaweedfs-local",
            "ttl": "24h0m0s",
            "defaultVolumesToFsBackup": True,
            "snapshotVolumes": False,
        },
    })

    try:
        cl._run(["create", "-f", "-"], input=manifest)
    except Exception as exc:
        return Result("backup", "pre-update/trigger", False, "critical",
                      f"Failed to create pre-update backup: {exc}")

    print(f"  → Created backup '{backup_name}', waiting up to 10 min for completion...")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(15)
        try:
            b = cl.get_json("backup", backup_name, "-n", "velero")
            phase = b.get("status", {}).get("phase", "")
            if phase == "Completed":
                return Result("backup", "pre-update/trigger", True, "critical",
                              f"Pre-update backup '{backup_name}' completed successfully")
            if phase in ("Failed", "PartiallyFailed"):
                errors = b.get("status", {}).get("errors", 0)
                return Result("backup", "pre-update/trigger", False, "critical",
                              f"Pre-update backup '{backup_name}' {phase} (errors: {errors})")
        except Exception:
            pass

    return Result("backup", "pre-update/trigger", False, "critical",
                  f"Pre-update backup '{backup_name}' timed out after 10 min")


# ── Output ──────────────────────────────────────────────────────────────────────

_ICONS = {(True, "critical"): "✓", (True, "warning"): "✓", (True, "info"): "✓",
          (False, "critical"): "✗", (False, "warning"): "⚠", (False, "info"): "·"}


def print_report(results: List[Result], mode: str) -> None:
    groups: Dict[str, List[Result]] = {}
    for r in results:
        groups.setdefault(r.group, []).append(r)

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    critical_failures = sum(1 for r in results if not r.passed and r.severity == "critical")
    warnings = sum(1 for r in results if not r.passed and r.severity == "warning")

    width = 72
    print()
    print("═" * width)
    print(f"  Edge GitOps — Cluster Health  [{mode.upper()}]  {utcnow():%Y-%m-%d %H:%M UTC}")
    print("═" * width)

    for group, group_results in groups.items():
        group_ok = all(r.passed or r.severity not in ("critical", "warning") for r in group_results)
        group_icon = "✓" if group_ok else "✗"
        crit_count = sum(1 for r in group_results if not r.passed and r.severity == "critical")
        warn_count = sum(1 for r in group_results if not r.passed and r.severity == "warning")
        suffix = ""
        if crit_count:
            suffix += f"  [{crit_count} CRITICAL]"
        if warn_count:
            suffix += f"  [{warn_count} warn]"
        print(f"\n  {group_icon} {group.upper()}{suffix}")
        print("  " + "─" * (width - 2))
        for r in group_results:
            icon = _ICONS.get((r.passed, r.severity), "?")
            sev_tag = f"[{r.severity.upper()[:4]}] " if not r.passed and r.severity != "info" else ""
            line = f"  {icon}  {r.name:<44} {sev_tag}{r.message}"
            print(line[:width + 10])  # allow slight overflow for readability
            if r.detail and not r.passed:
                print(f"          → {r.detail[:width - 12]}")

    print()
    print("═" * width)
    overall = "PASS ✓" if critical_failures == 0 else "FAIL ✗"
    print(f"  {overall}  |  {passed}/{total} checks passed  |  "
          f"{critical_failures} critical  |  {warnings} warnings")
    print("═" * width)
    print()


# ── Entry point ─────────────────────────────────────────────────────────────────

GROUPS: Dict[str, Callable] = {
    "flux":       check_flux,
    "storage":    check_storage,
    "backup":     check_backup,
    "etcd":       check_etcd,
    "certs":      check_certs,
    "network":    check_network,
    "apps":       check_apps,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Edge GitOps cluster health and backup verification suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode", choices=["monitor", "pre-update"], default="monitor",
        help="monitor: continuous health check (default). "
             "pre-update: stricter age thresholds; triggers a fresh backup if the most recent is >2h old.",
    )
    parser.add_argument(
        "--group", nargs="+", choices=list(GROUPS),
        help="Run only specified group(s). Default: all groups.",
    )
    parser.add_argument("--kubeconfig", metavar="PATH", help="Path to kubeconfig file.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--fail-fast", action="store_true",
        help="Stop after the first group that contains a critical failure.",
    )
    args = parser.parse_args()

    cl = Cluster(kubeconfig=args.kubeconfig)
    is_pre_update = args.mode == "pre-update"
    groups_to_run = args.group or list(GROUPS)

    all_results: List[Result] = []
    for group in groups_to_run:
        fn = GROUPS[group]
        kwargs = {}
        if group == "backup":
            kwargs["pre_update"] = is_pre_update
        try:
            results = fn(cl, **kwargs)
        except Exception as exc:
            results = [Result(group, "runner", False, "critical", f"Check group crashed: {exc}")]

        all_results.extend(results)

        if args.fail_fast and any(not r.passed and r.severity == "critical" for r in results):
            all_results.append(Result(group, "_fail-fast", False, "info",
                                      "Stopped after first critical failure (--fail-fast)"))
            break

    # ── Pre-update: trigger backup if too old ─────────────────────────────────
    if is_pre_update and "backup" in groups_to_run:
        age_ok = any(r.name == "recent-backup/age" and r.passed for r in all_results)
        if not age_ok:
            print("  → Most recent backup is older than 2h; triggering pre-update backup...")
            trigger_result = trigger_backup(cl)
            all_results.append(trigger_result)

    if args.json:
        print(json.dumps([dataclasses.asdict(r) for r in all_results], indent=2))
    else:
        print_report(all_results, args.mode)

    critical_failures = any(not r.passed and r.severity == "critical" for r in all_results)
    return 1 if critical_failures else 0


if __name__ == "__main__":
    sys.exit(main())
