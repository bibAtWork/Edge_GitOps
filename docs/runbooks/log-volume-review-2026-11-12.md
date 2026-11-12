# Log-volume review, 2026-11-12

VictoriaLogs was queried directly through the Kubernetes Service proxy. Numbers
below are `sum_len(_msg)`: **uncompressed message text ingested**, not compressed
VictoriaLogs disk growth or the size of Talos audit files. The 24-hour window
contained 4,983.6 MB across 4,717,187 records. Kubernetes API audit events
accounted for 4,745.5 MB (95.2%); application/container logs for 238.1 MB.

The 24-hour total is **not the current daily run rate**. The OTel audit-body
deduplication in #1243 had been running for only about 22 minutes when this
review was made. In a wholly post-rollout 10-minute window, all logs totalled
3.525 MB and audit logs 1.922 MB. Multiplication by 144 gives a provisional
508 MB/day total and 277 MB/day audit, subject to workload variation. This is
an estimated 90% reduction in raw message text; confirm with a full 24-hour
window before treating it as an observed daily saving. Retention stays at 90
days.

## Top 20 producers of the historical 5 GB/day audit volume

The table groups the audit stream by Kubernetes identity, verb and resource.
It covers 2,037.0 MB, or 40.9% of all message text in the historical 24-hour
window. Other audit actions, notably additional leader-election Leases,
account for most of the remainder. `Lease update` across all identities was
1,581.4 MB/day. Values are decimal MB and rounded; `Low` means successful
routine events are of limited investigative value, not that failures or
unexpected identities are unimportant.

| # | Identity and operation | MB/24h | Importance | Reduction potential |
|---:|---|---:|---|---|
| 1 | Kyverno cleanup: create SelfSubjectAccessReview | 541.6 | Medium: permission decisions matter, repeated successful probes do not | High: 15m TTL reconcile instead of 1m |
| 2 | Longhorn CSI: update Lease | 111.6 | Low for normal renewals | High if successful renewals can be filtered precisely |
| 3 | Kyverno reports: update PolicyReport | 111.0 | High when findings change | Low until no-op writes are distinguished |
| 4 | Kyverno reports: get PolicyReport | 109.6 | Low for expected reads | Medium; keep failures and unauthorized reads |
| 5 | Kyverno reports: list EphemeralReport | 109.4 | Low for expected polling | Medium; keep failures and unauthorized reads |
| 6 | Longhorn manager: get Lease | 82.4 | Low for leader checks | Medium |
| 7 | Kyverno admission: update Lease | 81.6 | Low for normal renewals | Medium |
| 8 | Envoy Gateway: update Lease | 73.0 | Low for normal renewals | Medium |
| 9 | VM operator: update Lease | 70.9 | Low for normal renewals | Medium |
| 10 | Cilium: update Lease | 70.4 | Low for normal renewals | Medium |
| 11 | Barman plugin: update Lease | 69.4 | Low for normal renewals | Medium |
| 12 | System upgrade controller: update Lease | 69.0 | Low for normal renewals | Medium |
| 13 | Kyverno background: update Lease | 68.9 | Low for normal renewals | Medium |
| 14 | Longhorn manager: update Lease | 68.4 | Low for normal renewals | Medium |
| 15 | Cilium operator: update Lease | 68.3 | Low for normal renewals | Medium |
| 16 | Kyverno reports: update Lease | 68.0 | Low for normal renewals | Medium |
| 17 | Kyverno cleanup: update Lease | 67.9 | Low for normal renewals | Medium |
| 18 | CloudNativePG operator: update Lease | 66.4 | Low for normal renewals | Medium |
| 19 | Screener PostgreSQL: update Lease | 65.7 | Low for normal renewals | Medium |
| 20 | SeaweedFS PostgreSQL: update Lease | 63.5 | Low for normal renewals | Medium |

## What remains noisy after the audit-body fix

Current ranking by **uncompressed message bytes in a 10-minute sample** mixes
container sources with audit actions. This is a snapshot, not a 24-hour ranking.
The sample was taken after the OTel agent restarted with #1243. A container
name can cover multiple short-lived Pods. Routine probe logs are cheap per
event but frequent; scanner output may carry diagnostic or report content and
must not be blindly dropped.

