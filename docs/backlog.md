# Backlog

Known open issues that aren't yet fixed. Not a full project backlog — just things worth not forgetting.

Closed items are removed rather than kept here once resolved — this file tracks what's still
open, not project history. The investigation trail for anything that used to be here is fully
preserved in git: PR descriptions and commit messages for the OPA `ExternalAuth`→`SecurityPolicy`
migration, the Envoy Gateway cutover (ADR-001), and the CNCF IAM role mapping are all still
readable via `git log`, even though the working narrative is no longer duplicated here.

---

## Security review findings — see local `security-review.md`, not this file

A dated security review exists at `docs/security-review.md`, gitignored and **never committed** —
it names live, unremediated weaknesses in a public repository, which must not be published
regardless of severity. Do not copy findings, resource names, or exploit specifics from it into
this file, commit messages, or PR descriptions.

As of the last review (2026-08-14), fixes exist for every finding except the two explicitly
deferred ones (Kubernetes-native RBAC via Talos OIDC; the `schenkmatch:latest` CI gate, both
still tracked below) — H3 (kube-proxy/flannel disabled) was fixed live in-session; C1, H1, H2,
M1 (partial), M2, M3, M4, L1, and L2 each have an open PR against `ops/talos_linux` pending
review/merge. Ask to see the actual review locally if you need the specifics — they're
intentionally not reproduced here.

---

## Trivy CVE gate auto-merges non-zero-CVSS images, and runs 5x redundantly per PR

