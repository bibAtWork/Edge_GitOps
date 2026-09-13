#!/usr/bin/env python3
"""
cluster-health.py — Functional verification suite for the Edge_GitOps cluster.

Tests the recovery system, storage health, core components, and application
readiness. Use for continuous monitoring and as a pre-update gate.

Usage:
  python3 scripts/cluster-health.py                     # monitoring checks (all groups)
  python3 scripts/cluster-health.py --mode pre-update   # stricter: verified in AWS within 26h
  python3 scripts/cluster-health.py --group backup certs # run specific group(s) only
  python3 scripts/cluster-health.py --json              # machine-readable output

Requirements: kubectl.
Run from any machine that has a working kubeconfig for the cluster.
"""

import argparse
import dataclasses
import json
import re
import subprocess
import sys
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
    """Thin wrapper around kubectl. All calls are read-only."""

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
    """The recovery system (ADR-012).

    Every application with a recovery-point CronWorkflow must have a recent
    point that passed its restore test, with a verified copy in AWS. The
    evidence is the Workflows in backup-system, as for the pre-upgrade gate: a
    recovery-point Workflow succeeds only once its point is recorded VALIDATED,
    and a promote Workflow only once its copy is verified in AWS.
    """
    results: List[Result] = []
    ns = "backup-system"

    def age(ts: Optional[str]) -> float:
        t = parse_time(ts)
        return hours_since(t) if t else float("inf")

    try:
        ctl = cl.get_json("deployments", "argo-workflows-workflow-controller", "-n", ns)
        ready = ctl.get("status", {}).get("readyReplicas", 0) or 0
        results.append(Result("backup", "argo/controller", ready >= 1, "critical",
                              f"Argo Workflows controller: {ready} replica(s) ready"))
    except Exception as exc:
        results.append(Result("backup", "argo/controller", False, "critical",
                              f"Cannot read the Argo Workflows controller: {exc}"))

    try:
        crons = cl.items("cronworkflows", "-n", ns)
        workflows = cl.items("workflows", "-n", ns)
    except Exception as exc:
        results.append(Result("backup", "recovery/list", False, "critical",
                              f"Cannot list the recovery system's workflows: {exc}"))
        return results

    for c in crons:
        name = c["metadata"]["name"]
        if c.get("spec", {}).get("suspend"):
            severity = "critical" if name.startswith(("recovery-point-", "promote-")) else "warning"
            results.append(Result("backup", f"schedule/{name}", False, severity,
                                  f"CronWorkflow {name} is SUSPENDED"))

    apps = sorted(c["metadata"]["name"][len("recovery-point-"):]
                  for c in crons if c["metadata"]["name"].startswith("recovery-point-"))
    if not apps:
        results.append(Result("backup", "recovery/applications", False, "critical",
                              "No recovery-point CronWorkflows in backup-system"))
        return results

    def runs(template: str, app: str) -> List[dict]:
        found = []
        for w in workflows:
            spec = w.get("spec", {})
            if (spec.get("workflowTemplateRef") or {}).get("name") != template:
                continue
            params = {prm.get("name"): prm.get("value")
                      for prm in (spec.get("arguments") or {}).get("parameters", [])}
            if params.get("application") == app:
                found.append(w)
        return found

    # The offsite bound is the pre-upgrade gate's (26h) before an update, and
    # RecoveryOffsiteStale's (48h) otherwise.
    offsite_h = 26.0 if pre_update else 48.0
    for app in apps:
        points = runs("recovery-point", app)
        done = sorted(w["status"]["finishedAt"] for w in points
                      if w.get("status", {}).get("phase") == "Succeeded" and w["status"].get("finishedAt"))
        promoted = [w["status"]["startedAt"] for w in runs("promote", app)
                    if w.get("status", {}).get("phase") == "Succeeded" and w["status"].get("startedAt")]
        if not done:
            results.append(Result("backup", f"{app}/validated", False, "critical",
                                  f"{app}: no validated recovery point"))
            continue
        results.append(Result("backup", f"{app}/validated", age(done[-1]) <= 26.0, "critical",
                              f"{app}: newest validated point finished {age(done[-1]):.1f}h ago (threshold 26h)"))
        # A promotion that started after a point finished, and succeeded, took it.
        verified = [ts for ts in done if any(p >= ts for p in promoted)]
        if verified:
            results.append(Result("backup", f"{app}/offsite", age(verified[-1]) <= offsite_h, "critical",
                                  f"{app}: newest point verified in AWS finished {age(verified[-1]):.1f}h ago "
                                  f"(threshold {offsite_h:.0f}h)"))
        else:
            results.append(Result("backup", f"{app}/offsite", False, "critical",
                                  f"{app}: no validated point has a verified copy in AWS"))
        recent = sorted(points, key=lambda w: w.get("metadata", {}).get("creationTimestamp", ""))[-5:]
        failed = [w["metadata"]["name"] for w in recent
                  if w.get("status", {}).get("phase") in ("Failed", "Error")]
        if failed:
            results.append(Result("backup", f"{app}/recent-failures", False, "warning",
                                  f"{app}: {len(failed)} of the last {len(recent)} recovery points failed",
                                  detail=", ".join(failed)))

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

    # ── Talos API credential expiry ───────────────────────────────────────────
    #
    # system-upgrade-controller drives every Talos and Kubernetes upgrade
    # through a talosconfig client certificate. cert-manager does not manage it
    # and nothing else watches it, so the day it lapses the upgrade Plans stop
    # working -- silently, because a Plan that cannot authenticate looks much
    # like a Plan with nothing to do.
    #
    # It also cannot be scoped narrower. Talos requires os:admin for
    # /machine.MachineService/Upgrade (os:operator can reboot but not upgrade),
    # so this credential holds full control of the node and its lifetime is the
    # only axis available. `talosctl config new` defaults --crt-ttl to 8760h,
    # which makes "when does it expire" a question worth asking continuously
    # rather than once a year.
    try:
        secrets = cl.items("secret", "talos-credentials", "-n", "cattle-system")
        if not secrets:
            data = cl.get_json("secret", "talos-credentials", "-n", "cattle-system")
            secrets = [data] if data else []
        blob = (secrets[0].get("data") or {}).get("talosconfig") if secrets else None
        if not blob:
            raise RuntimeError("talos-credentials holds no talosconfig key")

        import base64 as _b64
        cfg = _b64.b64decode(blob).decode("utf-8", "replace")
        m = re.search(r"crt:\s*([A-Za-z0-9+/=]+)", cfg)
        if not m:
            raise RuntimeError("talosconfig contains no client certificate")

        der = _b64.b64decode(m.group(1))
        if der[:5] == b"-----":
            inner = re.search(rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
                              der, re.S)
            der = _b64.b64decode(inner.group(1)) if inner else der

        # Read notAfter straight out of the DER rather than adding a crypto
        # dependency: an X.509 Validity is two ASN.1 time values, and the
        # second is notAfter. UTCTime is tag 0x17 len 0x0d, GeneralizedTime is
        # tag 0x18 len 0x0f.
        times: List[datetime] = []
        for tag, ln, fmt in ((b"\x17\x0d", 13, "%y%m%d%H%M%S"),
                             (b"\x18\x0f", 15, "%Y%m%d%H%M%S")):
            for mt in re.finditer(re.escape(tag) + b"([0-9]{" + str(ln - 1).encode() + b"})Z", der):
                try:
                    times.append(datetime.strptime(mt.group(1).decode(), fmt)
                                 .replace(tzinfo=timezone.utc))
                except ValueError:
                    continue
        times.sort()
        if len(times) < 2:
            raise RuntimeError("could not read validity dates from the certificate")

        not_after = times[-1]
        days_left = (not_after - utcnow()).days
        roles = sorted({r.decode() for r in re.findall(rb"os:[a-z:]+", der)})
        role_note = ", ".join(roles) if roles else "role not recorded in cert"

        results.append(Result(
            "certs", "talos/upgrade-credential",
            days_left >= 30,
            "critical" if days_left < 14 else "warning",
            f"Talos API credential ({role_note}) expires in {days_left}d "
            f"({not_after:%Y-%m-%d})",
            detail=("Renew with `talosctl config new --roles os:admin --crt-ttl <ttl>` and "
                    "reseal cattle-system/talos-credentials. Upgrade requires os:admin, so "
                    "the role cannot be reduced -- the TTL is the only control."),
        ))
    except Exception as exc:
        results.append(Result(
            "certs", "talos/upgrade-credential", False, "warning",
            f"Cannot read the Talos upgrade credential: {exc}",
            detail="Without this, an expiring credential fails the next upgrade silently.",
        ))

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

    # Immich: its server (a Deployment) and its database, the CloudNativePG
    # cluster immich-pg (instances/readyInstances rather than replicas).
    for name, display in [("immich-server", "Immich server"), ("immich-pg", "Immich PostgreSQL")]:
        try:
            if name == "immich-server":
                obj = cl.get_json("deployments", name, "-n", "immich")
                desired = obj.get("spec", {}).get("replicas", 1)
                ready = obj.get("status", {}).get("readyReplicas", 0)
            else:
                obj = cl.get_json("clusters.postgresql.cnpg.io", name, "-n", "immich")
                desired = obj.get("spec", {}).get("instances", 1)
                ready = obj.get("status", {}).get("readyInstances", 0)
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

