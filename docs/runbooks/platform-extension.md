# Extending the homelab

The platform supplies routing/TLS, identity and authorization, observability,
storage, recovery, and workload policies. `cluster/base/platform` contains the
common platform composition; `cluster/base/applications` registers applications.
Both node profiles consume these compositions. Profile patches express topology.

## Application contract

1. Copy `cluster/templates/application` to `cluster/base/applications/<name>`.
   Replace every `example-app`, image, hostname, resource budget and health probe.
   The example is stateless and is not included in any live profile.
2. Keep the namespace, workload, ServiceAccount, RoleBinding, route and access
   policy together. Register the directory **once** in
   `cluster/base/applications/config/kustomization.yaml`: this layer runs after
   CRDs are ready and may contain ordinary Kubernetes resources too. Do not also
   register the same objects in the root application composition. Existing
   Immich/Paperless installations retain their root/config split for continuity.
3. Configure and test native OIDC or an application-specific Envoy OIDC
   SecurityPolicy. Shared gateway TLS and OPA do not automatically authenticate
   arbitrary browser applications. The template's gateway capability is `false`;
   change it to `true` only after authentication and authorization are configured.
   Use SOPS for client credentials. Register clients in Keycloak's realm config
   and use its existing role/group mapping; this shared identity registration is
   a platform change, not an edit to another application.

   Then say so on the route. OPA's last rule lets a request with no
   `Authorization` header through (browser apps sign users in themselves), so a
   route with neither a native login nor a gateway policy is public, and nothing
   would tell you. CI therefore fails an HTTPRoute that does not carry
   `gitops.homelab/auth`: `native-oidc` (the app signs in against Keycloak; add
   `gitops.homelab/auth-client` naming the realm client, whose redirect URI must
   be on the route's host), `gateway-oidc` (Envoy's OIDC filter, with the
   SecurityPolicy and the host listed in OPA's `admin_only_apps`, which must be
   exactly the `gateway-oidc` hosts), or `public` (deliberately open;
   `gitops.homelab/public-reason` must say why). `identity-provider`, `deny` and
   `redirect` cover Keycloak itself, its refused admin path and the HTTP-to-HTTPS
   redirect. Each claim is checked against what it names.
4. Set `homelab.local/telemetry-client: "true"` on instrumented pods. Set a stable
   `OTEL_SERVICE_NAME`. DNS, gateway ingress and OTLP egress are shared policies.
   Add explicit application policies for database, object store and peer access.
   Cilium allows are additive: an empty per-app policy cannot cancel a shared
   allow. The existing internet policy permits TCP/443 to any external address;
   it neither verifies TLS nor restricts destinations. Changing it to opt-in
   needs an inventory of existing platform and application outbound traffic.
5. Declare recovery intent. Stateless applications retain the namespace's
   `homelab.local/recovery: stateless` annotation and a reason. Before introducing
   persistent data, enroll it in the shared
   `37-backup-system/deferred/recovery-policy.yaml`, add its matching recovery-point
   CronWorkflow and dataset permissions, and complete an AWS restore drill.
   The annotation is documentation, not automatic enrollment in backup jobs.
6. Run `python scripts/check-platform-contracts.py` and existing CI checks. Verify
   unauthenticated/authorized/unauthorized access, telemetry, and recovery in the
   cluster before treating the service as ready.

Application operators receive built-in `edit` through local RoleBindings in
`immich` and `paperless`. New grants live with the new application. `edit` permits
Secret reads and running as namespace ServiceAccounts: application namespaces
must not hold privileged platform credentials or ServiceAccounts. Namespace
labels and cluster administration remain platform-admin responsibilities.

## Telemetry contract

All three signals use `otel-collector-gateway.monitoring.svc`:

| Transport | Endpoint | SDK protocol |
| --- | --- | --- |
| OTLP/HTTP | `http://otel-collector-gateway.monitoring.svc:4318` | `http/protobuf` |
| OTLP/gRPC | `http://otel-collector-gateway.monitoring.svc:4317` | `grpc` |

For the shared HTTP base endpoint, SDKs append `/v1/logs`, `/v1/metrics` and
`/v1/traces`. Signal-specific endpoint variables need those suffixes explicitly.
These are internal plaintext endpoints with network policy controls, without
tenant authentication. Callers can supply their own service identity. Do not
expose the collector through a public route or treat it as a tenant boundary.

