# SRE Agent deployment

Source and all image builds: https://github.com/bibAtWork/sre-agent.
This directory contains production deployment configuration only. It is registered
once in applications/config and consumed by both 1-node and 3-node profiles.

Dashboard: https://sre-agent.homelab.data-harness.org/ (after rollout).
Envoy signs users in through Keycloak and forwards their access token to OPA.
Only app-operator/platform-admin users may access incident evidence. The route
points to the separate read-only dashboard port 8081; no control or worker routes
are exposed. The API on 8080 requires `agent-auth/API_TOKEN` for every route except
its health probe. Treat port-forward access to the dashboard as privileged.

## Before merge

The source repository is private; provision the following externally managed
Secrets in namespace `sre-agent` through your secret manager. For Git-managed
Secrets, use SOPS and add a bootstrap generator/inventory entry as required by
`docs/runbooks/platform-extension.md`; do not commit plaintext credentials.
No placeholder Secrets or keys are included in this PR:

| Secret | Required key/type | Purpose |
| --- | --- | --- |
| agent-auth | API_TOKEN (random, at least 24 characters) | API, workers, scan trigger |
| agent-gemini | api-key | Gemini API for both workers |
| ghcr-pull | .dockerconfigjson; kubernetes.io/dockerconfigjson | read access to private ghcr.io/bibatwork/sre-agent-* images |

Create the namespace before provisioning the Secrets if it does not yet exist.
The images are pinned to published manifest digests from source commit
980341d07505d0449ce3ea241093b76e27442904; all three builds passed and published. The GitHub connector's authorization is not a GHCR pull credential.
Verify DNS resolves the hostname to the existing homelab Gateway. The HTTPS route
uses its existing certificate; this change does not create a second ingress or
TLS issuer. Keycloak reconciles the shared client's new redirect URI through the
existing realm-config workflow. Confirm that reconciliation has completed before
trying browser login. No Kubernetes API access is configured in this workspace.

## Scope, resources and retention

`WATCH_NAMESPACE=real-estate-screener` matches the read-only Role and RoleBinding.
Change all three together to watch a different namespace. Holmes can inspect
workloads, events and bounded pod logs, with no Secrets, exec or write grants.
OpenCode has no Kubernetes token; it reads Edge_GitOps's public ops/talos_linux
branch and produces a patch only after a human requests it. AUTO_PROPOSE is false.

The hourly CronJob creates investigations automatically. There is one shared
execution slot, a 600s agent timeout and a 1200s claim lease. There is no automatic
model fallback, retry budget or guarantee of free Gemini usage.

The three deployments request 250m CPU and 576Mi RAM; their configured limits total
2.5 CPUs and 1792Mi RAM. The hourly trigger adds up to 100m/64Mi. No Ollama or GPU.
These are budgets, not measured runtime consumption. Workers use bounded disk
scratch space; the single-replica SQLite API uses a 2Gi Longhorn PVC with fsGroup.

The PVC is deliberately a temporary investigation cache, explicitly excluded
from disaster recovery. Disk loss discards historical reports, proposed patches
and pending work; fresh scans can regenerate current evidence. Export artifacts
that need retention. This MVP does not provide durable audit history or HA.

## Rollout checks

```sh
kubectl -n sre-agent get deployments,pods,pvc
kubectl -n sre-agent rollout status deployment/agent-api
kubectl -n sre-agent rollout status deployment/investigator
kubectl -n sre-agent rollout status deployment/proposer
kubectl -n sre-agent get httproute sre-agent -o yaml
kubectl -n sre-agent get securitypolicy sre-agent-oidc -o yaml
kubectl auth can-i list pods -n real-estate-screener --as=system:serviceaccount:sre-agent:investigator
kubectl auth can-i get secrets -n real-estate-screener --as=system:serviceaccount:sre-agent:investigator
kubectl auth can-i patch deployments -n real-estate-screener --as=system:serviceaccount:sre-agent:investigator
kubectl -n sre-agent create job --from=cronjob/workload-scan sre-agent-first-scan
```

The last two authorization checks must answer no. Confirm a report appears, then
validate browser login with an allowed group and denial with a viewer account.
Check Hubble for blocked API/model flows and worker logs for execution failures.
This manifest uses stable APIs; Kubernetes 1.37 runtime compatibility and live
Gemini tool calling still require validation on the actual cluster.

## Alerts and proposals

Webhook endpoint inside the cluster:
`http://agent-api.sre-agent.svc.cluster.local:8080/v1/alertmanager`.
Configure the monitoring sender with `Authorization: Bearer <API_TOKEN>` and the
Alertmanager-compatible `alerts` payload. Only firing alerts with label
`namespace=real-estate-screener` are accepted. The monitoring namespace has an
explicit Cilium allow to port 8080. Automatic alert sender configuration is not
included; the hourly workload scan operates independently.

Use the app repository's API runbook to port-forward agent-api:8080, inspect
`GET /v1/incidents`, and request `POST /v1/incidents/<id>/propose` with your bearer
token after reviewing the report. Proposed patches are available in the dashboard
or authenticated API. They are never committed, merged or applied automatically.
Restart deployments after configuration/key changes if Reloader has not already
rolled them. Roll back by restoring the previous verified source commit's image
references; removing the application does not deliberately delete its PVC data.
