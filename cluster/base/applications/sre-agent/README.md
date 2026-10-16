# SRE Agent deployment

Source and all image builds: https://github.com/bibAtWork/sre-agent.
This directory contains production deployment configuration. It is registered
once in applications/config and consumed by both 1-node and 3-node profiles.

Dashboard: https://sre-agent.homelab.data-harness.org/ (after rollout).
Envoy signs users in through Keycloak and forwards their access token to OPA.
Only app-operator/platform-admin users may access incident evidence. The route
points to the separate read-only dashboard port 8081; no control or worker routes
are exposed. The API on 8080 requires `agent-auth/API_TOKEN` for every route except
its health probe. Treat port-forward access to the dashboard as privileged.

## Private credentials

The source repository and images are private. Keep a classic GitHub PAT with
`read:packages` and read access to all four application packages, plus the Gemini
API key, in files outside this repository. The encrypted manifests are retained
in Git and use the cluster's offline age identity; there is no runtime bootstrap
script. To rotate a value, set `SOPS_AGE_KEY_FILE` to that identity on a trusted
admin machine and pass a JSON-encoded value to `sops set --value-stdin`. This
updates the ciphertext without putting the value in command arguments or writing
plaintext YAML to the repository.

For the GHCR token, build the Docker config from the token file and update its
encrypted field (PowerShell example; substitute your username and file path):

```powershell
$env:SOPS_AGE_KEY_FILE = 'C:\path\to\offline\age.key'
$token = (Get-Content -LiteralPath 'C:\path\to\ghcr-token.txt' -Raw).Trim()
$auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("YOUR_GITHUB_USERNAME:$token"))
$config = @{auths = @{'ghcr.io' = @{auth = $auth}}} | ConvertTo-Json -Compress -Depth 4
$config | ConvertTo-Json -Compress | sops set --value-stdin cluster/base/secrets/ghcr-pull-secret.yaml '["stringData"][".dockerconfigjson"]'
Remove-Variable token, auth, config
```

For Gemini, send only the key value (the local file's `project` field is not a
Kubernetes Secret field). For the API's bearer token, generate a fresh random
value when rotating it:

```powershell
$keyLine = Get-Content -LiteralPath 'C:\path\to\gemini-key.txt' | Where-Object { $_ -match '^api-key:\s*' } | Select-Object -First 1
if (-not $keyLine) { throw 'Gemini file has no api-key field' }
($keyLine -replace '^api-key:\s*', '').Trim() | ConvertTo-Json -Compress | sops set --value-stdin cluster/base/secrets/google-gemini-api-key.yaml '["stringData"]["api-key"]'
python -c 'import json,secrets; print(json.dumps(secrets.token_urlsafe(32)))' | sops set --value-stdin cluster/base/applications/sre-agent/agent-auth-secret.yaml '["stringData"]["API_TOKEN"]'
```

Rotate only the value that changed. Verify the token can read all four private
GHCR packages before committing a replacement, then verify fresh Pods can pull
their images after rollout. Never use the Flux commit-status token for pulls.
The GHCR credential is stored once in `../../secrets/ghcr-pull-secret.yaml`;
the two Kustomizations render it into their own namespaces.
The Gemini key is stored in `../../secrets/google-gemini-api-key.yaml` and
referenced by this application's Kustomization.
Commit only encrypted YAML.

| Secret | Required key/type | Purpose |
| --- | --- | --- |
| agent-auth | API_TOKEN (random, at least 24 characters) | API, workers, scan trigger |
| google-gemini-api-key | api-key | Gemini API for both workers |
| ghcr-pull | .dockerconfigjson; kubernetes.io/dockerconfigjson | read access to private ghcr.io/bibatwork/sre-agent-* images |

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

## Network troubleshooting

A browser error saying "upstream connect error ... remote connection failure"
means the gateway could not establish its upstream connection; successful SSO
does not prove that the dashboard listener or Service endpoints are reachable.

Check agent-api Pods, the sre-agent-dashboard EndpointSlice (ready addresses on
8081), and a local HTTP request from inside agent-api to 127.0.0.1:8081. If that
works, compare a port-forward to the Service with the gateway path, then inspect
Hubble drops and Envoy upstream details. Distinguish refused connections from
timeouts before changing policy. Shared gateway rules already permit proxy
connections to backend pods; validate that they are reconciled in the cluster.

The new namespace is absent from shared same-namespace and Internet HTTPS allow
lists. Local policies therefore explicitly allow callers to agent-api:8080,
allow API ingress from namespace peers, and allow only worker pods to world:443.
DNS and gateway ingress still come from the shared policies. This restores scan
and investigation traffic but is not evidence of the cause of a dashboard 503.
