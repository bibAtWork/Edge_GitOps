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

CAO 2.5.0, OpenCode 1.19.4, and Codex CLI 0.165.0 are pinned in
[Dockerfile](Dockerfile). CAO runs
as one non-root pod with a 2 GiB Longhorn state volume. It mounts the GitOps
profile, flow, runbook and Python collector from a generated ConfigMap. The
shared agent definitions in `profiles/` are rendered at startup with OpenCode
and Codex overlays. CAO installs `screener-triage` and `screener-rca` for
OpenCode, plus `screener-triage-codex` and `screener-rca-codex` for Codex.
The scheduled monitor starts a durable crew workflow: OpenCode triage followed
by Codex RCA. Both harness variants remain available for manual launches in
the same UI. A PR member is available as a suspended one-shot Job template;
it starts only when an operator supplies an actionable RCA and a scoped
GitHub credential.
OpenCode permits reading `/config` for the runbook and collector while denying
edits there; other external directories still require approval.

The built-in CAO scheduled flow runs every ten minutes. Its Python gate reads
only the `real-estate-screener` namespace and the application's internal health
endpoint. A symptom must appear twice before launch, and the same unresolved
symptom has a one-hour cooldown. A recovered failed Job is not an active signal
when a newer Job succeeded. The gate saves a bounded incident packet under
`/home/cao/incidents`, then enqueues it on the existing state volume. The
five-minute controller starts the `screener_crew` workflow, validates the
triage handoff before RCA, and validates the RCA handoff afterward. Reports
and handoffs are saved under `/home/cao/reports` and `/home/cao/handoffs`.
Only one scheduled crew run is active at a time.
The gate also reads `/health/ingestion`: it investigates a confirmed low-yield
search or at least two exact plot-extraction mismatches among 20 labeled recent
ImmoScout details. These are aggregate counts, not listing contents. Grafana
sends one warning after 30 more minutes; CAO keeps its existing two-sample gate
and cooldown to avoid duplicate investigations.

The incident packet and handoff format use `schema_version: 1`. The RCA profile
validates the triage handoff before use. A future PR member can consume an RCA
handoff in a separate runtime with a scoped GitHub credential. CAO profiles are
behavioral guidance; Kubernetes RBAC and runtime separation enforce permissions.
The collector records GET receipts for seven required Kubernetes resource
lists and both application health endpoints. The controller checks these
receipts before starting an agent, and the workflow checks them on replay.
They describe what the PoC collector returned; they are not tamperproof audit
logs and do not cover API calls an agent makes independently.

CAO's workflow journal replays completed steps after interruption. The
controller waits five minutes after a recorded connection failure or five
hours after a usage-window failure, then resumes the journaled run. It allows
three attempts total. Unknown failures, missing journal state, and runs still
active after 45 minutes go to `needs_review` without spawning a duplicate.
Records live in `/home/cao/crew-queue`.
To add another harness, add its pinned CLI and a small profile overlay. The
handoff format does not depend on the harness. Codex uses a named CLI config
profile with no interactive approvals. The pod's `RuntimeDefault` seccomp policy
blocks Codex's nested bubblewrap sandbox, so Codex runs commands without its
own sandbox inside the constrained pod. Kubernetes RBAC, read-only mounts,
non-root UID, and network policy are the enforced boundary. CAO's `allowedTools`
list is omitted from Codex profiles because those CAO tool names do not map to
Codex tools and CAO cannot enforce this list for Codex. The same pod user can
access both harnesses' credentials.

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

4. To use the Codex variants with a ChatGPT subscription, sign in once inside
   the pod with `kubectl -n cao-sre exec -it deploy/cao-sre -- codex login
   --device-auth`, then check `kubectl -n cao-sre exec deploy/cao-sre -- codex
   login status`. Device-code login must be enabled in the ChatGPT account or
   workspace. The login cache lives in `/home/cao/.codex` on the persistent
   volume and contains sensitive tokens; do not put it in GitOps or a ConfigMap.
   You can then select `screener-triage-codex` or `screener-rca-codex` in CAO.
   The pod's 2 GiB memory limit covers all concurrent sessions, so start with
   one active Codex session while observing memory use.

5. Inspect automatic crew state with `cao schedule list`, `cao workflow runs`,
   and the JSON records in `/home/cao/crew-queue`. A `needs_review` record
   requires an operator to inspect the CAO journal; the controller does not
   automatically reset it.

## Isolated draft PR worker

The suspended `cao-sre-pr-worker` CronJob is a template for one-shot Jobs.
It has an ephemeral workspace, no Kubernetes service-account token, and egress
limited to DNS plus HTTPS. It has no screener namespace RBAC. The worker
receives the model key and a separate, repository-scoped GitHub token. Create
a fine-grained token restricted to `bibAtWork/Edge_GitOps` with Contents and
Pull requests write access, and store it in Secret `cao-sre-pr-github` as key
`token`. GitHub requires the account owner to create that token in settings;
never put its plaintext value in Git or chat.

Only launch this Job after reviewing an actionable RCA. The RCA handoff must
set `pr_candidate: true`, specify a concrete `proposed_change`, and include
an evidenced hypothesis. Copy its two artifacts locally, create the input
ConfigMap, then launch one Job:

```sh
kubectl -n cao-sre cp <cao-pod>:/home/cao/handoffs/<incident-id>-rca.json ./rca.json
kubectl -n cao-sre cp <cao-pod>:/home/cao/reports/<incident-id>-rca.md ./rca-report.md
kubectl -n cao-sre create configmap cao-sre-pr-handoff \
  --from-file=rca.json=./rca.json --from-file=rca-report.md=./rca-report.md \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n cao-sre create job cao-sre-pr-<short-id> \
  --from=cronjob/cao-sre-pr-worker
```

The entrypoint rejects an RCA without a supported PR candidate. It clones
the GitOps repository, starts `screener-pr-draft` in its own CAO session,
and may open a draft PR. A healthy or uncertain RCA does not trigger this
stage. The repository has an active PR ruleset on `ops/talos_linux`, but that
ruleset includes an administrator bypass. A token owned by a bypassing account
could push or merge despite the profile instruction; review the credential's
owner and remove that bypass before treating draft-only behavior as enforced.

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
3. The PR member receives an operator-selected RCA handoff and Git checkout
   in a one-shot pod with no Kubernetes token. It may open a draft PR; the
   operator decides whether to merge or deploy.

The `config` Flux inventory includes this Kustomization. Keep its image digest
pinned; a new build requires a digest update before Flux adopts it.
