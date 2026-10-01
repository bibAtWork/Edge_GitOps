# Trivy config-audit triage

The [Trivy Operator dashboard](https://grafana.homelab.data-harness.org/d/ycwPj724k)
leads with **active High config checks**. It counts reports for current
non-Job/ReplicaSet resources, ReplicaSets with desired replicas above zero, and
Jobs with active pods. The lower row keeps the raw all-severity report total and
the High checks excluded as inactive. A completed Job or scaled-to-zero
ReplicaSet can retain a report after it stops exposing a workload. The dashboard
uses kube-state-metrics to classify those reports; if that scrape is absent,
check its health before trusting the active count.

The namespace panel shows active High checks. The severity panel shows **all**
reported config checks, including inactive resources. RBAC checks are separate:
the Critical headline includes all reported Roles and ClusterRoles, whether or
not they are bound. A RoleBinding/ClusterRoleBinding and the controller's actual
verbs must be checked before treating an RBAC report as an effective permission.

For a new finding, list recent reports with
`kubectl get configauditreports -A --sort-by=.metadata.creationTimestamp`, then
inspect the relevant report with
`kubectl -n <namespace> get configauditreport <name> -o yaml`. Compare
`.report.checks[]` entries where `severity: HIGH` and `success: false` with the
workload's current manifest and owner. For a ReplicaSet, check desired replicas;
for a Job, check whether it is still active. Check the GitOps change that created
the workload before changing a chart value. The Grafana
`TrivyConfigAuditHighRise` rule compares each report with the previous day and
normalizes ReplicaSets to the active Deployment revision. It can still report a
new completed Job finding, so confirm workload state during triage. The raw
historical total is context, not an alert threshold.

## Scoped findings under review

| Workload/check | Why it may be required | Scope of exception |
| --- | --- | --- |
| Cilium agent `AVD-KSV-0005` (SYS_ADMIN) and `AVD-KSV-0009` (host network) | Cilium's [system requirements](https://docs.cilium.io/en/stable/operations/system_requirements/) require kernel eBPF installation and the host network namespace for the agent. | Treat only these checks on the Cilium agent as expected for the current CNI deployment. Do not exempt its writable root, extra capabilities, or other Cilium workloads by association. |
| Falco DaemonSet host mounts used for kernel event collection | Falco's [modern eBPF guidance](https://falco.org/docs/concepts/event-sources/kernel/) requires kernel access and specific capabilities. | Review the exact mounts and capabilities against that minimum. The current `AVD-KSV-0017` privileged finding is **not** accepted as necessary merely because Falco needs kernel access; test least-privileged chart settings before granting an exception. |

These entries explain known checks; they do not suppress reports or the
new-finding alert. Revisit them when the chart, driver, node OS, or CNI mode
changes. Longhorn's privileged/storage-host checks also need a separate
component-by-component review; no blanket exception is recorded for them.
