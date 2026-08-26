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
deferred ones (Kubernetes-native RBAC via Talos OIDC, both
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

**Correction (2026-08-25).** The concurrency group fixed the duplicate comments and the
ambiguity over which result the merge honours, but not the cost. Measured across three
Renovate PRs, the pattern was still 5 runs per PR — 4 cancelled, 1 successful — and a
cancelled run has already claimed a runner and started checking out. The remaining half is
fixed by skipping the job outright when a `labeled` event carries a label the gate does not
act on: a job skipped by `if:` allocates no runner at all. This does not reintroduce the
ordering risk, because the guard tests *which* label was just added rather than the order
they arrive in. The concurrency group is split alongside it, so a skipped run lands in a
group of its own and cannot cancel a real evaluation in flight.

---

## Closed: `schenkmatch:latest`, and the CI gate that never caught it

Closed 2026-08-26 by removing the schenkmatch application entirely, which made the
long-blocked half of this finally safe to fix.

Two separate faults sat here. The visible one was an image pinned to `:latest`, bypassing
Renovate tracking and the Trivy CVE gate; it carried 3 critical and 21 high CVEs at removal.
The one that mattered more was that `gitops-lint.yml`'s guard against exactly this had a
regex bug -- `^\s+image:` requires whitespace immediately before `image:`, but container
specs render as list items (`      - image: ...`), so the leading dash broke every match.
The gate had passed on everything for as long as it existed.

The regex could not be fixed on its own: schenkmatch had never published a tagged release,
so a working gate would have failed every PR repo-wide from the moment it merged. That is
why this sat open rather than being a one-line change. With the application gone there are
zero `:latest` images in any overlay under the corrected pattern, so the fix went in
alongside the removal.

Worth keeping in mind: a guard that has never once fired is indistinguishable from a guard
that has nothing to catch. This one was checked by hand only because an audit went looking
for what Renovate did not cover.

---

## Kubernetes-native RBAC (`kubectl`/`kubeoc` CLI access) has no identity layer

Every app behind the Gateway now has real per-user auth (Keycloak OIDC, either native or via Envoy Gateway's `SecurityPolicy.oidc`) or OPA's coarser Rego gate. `kubectl`/`kubeoc` access to the API server itself is still cert-based only — no OIDC trust configured at all. Scoped 2026-08-12 against the CNCF IAM whitepaper's "Administrator" actor: requires `cluster.apiServer.extraArgs` (`oidc-issuer-url`/`oidc-client-id`/`oidc-groups-claim`) in `cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml` (currently absent), applied via `talosctl apply-config` — a brief kube-apiserver restart on this single control-plane node. Cert-based admin access stays untouched either way, so it remains the safety net regardless of when/whether this lands. Not started.

---

## Closed: Envoy Gateway capabilities, and README architecture claims

Both were carried here as "fixed, pending review" and both are merged and live. Kept as a
one-paragraph record rather than deleted, because the ADR they came from now describes
these as what the decision enabled, and someone reading that will want to know when.

**Envoy Gateway rate limiting, edge tracing and fleet observability** (#153). Verified
live 2026-08-25: `BackendTrafficPolicy/homelab-gateway-rate-limit` present,
`EnvoyProxy.telemetry.tracing` configured against the OTel gateway, a VMServiceScrape for
the control plane and two envoy-mixin dashboards loaded. ADR-001's "not yet realized"
section was rewritten to match.

**README architecture claims** (#152). The Stack table's SeaweedFS-CSI and CSI-snapshot
claims were wrong and are corrected. The README was refreshed again on 2026-08-25 for a
larger set of drifts -- component count, missing components, the etcd job that no longer
exists, and SSE-KMS vs SSE-S3.

One loose end survives from #152: the empty `pvs` bucket is out of the bucket-init loop but
still exists in SeaweedFS, because live deletion was blocked as a destructive action. It is
0 objects and costs nothing; remove it by hand if the tidiness is worth a command.

---

## kubeopencode is parked: controller needs cluster-wide secrets read

**Status (2026-08-18): deliberately left non-functional. Decision needed to change that.**

`kubeopencode-controller` is in CrashLoopBackOff and its Agent no longer reconciles
(`observedGeneration` 9 behind `generation` 11). It fails at startup with:

```
failed to list *v1.Secret: secrets is forbidden: User "system:serviceaccount:
kubeopencode-system:kubeopencode-controller" cannot list resource "secrets"
in API group "" at the cluster scope
...
problem running manager: failed to wait for agenttemplate caches to sync
```

**Why it happens.** controller-runtime builds informers that LIST/WATCH at *cluster*
scope. Security review finding M2 (2026-08-14) moved secrets — correctly, on security
grounds — to a namespace-scoped Role, and a namespaced Role can never satisfy a
cluster-scoped informer. This was a **latent** break, not caused by M2 going live: the
running process had already built its cache and would have died on its next restart
whenever that came. The kube-apiserver restart for the OIDC change on 2026-08-18 is what
finally triggered it.

Cluster-scope *read* for `configmaps`/`pods`/`services`/`PVCs`/`deployments` has been
restored (they have the same informer requirement and are far less sensitive); every
**mutating** verb remains namespace-scoped, so M2's actual substance is intact. Only
secrets remain withheld.

**Why it is not simply fixed.** Granting cluster-wide secrets read to a code-task-execution
tool that runs LLM agents — i.e. a live prompt-injection and exfiltration surface — is a
genuine privilege escalation, not a mechanical RBAC gap. Upstream offers no namespace-scoped
cache option; `kubeopencode controller --help` exposes no such flag, so there is no middle
ground available today.

**Consequence, and why the Agent CR was removed.** The `config` Kustomization runs with
`wait: true`, which health-checks every applied object, and Flux has no per-resource opt-out.
A permanently-unreconciled Agent failed the entire Kustomization on every run
(`timeout waiting for: [Agent/kubeopencode-system/default status: 'InProgress']`), blocking
unrelated config-layer changes behind a known-broken component. `agent.yaml` is therefore
commented out of `27-kubeopencode/config/kustomization.yaml` — the file is kept, so
re-enabling is a one-line change.

**Options when this is picked up:**

1. Grant cluster-scope `get/list/watch` on secrets — accepts the escalation; tool works again.
2. Leave parked (current state) — preserves M2; the tool stays unavailable.
3. Remove kubeopencode entirely — also drops the server ClusterRole's `pods/exec` surface.
4. Upstream: request a namespace-scoped cache/`--namespace` option, which would resolve it
   without either tradeoff.

---

## system-upgrade-controller has never created a single Job

Found 2026-08-26 while triggering an on-demand Talos upgrade. The node was on
v1.13.6 against a v1.13.9 pin, a node was labelled for the windowless on-demand Plan, and
nothing happened.

`kubectl get events -n cattle-system` filtered to `involvedObject.kind=Job` returns **zero
events, ever** -- for any of the three Plans, across the cluster's whole life. The automated
node upgrade has never once run.

What makes this hard to see is that every signal says it is fine:

```
Validated=True        PlanIsValid
LatestResolved=True   Version
Complete=True         Complete
```

and SUC logs nothing about any Plan at all -- not a rejection, not a warning. It acquires
its leader lease, starts the Node/Job/Plan controllers, and goes silent. The only visible
symptom is a version pin that never takes effect, which reads as "the window has not come
round yet".

Ruled out so far:

- **Namespace** -- Plans are in `cattle-system` with the controller (fixed in #335)
- **Node selector** -- `kubectl get nodes -l talos.homelab/upgrade-now` returns 1
- **Window** -- the on-demand Plan has none
- **Taints** -- the node has none, and the Plan tolerates everything anyway
- **Node completion label** -- the node carries no `plan.upgrade.cattle.io/*` label
- **serviceAccountName** -- was genuinely missing from all three Plans and is now set;
  adding it did not change the behaviour
- **Stale watches** -- SUC was restarted, re-listed the node, and stayed silent

Still unexplained. Worth checking next: whether SUC v0.20.1 needs `--kubeconfig`/RBAC it does
not have for the Plan CRD specifically, whether its `Complete` condition is sticky and
suppresses re-evaluation, and whether the drain/cordon prerequisites it evaluates silently
exclude a single-node control plane.

Until it is understood, upgrades must be driven by hand:

```
talosctl upgrade --nodes <ip>   --image factory.talos.dev/installer/<schematic>:<version> --preserve=true --wait=true
```

The wider lesson is the one this repo keeps relearning: a component reporting healthy is not
the same as a component doing its job. This one reported `Complete` for 45 days.

---

## Backup restore drill has never been performed

**Status (2026-08-19): open. The "0" of 3-2-1-1-0.**

Backups now exist and their artifacts are checked -- the database dump jobs refuse to upload
an implausibly small file, both PostgreSQL dumps were confirmed to carry valid dump headers,
and the Paperless SQLite snapshot passes `PRAGMA integrity_check`. That is stronger evidence
than most setups have, and it is still not a restore.

Nothing in this cluster has ever been restored from a backup. Until that happens the backups
are believed-good rather than known-good, and the failure modes that only appear at restore
time -- a dump that replays with errors, a missing role or extension, an artifact that
decompresses but is logically incomplete -- remain undetected.

This is deliberately called out alongside [ADR-003](adr/0003-backup-immutability-versioning-only.md),
which declines Object Lock. Both are 3-2-1-1-0 gaps, but they are not equivalent: immutability
was declined as disproportionate for a homelab, whereas a restore drill is cheap and is the
more valuable of the two.

**Suggested drill**, roughly 15 minutes and non-disruptive:

1. Pull the newest Keycloak dump from `s3://db-backups/keycloak/`.
2. Start a throwaway `postgres:16-alpine` pod with an empty database.
3. `gunzip | psql` the dump into it and watch for errors rather than just exit status.
4. Query for a known realm and a known user to prove the data is really there.
5. Delete the pod.

Repeat for Paperless (open the SQLite snapshot, count documents) and Immich (17MB dump,
so allow more time). Worth doing after any PostgreSQL major-version change, since that is
exactly what silently broke the Immich dump once already -- pg_dump 16 against a 17.6 server
produced a 20-byte file that only the size guard caught.

---

## The 3-node overlay would not produce a working cluster

Found 2026-08-25 while reviewing the repository against the live cluster.

`cluster/overlays/3-node/kustomization.yaml` lists **16** infrastructure components.
`1-node` lists **33**. The 18 it is missing are not trimmings:

```
00-gateway-api  00-local-path-provisioner  16-immich  17-paperless-ngx
18-falco  19-kyverno  20-kubescape  22-schenkmatch  24-opa  26-keycloak
27-kubeopencode  28-envoy-gateway  29-metrics-server  30-trivy-renovate-bridge
31-cluster-rbac  32-longhorn  33-cloudnative-pg  34-backup
```

That is no storage, no ingress, no identity, no databases and no backups. It stops at
`21-flux-notifications`, roughly where the repo stood when the overlay was last touched;
everything added since went to `1-node` only.

There is also **no `3-node-config` overlay**. The `config` layer is what applies
CRD-dependent resources — including the Talos upgrade Plans — so on 3-node those would
not deploy at all. And `3-node/talos-machineconfigs/controlplane.yaml` has no installer
pin, so a rebuilt node would not land on the pinned Talos version.

**Why this is worse than an incomplete profile.** It looks deployable. `kustomize build`
succeeds, CI validates it, and nothing signals that the result is a fraction of the
product. The moment it would be reached for is a rebuild after losing the cluster, which
is the worst possible time to discover it.

**This is a decision, not a task.** Either:

1. **Maintain it** — port the 18 components, add `3-node-config`, add the installer pin,
   and add CI that fails when the two overlays diverge. The last part matters most: without
   it, this recurs.
2. **Mark it aspirational** — a header in its `kustomization.yaml` and a line in the README
   saying it is not deployable today, so nobody reaches for it in a recovery.

Option 2 is minutes and removes the trap. Option 1 is the real fix and only worth doing if
a second and third node are actually planned.

---

## Closed: M8, host-privileged collectors split out of `monitoring`

Done 2026-08-26. `monitoring` now enforces PSS `baseline` and audits at `restricted`;
the two workloads that need the host run in `monitoring-agents`, which is privileged and
holds nothing else.

The entry that stood here was wrong on its central claim -- that `monitoring` was
privileged "for one reason: node-exporter needs host access". The OTel agent hostPath-mounts
`/var/log/pods` and `/var/log/kubernetes/audit`, and hostPath alone is a baseline violation.
Moving node-exporter on its own would have left the namespace exactly where it was. Anything
that reads a namespace's PSS level as a proxy for one workload is worth re-deriving from the
pod specs before acting on it.

`restricted` is still out of reach, and that is the honest remainder. The VictoriaMetrics
operator generates pod specs from its own CRs and sets none of `runAsNonRoot`,
`seccompProfile`, `capabilities.drop` or `allowPrivilegeEscalation`, so six of the seven
remaining workloads would be refused admission. Closing that means overriding
`securityContext` per CR -- a separate change, with its own way of failing.

The split also surfaced an unrelated live bug, fixed in the same PR: `restrict-vmsingle-ingress`
had been denying the OTel agent's Hubble remote-write to vmsingle since the hub-and-spoke
migration. No Hubble flow metric had ever reached VictoriaMetrics, and nothing alerted,
because a metric that never arrives has no series for a rule to fire on.

---

## Nothing prompts secret rotation

Found 2026-08-25. `bootstrap/scripts/rotate-secrets.py` exists, is executable, and
supports two-phase SOPS age rotation and per-credential rotation. No workflow, cron or
issue template references it — `grep -rn rotate-secrets .github/` returns nothing.

So rotation happens when somebody remembers, which in practice means never. The
credentials that most warrant it are the ones whose compromise is hardest to notice: the
SeaweedFS S3 admin key, the AWS relay and auditor keys, and the SOPS age key.

**Do not automate the rotation itself.** SOPS age rotation is a two-phase operation —
re-encrypt under both keys, commit, then drop the old key. A half-completed unattended
run leaves every encrypted manifest in the repo unreadable, which is a worse outcome than
an old key. The same argument applies more weakly to the S3 and AWS credentials, where a
rotation that updates the secret but not every consumer breaks the backup chain silently.

**Automate the prompt instead.** A scheduled workflow that:

1. reads a tracked file — say `docs/rotation-log.md` or a small YAML — holding
   `credential: last-rotated-date` and an interval per credential
2. opens a GitHub issue for anything past its interval, titled with the credential and the
   age ("SeaweedFS S3 admin key last rotated 94 days ago")
3. closes or skips when the date moves forward

Rotation stays a deliberate act; forgetting stops being silent. That is the same shape as
the rest of the alerting in this cluster — the failure mode being guarded against is not
"the key is old" but "nobody knows the key is old".

Two scheduled workflows already exist (`cluster-health.yml`, `trivy-auto-patch.yml`), so
the pattern and the permissions are established.

**Worth pairing with**: `rotate-secrets.py` currently has to be told which file and key to
act on. Having it read the same tracked file would let the issue body carry the exact
command to run, which is the difference between a reminder and a runbook.
