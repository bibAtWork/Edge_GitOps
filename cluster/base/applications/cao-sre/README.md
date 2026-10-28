# CAO SRE PoC for real-estate-screener

This PoC is independent of the existing `sre-agent` API, Holmes worker,
proposer, dashboard, scan and namespace. It has its own `cao-sre` namespace,
ServiceAccount, network policy, state volume and SOPS-encrypted model and GHCR
pull Secrets. The new Secrets were encrypted from the corresponding live
credentials without printing or storing plaintext in this repository. Rotating
those credentials later requires updating these Secrets too.

The two encrypted manifests have a manual rotation owner. On a trusted admin
machine with the cluster's offline age identity, use `sops set --value-stdin`
to replace `stringData.api-key` in `model-secret.yaml` and
`stringData[".dockerconfigjson"]` in `ghcr-pull-secret.yaml`. Pass a JSON-encoded
string on stdin and keep plaintext credentials outside this repository. Rotate
both the old and CAO copies when the shared upstream credential changes.

## Runtime

CAO 2.5.0 and OpenCode 1.19.4 are pinned in [Dockerfile](Dockerfile). CAO runs
as one non-root pod with a 2 GiB Longhorn state volume. It mounts the GitOps
profile, flow, runbook and Python collector from a generated ConfigMap. The
first profile is `screener-triage`; `screener-rca` is installed for manual crew
experiments. The PR member profile is present as a contract but is deliberately
not installed because it needs a separate pod and GitHub credentials.
OpenCode permits reading `/config` for the runbook and collector while denying
edits there; other external directories still require approval.

The built-in CAO scheduled flow runs every ten minutes. Its Python gate reads
only the `real-estate-screener` namespace and the application's internal health
endpoint. A symptom must appear twice before launch, and the same unresolved
symptom has a one-hour cooldown. A recovered failed Job is not an active signal
when a newer Job succeeded. The gate saves a bounded incident packet under
`/home/cao/incidents`, then CAO launches an OpenCode triage session. Reports and
handoffs are saved under `/home/cao/reports` and `/home/cao/handoffs`.

The incident packet and handoff format use `schema_version: 1`. The RCA profile
validates the triage handoff before use. A future PR member can consume an RCA
handoff in a separate runtime with a scoped GitHub credential. CAO profiles are
behavioral guidance; Kubernetes RBAC and runtime separation enforce permissions.
To try another harness later, add its pinned CLI to a worker image and set that
member's `provider` field to CAO's provider identifier. The handoff format does
not depend on OpenCode.

## Build and deployment

1. The included `.github/workflows/cao-sre-image.yml` builds the image and
   prints its immutable reference in the job summary. For a manual rebuild,
   the registry account must have package write access:

   ```sh
   docker buildx build --platform linux/amd64 -t ghcr.io/bibatwork/cao-sre:poc-1 .
   docker push ghcr.io/bibatwork/cao-sre:poc-1
   ```

2. The published digest is pinned in `workloads.yaml`; the Deployment uses the
   independent `ghcr-pull` Secret in `cao-sre` to pull it. Reconcile through
   Flux, which decrypts the SOPS Secrets. Do not directly
   `kubectl apply -k` the encrypted source: that would install ciphertext as
   the model key. Check the RoleBinding and UI:

   ```sh
   kubectl auth can-i list jobs -n real-estate-screener --as=system:serviceaccount:cao-sre:cao-sre
   kubectl auth can-i get secrets -n real-estate-screener --as=system:serviceaccount:cao-sre:cao-sre
   kubectl -n cao-sre rollout status deployment/cao-sre
   kubectl -n cao-sre port-forward service/cao-sre 9889:9889
   ```

   Open `https://cao-sre.homelab.data-harness.org` through the cluster Gateway.
   It uses the shared Keycloak OIDC client and OPA's app-operator/platform-admin
   gate, including for the WebSocket terminal. The port forward remains useful
   for local administration at `http://localhost:9889`. CAO trusts the exact
   Gateway hostname; its WebSocket client IP filter accepts the Gateway's
   changing pod IPs, so the Gateway authentication and Cilium ingress policy
   are the external access boundary. Do not expose the Service directly.

3. In the UI, launch `screener-triage` with a manual prompt, or check
   `cao schedule list` in the pod. To rehearse the historical recovered
   collector failure, prompt the agent to compare the failed Job with the most
   recent successful Job; it should identify recovery rather than claim an
   ongoing outage. Do not trigger a synthetic production failure for this test.

## Scope and future crew

The first step is triage, not remediation. The agent can list and read selected
Kubernetes objects, check application health, and examine bounded redacted error
lines. It has no Secret, exec, patch, delete, GitHub, or deployment permission.
The incident packet includes ReplicaSet revisions and image timestamps as a
change timeline. It does not yet query Flux reconciliation history or historical
VictoriaMetrics data; those can be added to this collector if the first incident
review shows a concrete gap.

For a crew, use `incident_id` and the versioned handoff file as the boundary:

1. Triage decides whether there is an active incident and records evidence.
2. RCA rechecks the evidence, tests hypotheses, and produces a cause analysis.
3. A future PR member receives only the validated RCA handoff and a Git checkout
   in an isolated pod with no Kubernetes token. It may open a draft PR, never
   merge or deploy. The orchestrator can then coordinate the stages, but each
   stage's credentials stay in its own runtime.

The `config` Flux inventory includes this Kustomization. Keep its image digest
pinned; a new build requires a digest update before Flux adopts it.