| # | Producer | kB/10m | Importance | Reduction potential / next check |
|---:|---|---:|---|---|
| 1 | Trivy `wait-for-buckets` | 291 | Medium | Investigate encoded scan output before filtering |
| 2 | Trivy `create-buckets` | 145 | Medium | Investigate scan-job output and retries |
| 3 | Kyverno cleanup SelfSubjectAccessReview | 144 | Medium | High API-call saving from 15m TTL reconcile |
| 4 | Cilium agent | 119 | High for errors, low for routine info | Review info-level repetition |
| 5 | Trivy `main` | 105 | High for scan failures | Review scan-job output |
| 6 | Trivy `wait` | 101 | Medium | Review repeated wait messages |
| 7 | Trivy `init` | 101 | Medium | Review repeated initialization messages |
| 8 | Kyverno reports: list EphemeralReport | 86 | Low for expected reads | Filter only after checking error/status mix |
| 9 | Trivy `configure-collections` | 83 | Medium | Review repeated setup output |
| 10 | Grafana | 82 | Medium for errors, low for successful queries | Consider reducing successful plugin-request logs |
| 11 | VM operator | 77 | **High**: a repeated Deployment spec diff was observed | Fix apparent reconcile loop before changing log level |
| 12 | Flux manager | 62 | High for failures | Inspect recurring messages before filtering |
| 13 | Kyverno reports: get PolicyReport | 58 | Low for expected reads | Status-aware filtering could help |
| 14 | Zot registry | 58 | Medium for failures, low for `/livez` HTTP 200 | Exclude successful health probes at source if supported |
| 15 | Kyverno reports: update PolicyReport | 57 | High when findings change | Keep until no-op writes identified |
| 16 | Longhorn CSI: update Lease | 46 | Low for routine renewal | Status-aware filtering could help |
| 17 | Backup controller | 46 | High for workflow failures | Keep failures and recovery evidence |
| 18 | Falco | 45 | High security value | Low; do not suppress detections |
| 19 | Cilium: update Lease | 42 | Low for routine renewal | Status-aware filtering could help |
| 20 | OTel agent | 41 | Medium for ingest errors | Reduce only repetitive file-watch info |

The VM operator repeatedly logged a VLSingle Deployment diff of
`spec.revisionHistoryLimit: 3 -> 10`. Kyverno's
`set-revision-history-limit` policy mutates Deployments asking for 10 back to
3. This looks like two controllers fighting over one field; verify the
correlation and fix ownership before suppressing the operator log. The Trivy
scanner's `wait-for-buckets` and `create-buckets` logs are currently the largest
single container streams. They came from short-lived vulnerability-scan Jobs;
some sample lines are encoded, so their contents need inspection before any
drop rule.

The cleanup controller's TTL permission checker made 384,480 successful
SelfSubjectAccessReviews in the historical day. Kyverno's chart default is 1m,
and upstream documents this API load as adjustable with
`features.ttlController.reconciliationInterval`. A change to 15m should remove
roughly 14/15 of these calls if the workload is stable: about 359,000 API
requests per day. With compact audit messages already live, the additional
raw-message saving is now around 19 MB/day rather than the historical 506
MB/day. The scheduled DeletingPolicies use their own cron expressions.

Do not use a broad audit `None` rule for all Lease or authorization-review
operations: it would also hide denied requests and unexpected principals.
If the post-rollout full-day volume remains too high, filter only verified
successful routine operations in the OTel pipeline while preserving errors,
creates/deletes, unknown identities and the original security-sensitive
audit policy. That reduces VictoriaLogs ingestion but not Talos's local audit
file writes; quantify both before changing either.

Reproduce the measurements using LogsQL against VictoriaLogs:

```text
_time:24h | stats count() as events, sum_len(_msg) as bytes
_time:24h kind:Event | stats by (user.username, verb, objectRef.resource, objectRef.subresource) count() as events, sum_len(_msg) as bytes | sort by (bytes) desc | limit 20
_time:10m | stats by (kubernetes_namespace_name, kubernetes_container_name, kind) count() as events, sum_len(_msg) as bytes | sort by (bytes) desc | limit 30
```