Found 2026-08-16 while reviewing the first two Renovate PRs to land after the `kubernetes` manager was enabled (#131/#132) — [`chore(deps): update rancher/local-path-provisioner docker tag to v0.0.37`](../../pull/134) and [`chore(deps): update amazon/aws-cli docker tag to v2.36.24`](../../pull/133).

**The gate's own comment on #134**:

| | Image | Max CVSS (CRITICAL+HIGH) |
|---|---|---|
| Current | `rancher/local-path-provisioner:v0.0.36` | 8.8 |
| Proposed | `rancher/local-path-provisioner:v0.0.37` | **8.2** |

Auto-merged anyway — rule **3c** is "update doesn't worsen CVE posture" (`old_cvss=8.8 new_cvss=8.2`), not "CVE posture is acceptable." An image can auto-merge while still carrying a HIGH-severity CVE indefinitely, as long as each successive update is monotonically no-worse than the last. `local-path-provisioner` is a meaningful one to catch this on: it's the cluster's only real `StorageClass`, runs in the one namespace forced to PSS `privileged` (see `docs/network-architecture.md`), and backs all 17 app PVCs (Immich, Keycloak, Paperless, Grafana, VictoriaMetrics, Zot, KubeOpenCode). `aws-cli`'s companion PR (#133), by contrast, went `9.1 → 0` — a real fix — so the gate does work correctly when a clean version exists; it's specifically the "stuck at HIGH forever" case that isn't distinguished from "used to be worse, now merely bad."

**Separately**: `trivy-gate` ran **5 times** for the identical commit on both PRs (confirmed via `check-runs` — 5 runs within a ~2-second window, `2026-08-16T21:23:10Z`–`21:23:12Z` on #134), each posting its own comment. Pure noise today (all 5 agreed), but redundant Actions minutes on every single Renovate PR and a latent risk if a future 6th run ever disagreed with the other 5 (which comment would `gh pr checks` / the merge decision honor?). Root cause not yet investigated — likely duplicate workflow triggers (e.g. both `pull_request` and `pull_request_target`, or Renovate's automerge polling re-triggering the check) rather than anything content-dependent.

**Fixed, pending review**: [#144](../../pull/144). Policy chosen: keep auto-merging when 3c
passes (a clean version may not exist yet), but label the PR `cvss-high` when the result is
still ≥7.0 CVSS so it stays visible instead of disappearing into the merged-PR list unflagged.
The 5x trigger turned out to be Renovate applying labels via separate API calls, each firing
`labeled` independently — fixed with a `concurrency` group scoped to the PR number
(`cancel-in-progress: true`), not a `types:` change (rejected as riskier: could stop the gate
from firing at all if Renovate's labeling order ever differs from what's assumed).

---

## `schenkmatch:latest` bypasses the "No :latest image tags" CI gate

Found 2026-08-16 while auditing Renovate coverage. `22-schenkmatch/deployment.yaml` runs `ghcr.io/bibatwork/schenkmatch:latest`. The `gitops-lint.yml` job meant to catch exactly this (`grep -E '^\s+image:\s+\S+:latest(\s|$)'` against rendered kustomize output) has a regex bug: it requires the line to start with literal `image:` after whitespace, but real container specs render as list items (`- image: ...`), so the leading `-` breaks the match. Confirmed live: `kubectl kustomize` output contains `      - image: ghcr.io/bibatwork/schenkmatch:latest` verbatim, and the check passes anyway.

Fixing the regex alone (`^\s*-?\s*image:\s+\S+:latest(\s|$)`, tested against the real rendered output and confirmed to catch this without new false positives) would make the gate start **failing on every future PR repo-wide** the moment it's merged, since `schenkmatch` has never published a tagged release — checked its GitHub tags via the API: none exist, only `:latest`.

**Needs a decision before the regex gets fixed**, not just the regex fix itself:

1. Pin `schenkmatch`'s deployment to a specific image **digest** (`ghcr.io/bibatwork/schenkmatch@sha256:...`) instead of a tag — makes it reproducible today, and Renovate's `docker` datasource can track digest updates even without semver tags.
2. Or add a real release/tagging pipeline to the `schenkmatch` repo itself (separate repo, not this one).
3. Only then fix the CI regex, or the fix will immediately red-X unrelated PRs.

---

## Kubernetes-native RBAC (`kubectl`/`kubeoc` CLI access) has no identity layer

Every app behind the Gateway now has real per-user auth (Keycloak OIDC, either native or via Envoy Gateway's `SecurityPolicy.oidc`) or OPA's coarser Rego gate. `kubectl`/`kubeoc` access to the API server itself is still cert-based only — no OIDC trust configured at all. Scoped 2026-08-12 against the CNCF IAM whitepaper's "Administrator" actor: requires `cluster.apiServer.extraArgs` (`oidc-issuer-url`/`oidc-client-id`/`oidc-groups-claim`) in `cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml` (currently absent), applied via `talosctl apply-config` — a brief kube-apiserver restart on this single control-plane node. Cert-based admin access stays untouched either way, so it remains the safety net regardless of when/whether this lands. Not started.

---

## Envoy Gateway: rate limiting, edge tracing, and observability — fixed, pending review

Carried over from ADR-001's "not yet realized" list (`docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md`), closed by [#153](../../pull/153):

- **Rate limiting**: `BackendTrafficPolicy` with Local (token-bucket, no external Redis/Valkey dependency) rate limiting, 300 req/min per distinct source IP, attached to the Gateway.
- **Edge tracing**: `EnvoyProxy.spec.telemetry.tracing` now points at the OTel collector gateway already running in `monitoring`, whose traces pipeline was already wired to VictoriaTraces — the backend existed, Envoy just never sent anything to it.
- **Observability**: `VMServiceScrape`/`VMPodScrape` for both Envoy Gateway workloads (control plane + data plane), plus two official envoy-mixin-project Grafana dashboards.

---

## Stale claims in `README.md`'s architecture summary — fixed, pending review

Found 2026-08-16 while explaining the storage architecture, closed by [#152](../../pull/152).
The Stack table's *"Storage: SeaweedFS (S3-compatible, CSI driver)"* and the Backup Strategy
table's CSI VolumeSnapshot claim were both wrong — no CSI driver or VolumeSnapshotClass exists
on this cluster; all 17 app PVCs use `local-path-provisioner`; Velero backs up PVCs via its
node-agent DaemonSet (Kopia file-system backup), not CSI snapshots. Both tables corrected.

The unused `pvs` bucket (a vestige of the same abandoned CSI-for-PVCs plan) is confirmed empty
and removed from `bucket-init-job.yaml`'s creation loop, so it won't be recreated — but the
already-existing empty bucket itself is still sitting in SeaweedFS. Live deletion was blocked
by this session's auto-mode classifier as a destructive action needing explicit confirmation;
see #152 for the one-line command to remove it by hand if wanted.
