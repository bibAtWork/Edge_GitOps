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

As of the last review (2026-08-14), one finding has since been fixed in this session (H3 —
kube-proxy/flannel disabled). The rest remain open, roughly in this priority order: a critical
privilege-escalation path in a workload's RBAC, an overly broad read scope on an LLM tool's
service account, and a silently non-functional vulnerability scanner. Ask to see the actual
review locally if you need the specifics — they're intentionally not reproduced here.

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

**Not fixed yet.** Worth doing before the next batch of `kubernetes`-manager-sourced PRs lands (dozens are enumerated in the Dependency Dashboard):
1. Decide the actual policy for "stuck at HIGH/CRITICAL with no clean version available" — auto-merge-with-a-tracking-label, hold for manual review, or a CVSS ceiling regardless of direction — and encode whichever is chosen into rule 3c.
2. Find and dedupe the 5x `trivy-gate` trigger.

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

## Envoy Gateway: rate limiting, edge tracing, and Envoy's own observability are all still missing

Carried over from ADR-001's "not yet realized" list (`docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md`) now that the cutover itself is done and merged (#126):

- **No rate limiting at all.** The old Cilium-era `local_ratelimit` + Valkey rate-limit stack was removed as orphaned/unwired dead code before the cutover (#116) — nothing replaced it. `BackendTrafficPolicy` is the Envoy Gateway equivalent and hasn't been added.
- **No OpenTelemetry trace injection at the edge.** This was the original proposal's headline justification for decoupling L7 off Cilium's Gateway; never actually configured on the live `EnvoyProxy`.
- **No visibility into Envoy Gateway itself.** No `VMServiceScrape` targets `envoy-gateway-system`, no Grafana dashboard imports Envoy's metrics. Hubble covers the L4/Cilium view but not Envoy's L7 one — if Envoy Gateway develops a problem, there's currently no dashboard that would show it.

---

## Stale claims in `README.md`'s architecture summary

Found 2026-08-16 while explaining the storage architecture. The Stack table says *"Storage: SeaweedFS (S3-compatible, CSI driver)"* — but no SeaweedFS `StorageClass`/CSI driver is registered (`kubectl get storageclass` shows only `local-path`), and no application manifest references a SeaweedFS-backed PVC. All 17 app PVCs (Immich, Keycloak, Paperless, Grafana, VictoriaMetrics, Zot, KubeOpenCode) use `local-path-provisioner`. SeaweedFS is real and working, just narrower in scope than the README implies — it's S3-only, consumed by exactly three things: Zot's registry storage, Velero's backup target, and talos-backup's etcd snapshots.

Also found in the same pass: the SeaweedFS bucket-init job creates a `pvs` bucket alongside the three actually-used ones (`etcd-backups`, `velero-backups`, `zot-registry`) — nothing references it. Likely a vestige of an earlier plan to back PVCs with SeaweedFS via CSI that was never carried through. Low-priority cleanup, or evidence the README's CSI claim used to be true and the implementation was later simplified without the docs catching up.

**Fix**: correct the README's Stack table; decide whether to delete the unused `pvs` bucket or leave it as a specifically-reserved future bucket.