# Which pinned images track the chart's own appVersion.
#
# A chart's appVersion describes ONE application. Charts routinely ship
# companion images from other projects on their own release cadences -- a
# sidecar, a CLI the operator schedules, a metadata collector -- and comparing
# those against appVersion is meaningless: it reads a version from one project
# against a version from another and calls the difference drift.
#
# Charts absent from this map compare their conventional primary image only.
# Anything not listed is still reported, but as independent rather than
# compared, because a false critical trains people to ignore the check.
APP_IMAGE_PATHS: Dict[str, List[str]] = {
    # Every controller is one Kyverno release, versioned with the chart.
    "kyverno": [
        "admissionController.container.image.tag",
        "admissionController.initContainer.image.tag",
        "backgroundController.image.tag",
        "cleanupController.image.tag",
        "reportsController.image.tag",
    ],
    # Operator and proxy ship together.
    "tailscale-operator": ["operatorConfig.image.tag", "proxyConfig.image.tag"],
    # Two roles of one binary.
    "kubeopencode": ["controller.image.tag", "server.image.tag"],
    # falcoctl and k8s-metacollector are separate projects; only falco tracks
    # the chart.
    "falco": ["image.tag"],
    # The Trivy CLI the operator schedules is a different project on a
    # different cadence to the operator itself.
    "trivy-operator": ["image.tag"],
    # Longhorn's four own images ship with the chart -- all four read v1.12.1
    # against appVersion v1.12.1. The six image.csi.* pins beside them are
    # upstream Kubernetes sidecars (attacher v4.12.0, snapshotter v8.6.0 and
    # so on), unrelated to Longhorn's version and correctly left uncompared.
    "longhorn": [
        "image.longhorn.engine.tag",
        "image.longhorn.instanceManager.tag",
        "image.longhorn.manager.tag",
        "image.longhorn.shareManager.tag",
    ],
    # The gateway the chart exists to deploy.
    "envoy-gateway": ["deployment.envoyGateway.image.tag"],
    # The controller the chart exists to deploy. Its pin currently sits ahead
    # of appVersion, which is the point of comparing: if the chart ever passes
    # it, the pin turns from a patch into a freeze.
    "system-upgrade-controller": ["systemUpgradeController.image.tag"],
    # Deliberately absent: vmstack. The umbrella chart's appVersion tracks
    # VictoriaMetrics itself (v1.151.0), while the pins beside it are the
    # operator (v0.74.1) and kube-state-metrics (v2.20.0) -- different
    # projects on different numbering. Comparing them would report a
    # permanent, meaningless "behind".
}

