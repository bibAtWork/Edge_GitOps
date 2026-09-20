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
`kubeopencode-system`, `local-path-storage`, and `cert-manager`. These are
candidates for individual observation, not approval for a blanket reduction.
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
