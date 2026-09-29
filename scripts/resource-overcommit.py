#!/usr/bin/env python3
"""How much of the node have the pods asked for, counted the way the scheduler counts it.

  python3 scripts/resource-overcommit.py            # totals, per namespace, uncapped containers
  python3 scripts/resource-overcommit.py --usage    # plus 7-day observed usage per container
  python3 scripts/resource-overcommit.py --json

Needs kubectl. --usage also reads VictoriaMetrics through `kubectl exec` into its pod.

Why this exists: this repo recorded the same quantity twice and got 121% (2026-09-04) and
164% (2026-09-20). Both were "sum of memory limits over allocatable"; they differed in how
they summed. `sum(kube_pod_container_resource_limits)` keeps counting pods that have
finished -- completed workflow pods stay in the API until they are garbage collected, and
there were 37 of them -- and on some days a duplicate scrape doubled it. The scheduler
counts neither: a pod in phase Succeeded or Failed has released what it held. This script
counts the way the scheduler and `kubectl describe node` do, so the two agree; the node's
own figures are printed beside it as the check.

A pod's effective request (and limit) is what Kubernetes computes: the app containers plus
any restartable init containers ("sidecars"), or the peak reached while the init containers
run one after another, whichever is larger, plus the pod's RuntimeClass overhead.
"""
import argparse
import json
import subprocess
import sys

UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
         "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
GI = 1024**3
MI = 1024**2


def quantity(value):
    """Kubernetes resource quantity -> float (bytes for memory, cores for cpu)."""
    if value is None:
        return 0.0
    text = str(value)
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    for suffix, factor in UNITS.items():
        if text.endswith(suffix):
            return float(text[:-len(suffix)]) * factor
    return float(text)


def container_amount(container, kind, resource):
    return quantity(container.get("resources", {}).get(kind, {}).get(resource))


def effective(pod, kind, resource):
    """The scheduler's view of a pod: app containers + sidecars, or the init peak, + overhead."""
    spec = pod["spec"]
    apps = sum(container_amount(c, kind, resource) for c in spec.get("containers", []))
    sidecars = 0.0   # restartable init containers keep running once started
    init_peak = 0.0
    for init in spec.get("initContainers", []):
        amount = container_amount(init, kind, resource)
        if init.get("restartPolicy") == "Always":
            sidecars += amount
            init_peak = max(init_peak, sidecars)
        else:
            init_peak = max(init_peak, sidecars + amount)
    overhead = quantity((spec.get("overhead") or {}).get(resource))
    return max(apps + sidecars, init_peak) + overhead


def counts_against_the_node(pod):
    """A pod that has finished no longer holds anything; one not yet placed holds nothing yet."""
    return pod["status"].get("phase") not in ("Succeeded", "Failed") and bool(pod["spec"].get("nodeName"))


def totals(pods, resource):
    """-> {"request": x, "limit": y, "by_namespace": {ns: (request, limit)}, "pods": n}"""
    request = limit = 0.0
    by_namespace = {}
    count = 0
    for pod in pods:
        if not counts_against_the_node(pod):
            continue
        count += 1
        r, l = effective(pod, "requests", resource), effective(pod, "limits", resource)
        request += r
        limit += l
        ns = pod["metadata"]["namespace"]
        prev = by_namespace.get(ns, (0.0, 0.0))
        by_namespace[ns] = (prev[0] + r, prev[1] + l)
    return {"request": request, "limit": limit, "by_namespace": by_namespace, "pods": count}


def uncapped(pods, resource="memory"):
    """Containers with no limit for the resource: their ceiling is the node."""
    found = []
    for pod in pods:
        if not counts_against_the_node(pod):
            continue
        for container in pod["spec"].get("containers", []) + pod["spec"].get("initContainers", []):
            if resource not in container.get("resources", {}).get("limits", {}):
                found.append((pod["metadata"]["namespace"], pod["metadata"]["name"], container["name"]))
    return sorted(found)


def kubectl_json(*args):
    return json.loads(subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True,
                                     check=True, encoding="utf-8").stdout)


def node_reported():
    """The 'Allocated resources' block of `kubectl describe node`, as printed."""
    out = subprocess.run(["kubectl", "describe", "nodes"], capture_output=True, text=True, encoding="utf-8").stdout
    lines = out.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Allocated resources:")
    except StopIteration:
        return []
    return [line for line in lines[start + 1:start + 8] if line.strip().startswith(("cpu", "memory", "Resource"))]


# ── observed usage (--usage) ─────────────────────────────────────────────────

RUNNING = 'on(namespace,pod) group_left() (max by(namespace,pod)(kube_pod_status_phase{phase=~"Running|Pending"}) == 1)'