DEFAULT_APP_IMAGE_PATHS = ("image.tag",)


def check_pins(cl: Cluster) -> List[Result]:
    """Image pins must not fall behind the chart that packages them (ADR-009).

    An explicit image tag overrides the chart's appVersion permanently. While the
    pin is ahead it is a patch -- a security fix the chart has not shipped yet.
    The moment the chart's appVersion passes the pin, the same line silently
    becomes a downgrade: the chart's templates are written for a newer binary
    than the one that will run.

    Nothing else reports this. It produces no image diff in a version-bump PR, so
    the auto-merge gate classifies it as a chart-only update and treats it as the
    safest possible change.

    Both values are already in the cluster -- the pin in spec.values, the
    appVersion in the deployed release's status -- so this needs no registry
    call, no token and no chart download.
    """
    results: List[Result] = []

    def semver(raw: str):
        m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(raw or "").strip())
        return tuple(int(x) for x in m.groups()) if m else None

    def tags(node, path=""):
        """Yield (dotted-path, value) for every *.tag in a values tree."""
        if isinstance(node, dict):
            for key, value in node.items():
                yield from tags(value, f"{path}.{key}" if path else key)
        elif path.endswith(".tag") or path == "tag":
            if value_is_set(node):
                yield path, node

    def value_is_set(v) -> bool:
        return isinstance(v, str) and v.strip() not in ("", "null")

    try:
        releases = cl.items("helmrelease", "-A")
    except Exception as exc:
        return [Result("pins", "helmreleases", False, "critical",
                       f"Cannot list helmreleases: {exc}")]

    checked = frozen = unorderable = 0
    for hr in releases:
        name = hr.get("metadata", {}).get("name", "?")
        history = (hr.get("status", {}) or {}).get("history") or [{}]
        app_version = history[0].get("appVersion", "")
        values = (hr.get("spec", {}) or {}).get("values") or {}

        for path, pinned in tags(values):
            checked += 1
            tracks_app = path in APP_IMAGE_PATHS.get(name, DEFAULT_APP_IMAGE_PATHS)
            pin_v, app_v = semver(pinned), semver(app_version)

            if not tracks_app:
                # A companion image from another project. Recorded so the pin is
                # visible, but never compared -- its version has no relationship
                # to this chart's appVersion.
                results.append(Result(
                    "pins", f"{name}/{path}", True, "info",
                    f"{name}: pinned {pinned} (companion image, independent of appVersion)",
                ))
            elif pin_v is None or app_v is None:
                unorderable += 1
                # Not a failure. A pin that cannot be ordered is still explicit;
                # it simply cannot generate update proposals, and saying so is
                # the point -- "pinned" must not be read as "current".
                results.append(Result(
                    "pins", f"{name}/{path}", True, "info",
                    f"{name}: {pinned} not comparable with appVersion {app_version or '(none)'}",
                ))
            elif pin_v < app_v:
                frozen += 1
                results.append(Result(
                    "pins", f"{name}/{path}", False, "critical",
                    f"{name}: pinned {pinned} is OLDER than the chart's appVersion "
                    f"{app_version} -- the pin is now a downgrade, not a patch",
                    detail="Raise the pin to at least the chart's appVersion, or drop the "
                           "pin if the chart's own version is wanted. See ADR-009.",
                ))
            else:
                results.append(Result(
                    "pins", f"{name}/{path}", True, "info",
                    f"{name}: pinned {pinned} vs appVersion {app_version} "
                    f"({'ahead' if pin_v > app_v else 'equal'})",
                ))

    results.append(Result(
        "pins", "summary", frozen == 0, "critical" if frozen else "info",
        f"{checked} pin(s) checked, {frozen} behind their chart, {unorderable} not orderable",
    ))
    return results


GROUPS: Dict[str, Callable] = {
    "flux":       check_flux,
    "storage":    check_storage,
    "backup":     check_backup,
    "certs":      check_certs,
    "network":    check_network,
    "apps":       check_apps,
    "pins":       check_pins,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Edge GitOps cluster health and recovery verification suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode", choices=["monitor", "pre-update"], default="monitor",
        help="monitor: continuous health check (default). "
             "pre-update: stricter thresholds -- every application needs a point verified in AWS within 26h, as the upgrade gate requires.",
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

    if args.json:
        print(json.dumps([dataclasses.asdict(r) for r in all_results], indent=2))
    else:
        print_report(all_results, args.mode)

    critical_failures = any(not r.passed and r.severity == "critical" for r in all_results)
    return 1 if critical_failures else 0


if __name__ == "__main__":
    sys.exit(main())