Metrics use Prometheus remote write to VictoriaMetrics and require cumulative
monotonic sums/histograms; set
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` where supported.
Delta conversion and every SDK instrument type are not promised by this contract.
Logs and traces go to VictoriaLogs and VictoriaTraces. The node agent also accepts
all three signals for compatibility, but the gateway is the application endpoint.
Reloader rolls both collectors after configuration changes.

Send a known log, cumulative counter and span from a labelled application pod and
query all three stores. Check Hubble for denied connections and collector export
errors. Repeat without the label from a new application namespace to check the
intended policy restriction; existing broad same-namespace policies still apply.

## Profile guarantees and efficiency

`3-node` is a scale-out profile, **not an end-to-end HA guarantee**. SeaweedFS has
multiple masters/volumes/filers, but its filer PostgreSQL metadata cluster remains
a single instance on strict-local storage. Keycloak and Immich databases also
remain single-instance. Losing their storage node interrupts those services;
restoring or recovering the node is required. Multiple filers cannot compensate
for losing the filer database. Existing volumes are not converted by a replica
count or StorageClass edit.

Before promoting this profile to HA: confirm three independent schedulable
storage nodes, size database replicas, patch each CNPG cluster with explicit
anti-affinity and storage placement, validate backup/WAL recovery, then perform
node-loss and restore drills. Record measured RPO/RTO. This change preserves
existing data placement and makes no untested availability promise.

Hostmetrics and node-exporter collection remain enabled. Dashboards demonstrably
consume `node_*` series; removing either family without measuring its consumers
could remove useful signals. Measure idle/p95 CPU and memory by component,
samples/second, retained bytes and backup I/O for each profile. Consolidate unused
families only after checking dashboards/alerts and comparing a full operational
window. Helm's existing three-revision history cap and telemetry retentions remain.

## Reconciliation and migration

The root applies native objects, HelmReleases and bootstrap CRDs. It does not wait
for application readiness. `operators-ready` depends on root and checks controller
HelmReleases; `config` depends on that gate and applies custom resources. The
gate never waits on SeaweedFS, Immich or Zot, whose readiness needs config-layer
databases. VictoriaMetrics now depends on its actual storage provider Longhorn;
the obsolete SeaweedFS dependency would create a cycle. Barman explicitly waits
for CNPG and cert-manager.

The readiness marker's ConfigMap is owned by the gate, so the gate does not
compete for ownership of root HelmReleases. Gate `healthChecks` are intentionally
explicit: adding `wait: true` would override them. `config` retains `wait: true`,
which is a convergence check, not proof that Cilium traffic or restores work.

**Existing clusters require an inventory handoff before adopting this revision.**
The identity-only [handoff list](flux-config-handoff.yaml) contains resources moving
from root to config. Never `kubectl apply` that list: it intentionally omits specs.
Perform this from a trusted admin shell during a maintenance window:

1. Before making the new revision available to Flux, suspend both reconcilers:
   `flux suspend kustomization flux-system` and
   `flux suspend kustomization config`. Wait for any in-flight reconciliation to
   finish and confirm `spec.suspend` on both. Keep the old revision available.
2. Save `kubectl get -f docs/runbooks/flux-config-handoff.yaml -o yaml` outside Git
   as the handoff snapshot. Check every listed object exists, and record any
   pre-existing `kustomize.toolkit.fluxcd.io/prune` annotation. Missing objects or
   a failed command must be investigated before continuing.
3. Protect the live objects while root is still suspended:
   `kubectl annotate --overwrite -f docs/runbooks/flux-config-handoff.yaml kustomize.toolkit.fluxcd.io/prune=disabled`.
   Read the objects back and verify the annotation on **every** entry. This is
   necessary because root still has the old inventory when the new revision drops
   those objects; adding the annotation only in the new config manifests is too late.
4. Make the reviewed revision available, resume root, and reconcile it. Verify
   root's `status.lastAppliedRevision` is the new revision and its inventory no
   longer lists these objects. Check `operators-ready` becomes Ready. Resume and
   reconcile `config`; verify it applies the same revision and is Ready.
5. Verify each object's UID matches the snapshot and its Flux ownership label is
   `kustomize.toolkit.fluxcd.io/name=config`. Confirm databases and recovery jobs
   are healthy and the old `oidc-app-operator-edit` **ClusterRoleBinding** is gone.
   The namespace RoleBindings replace it; it is deliberately not protected from pruning.
6. Restore each object's original prune annotation. For entries originally without
   the annotation, remove it with `kubectl annotate <kind> <name> -n <namespace>
   kustomize.toolkit.fluxcd.io/prune-` (omit `-n` for cluster-scoped objects).
   Preserve any originally disabled annotation. Reconcile once more and retain
   the snapshot until recovery and connectivity checks pass.

If a step fails, keep the affected reconciler suspended and the protection in
place. Do not roll back paths blindly: transferring ownership back requires the
same protection/UID checks, with config as the old owner. Fresh clusters do not
need this handoff. Neither bootstrap recovery nor live migration was executed by
the repository validation.

References: [Flux health checks and dependencies](https://fluxcd.io/flux/components/kustomize/kustomizations/)
and [OpenTelemetry Collector configuration](https://opentelemetry.io/docs/collector/configuration/).