def vm_query(expr):
    pod = subprocess.run(["kubectl", "get", "pod", "-n", "monitoring", "-l", "app.kubernetes.io/name=vmsingle",
                          "-o", "name"], capture_output=True, text=True, check=True).stdout.split()[0].split("/")[1]
    from urllib.parse import quote
    url = f"http://127.0.0.1:8428/api/v1/query?query={quote(expr)}"
    out = subprocess.run(["kubectl", "exec", "-n", "monitoring", pod, "--", "wget", "-qO-", url],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["data"]["result"]


def by_pod_container(expr):
    """Keep the pod: unrelated workloads often use the same container name."""
    return {(r["metric"].get("namespace"), r["metric"].get("pod"),
             r["metric"].get("container")): float(r["value"][1]) for r in vm_query(expr)}


def usage_rows(days=7):
    """One row per container that has a memory limit: limit, request, p95 and max working set."""
    labels = "namespace,pod,container"
    limit = by_pod_container(f'max by ({labels}) (kube_pod_container_resource_limits{{resource="memory"}} * {RUNNING})')
    request = by_pod_container(f'max by ({labels}) (kube_pod_container_resource_requests{{resource="memory"}} * {RUNNING})')
    peak = by_pod_container(f'max by ({labels}) (max_over_time(container_memory_working_set_bytes{{container!="",image!=""}}[{days}d]))')
    p95 = by_pod_container(f'max by ({labels}) (quantile_over_time(0.95, container_memory_working_set_bytes{{container!="",image!=""}}[{days}d]))')
    rows = [{"namespace": ns, "pod": pod, "container": c, "limit": lim,
             "request": request.get(k, 0.0), "p95": p95.get(k), "max": peak.get(k)}
            for k, lim in limit.items() for ns, pod, c in [k]]
    return sorted(rows, key=lambda r: (r["max"] is not None,
                                       r["limit"] - (r["max"] or 0)), reverse=True)


# ── report ───────────────────────────────────────────────────────────────────

def report(node_memory, node_cpu, mem, cpu, missing, top, usage=None, days=7):
    lines = []
    pct = lambda a, b: f"{100 * a / b:5.0f}%"  # noqa: E731
    lines.append(f"Pods holding resources on the node: {mem['pods']} (finished pods excluded, as the scheduler does)")
    lines.append("")
    lines.append(f"{'':14s}{'requests':>12s}{'limits':>12s}{'allocatable':>13s}   requests/alloc  limits/alloc")
    lines.append(f"{'memory (GiB)':14s}{mem['request'] / GI:12.2f}{mem['limit'] / GI:12.2f}{node_memory / GI:13.2f}"
                 f"   {pct(mem['request'], node_memory):>14s}  {pct(mem['limit'], node_memory):>12s}")
    lines.append(f"{'cpu (cores)':14s}{cpu['request']:12.2f}{cpu['limit']:12.2f}{node_cpu:13.2f}"
                 f"   {pct(cpu['request'], node_cpu):>14s}  {pct(cpu['limit'], node_cpu):>12s}")
    lines.append("")
    lines.append(f"Largest memory limits by namespace (top {top}):")
    ranked = sorted(mem["by_namespace"].items(), key=lambda kv: kv[1][1], reverse=True)[:top]
    for ns, (request, limit) in ranked:
        lines.append(f"  {ns:28s} requests {request / MI:8.0f} Mi   limits {limit / MI:8.0f} Mi")
    lines.append("")
    lines.append(f"{len(missing)} declared containers in active pods have no memory limit "
                 "(init containers matter during startup):")
    for ns, pod, container in missing:
        lines.append(f"  {ns}/{pod} [{container}]")
    if usage is not None:
        lines.append("")
        lines.append(f"Memory limit against what each container actually used over {days} days (MiB), largest "
                     f"unused headroom first. A gap is a candidate, not a verdict: read why the limit is what it is "
                     f"before touching it (docs/runbooks/resource-rightsizing.md, rule 4).")
        measured = [row for row in usage if row["max"] is not None]
        lines.append(f"  {'namespace/pod/container':70s} {'limit':>7s} {'request':>8s} {'p95':>6s} {'max':>6s} {'limit/max':>10s}")
        for row in measured[:top]:
            ratio = f"{row['limit'] / row['max']:8.1f}x" if row["max"] else "       n/a"
            p95 = f"{row['p95'] / MI:6.0f}" if row["p95"] is not None else "   n/a"
            lines.append(f"  {(row['namespace'] + '/' + row['pod'] + '/' + row['container'])[:70]:70s} "
                         f"{row['limit'] / MI:7.0f} {row['request'] / MI:8.0f} "
                         f"{p95} {row['max'] / MI:6.0f} {ratio:>10s}")
        lines.append(f"  headroom above 7-day maximum across {len(measured)} measured containers: "
                     f"{sum(r['limit'] - r['max'] for r in measured) / GI:.1f} GiB")
        if len(measured) != len(usage):
            lines.append(f"  {len(usage) - len(measured)} current containers had no usage samples; their headroom is unknown")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--usage", action="store_true", help="add 7-day observed usage from VictoriaMetrics")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    pods = kubectl_json("get", "pods", "-A")["items"]
    nodes = kubectl_json("get", "nodes")["items"]
    node_memory = sum(quantity(n["status"]["allocatable"]["memory"]) for n in nodes)
    node_cpu = sum(quantity(n["status"]["allocatable"]["cpu"]) for n in nodes)
    mem, cpu, missing = totals(pods, "memory"), totals(pods, "cpu"), uncapped(pods)
    usage = usage_rows(args.days) if args.usage else None

    if args.json:
        print(json.dumps({"allocatable": {"memory": node_memory, "cpu": node_cpu},
                          "memory": {k: mem[k] for k in ("request", "limit", "pods")},
                          "cpu": {k: cpu[k] for k in ("request", "limit")},
                          "uncapped": missing, "usage": usage}, indent=2))
        return 0
    print(report(node_memory, node_cpu, mem, cpu, missing, args.top, usage, args.days))
    print("\nThe node's own accounting, for comparison (kubectl describe node):")
    for line in node_reported():
        print(f"  {line.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
