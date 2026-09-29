# Resource rightsizing

Use the provisioned **Resource efficiency** Grafana dashboard for every sizing
change. Select at least seven complete days that include backup, scanning and
upgrade activity. Do not size from an idle snapshot.

## Decision rules

1. Compare CPU request with seven-day p95 usage by namespace and workload. CPU
   is compressible, so reduce a request only when p95 remains below 50% of it and
   the workload has no latency or startup regression.
2. Compare memory request with seven-day p95 working set and seven-day maximum.
   Keep at least 25% above p95 and never set a limit below the observed maximum.
   Memory is not compressible; an optimistic reduction becomes eviction or OOM.
3. Change one workload at a time. Keep the old value in the PR description,
   observe one full operational week, then accept or revert it.
4. Treat Jobs separately from steady controllers. Backup, recovery, scanning and
   upgrade Jobs may be absent from an instant query while still defining the
   capacity the cluster needs.
5. Record the query window and before/after p95 in the commit or PR. A resource
   edit without measurement is not a rightsizing change.

## 2026-09-20 baseline

The first seven-day measurement established:

| Resource | Seven-day p95 | Requests | Limits | Decision |
| --- | ---: | ---: | ---: | --- |
| CPU | 1.282 cores | 7.178 cores | 10 cores | Requests have headroom; inspect per workload before reducing. |
| Memory | 13.85 GiB | 16.22 GiB | 50.89 GiB | Keep requests; aggregate margin is only about 17%. |

The largest CPU request-to-p95 ratios were in `cattle-system`, `security`,
`kubeopencode-system` (retired 2026-09-27), `local-path-storage`, and
`cert-manager`. The remaining namespaces are candidates for individual
observation, not approval for a blanket reduction.
Several namespaces already use more memory than they request, so cluster-wide
memory request reduction is explicitly rejected by this baseline.

Repeat the baseline after adding a service or changing the node profile. Use:

```promql
quantile_over_time(0.95,
  sum(rate(container_cpu_usage_seconds_total{container!="",image!=""}[5m]))[7d:5m])
```

```promql
quantile_over_time(0.95,
  sum(container_memory_working_set_bytes{container!="",image!=""})[7d:5m])
```

Compare them with `sum(kube_pod_container_resource_requests{resource="cpu"})`
and `sum(kube_pod_container_resource_requests{resource="memory"})` respectively.

## 2026-09-29 live commitment snapshot

The earlier baseline is a seven-day *usage* measurement. To compare the
resources held by today's pods with node allocatable capacity, run:

```sh
python3 scripts/resource-overcommit.py
python3 scripts/resource-overcommit.py --usage
```

The report reads scheduled, nonterminal Pods from the Kubernetes API. It counts
application containers, restartable init containers, the peak of ordinary init
containers, and Pod overhead. It excludes finished Pods, whose resource series
can linger in VictoriaMetrics, and prints `kubectl describe node` accounting as
a cross-check. On this single-node profile, the 2026-09-29 18:49 UTC snapshot
agreed with that node's reported totals:

| Resource | Requests | Limits | Allocatable | Requests / allocatable | Limits / allocatable |
| --- | ---: | ---: | ---: | ---: | ---: |
| Memory | 12.28 GiB | 39.70 GiB | 30.43 GiB | 40% | 130% |
| CPU | 5.05 cores | 10.10 cores | 11.95 cores | 42% | 85% |

The memory limit total is a ceiling if containers peak together, not measured
usage or an imminent OOM. The old 121% and 164% figures were produced on
different dates with metrics that could include finished Pods or duplicate
scrapes; neither is a comparable live commitment figure. This snapshot changes
as workloads roll or settings change.
For a multi-node profile, compare each node separately before judging placement
headroom; a cluster-wide sum can hide one full node.

`--usage` adds seven-day per-Pod/container p95 and maximum memory use. It keeps
Pods separate even when containers share the same name, and reports containers
without usage samples as unknown rather than as unused capacity. Treat its
limit-to-peak gaps as candidates for the decision rules above, never as an
automatic reduction budget. The complete seven-day baseline still needs a
window that includes backup, scanning and upgrade activity.
