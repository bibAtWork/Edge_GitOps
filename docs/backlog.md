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

## Open threads as of 2026-08-26

A working note, not a design decision. Delete entries as they close.

**S3 Inventory is fixed but unproven.** `terraform apply` ran successfully on 2026-08-26,
flipping both inventory configurations from Parquet to CSV -- which is the format
`34-backup/reconciler-script.yaml` actually parses. AWS regenerates inventory manifests on
its own schedule, up to 24h, so the reconciler will keep failing to parse until the first
CSV manifest lands. Confirm with:

```
kubectl create job -n longhorn-system inv-check --from=cronjob/backup-reconciler
kubectl logs -n longhorn-system job/inv-check -f
```

If it still reports a parse failure after 24h, the manifest has not rotated yet -- check the
`fileFormat` field in the newest `manifest.json` under the vault bucket before assuming the
apply did not take.

**The node is still on v1.13.6 against a v1.13.9 pin,** and carries a
`talos.homelab/upgrade-now` label that does nothing, because SUC never creates Jobs (see the
entry below). Remove the label when the upgrade is done by hand:

```
kubectl label node <node> talos.homelab/upgrade-now-
```

**Controllers can lose their watches silently when the apiserver restarts.** SUC was found
silent for three hours after the apiserver restarted on 2026-08-26 for the
`--disabled-metrics` change; it recovered only when restarted. Nothing alerted, because a
controller with dead watches looks healthy in every way a probe can see. After any apiserver
restart, `kubectl get events -A --sort-by=.lastTimestamp | tail -40` is a cheap smell test.

**Untested SSO paths.** The move to declarative Keycloak clients was verified for config
drift and for the Hubble, KubeOpenCode and zot logins. Grafana, Paperless, Immich and the
`kubernetes` client (kubectl OIDC) have not been signed into since. Grafana is the one to
check first: `role_attribute_strict: true` with no Viewer fallback means a broken `groups`
claim locks everyone out rather than degrading gracefully.

**apiserver memory needs re-reading.** It was 2983Mi, dropped to 1999Mi immediately after
the restart, and had climbed back to 2731Mi within the hour as caches rebuilt. Do not credit
the metrics change with a specific saving until it has been stable for a day.

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

---

## Deferred: report Trivy CVEs as a delta, not a standing total

#420 cut the CVE alerting from 1,325 firing instances to 70 by counting per image
instead of per CVE. 70 is workable; it is still not a to-do list, because most of
those images carry a steady-state CVE count that will never reach zero. The
actionable signal is **a new Critical appearing**, not the standing total.

The query shape is the same one already used by `SeaweedFSLostObjectsIncreased`
and `SeaweedFSUnreadableBackupObjects` -- compare today against yesterday so a
known baseline cancels out:

```promql
count by (exported_namespace, image_repository) (trivy_vulnerability_id{severity="Critical"})
-
count by (exported_namespace, image_repository) (trivy_vulnerability_id{severity="Critical"} offset 24h)
> 0
```

**Blocked on continuous history, not on effort.** Measured 2026-09-03:
`offset 24h` returned empty, because the cluster was shut down from roughly
13:00 on 09-02 until 07:13 on 09-03 and VictoriaMetrics has an ~18h ingestion
gap. An offset landing inside a gap makes every image look new, which would
reproduce the 1,325-instance storm by a different route.

Two things to settle before implementing:

- **A gap must suppress the rule, not trip it.** Requires the baseline series to
  exist, e.g. `... unless absent(count by (...) (M offset 24h))`, so a missing
  yesterday means "cannot tell" rather than "everything is new". Getting this
  backwards is worse than not having the rule.
- **A new image legitimately has no yesterday.** It should probably alert on its
  absolute count once, then fall into the delta rule -- otherwise a freshly
  deployed vulnerable image is invisible until its second day.

Do this after the store has a clean 48h behind it, and verify the query against
a real gap before trusting it.

---

## Disaster-recovery scenarios: what is actually mechanised

Assessed 2026-09-03 against the live cluster, ahead of the first Sunday on which
the upgrade Plans can fire.

| scenario | mechanised | proven |
|---|---|---|
| 1. local backup + verification | yes | yes |
| 2. recover from local backup | runbook only | yes, by hand |
| 3. recover from remote backup | runbook only | yes, by hand |
| 4. update Talos | yes | **never executed** |
| 5. roll back Talos | **no mechanism** | no |
| 6. update Kubernetes | yes | **never executed** |

**(2) and (3) are deliberately manual and should stay that way.** `backup-repair`
ships suspended for the reason in its own file: restoring on a schedule hides the
rate at which objects go bad. What is missing is not automation but a rehearsal --
both procedures have been executed once, under incident pressure, rather than
practised.

**(5) has nothing at all.** `guard-downgrade` is a CI gate that demands a
`confirmed-downgrade` label on a PR lowering a version; it does not roll anything
back. Talos itself provides `talosctl rollback` -- "rollback a node to the
previous installation", using the other boot partition -- and nothing in this
repo references it, documents it, or tests it. It is also single-shot: it reverts
to the *previous* install, so it works once after an upgrade and not at all after
two. On a single-node cluster with no HA that is the only fast way back from a
bad Talos upgrade, and it has never been tried here.

Lowering the pin and letting SUC run is NOT a rollback path. It would issue
`talosctl upgrade` to an older version, which is a different operation with
different guarantees than reverting a boot entry.

**What Sunday will actually do**, given the pins:

- 03:00 weekly Longhorn backup -- has NEVER succeeded unattended. The 08-30 run
  completed in 71s and produced nothing, because the backupstore was full of
  unreadable objects; the backups that exist came from a manual trigger.
- 11:00 gate -- passes only if that backup ran. The newest backup is presently
  2026-08-31T19:19, well past the 26h threshold, so on today's state the gate
  would refuse and nothing would upgrade.
- 12:00 talos-controlplane -- **no-op**: the node already runs the pinned v1.13.9.
- 14:00 talos-worker -- no worker nodes.
- 16:00 talos-kubernetes -- **the only real change**: v1.36.2 -> v1.36.3.

So the entire Sunday sequence reduces to one Kubernetes patch upgrade, gated on a
backup job that has never worked on its own. That is the thing to rehearse.

---

## Immich's photo library was lost, and had no backup covering it

**Status (2026-09-04): investigated, not recoverable. Found on a manual walkthrough of every
service** -- Immich came up empty; the only way back in was to redo first-run setup, meaning
the database was gone too, not just the library.

Traced through three sources: PVC/object ages, the offsite vault's actual contents (not its
listings -- see "Success is not the same as having done the work" in `Agent.md`), and Longhorn's
`backupvolumes.longhorn.io` for the current PVs.

**The timeline**:

- 2026-08-24: the photo library and cache volumes moved from SeaweedFS to Longhorn
  (#255, #252, #262). `immich-library-lh` (30Gi), `immich-ml-cache-lh`, `immich-valkey-lh` and
  `data-immich-postgresql-0` all date from this migration.
- 2026-08-26: the storage incident (see "Open threads as of 2026-09-01", below) destroyed that
  day's Immich database dump, `immich-20260826T024002Z.sql.gz`, in the same unprotected
  staging-to-relay window that took Keycloak's and Paperless's data. **Confirmed live**: the
  object at that path in `db-backups/immich/` is now an S3 delete marker (0 bytes,
  `Seaweed-X-Amz-Delete-Marker: true`, modified 2026-08-31), matching the cleanup of the 53
  permanently-lost objects from that incident.
- The 2026-08-26 -> 2026-09-01 recovery rebuilt Keycloak from the offsite vault and restored
  Paperless database-and-media together. **Immich is not in that list.** It was missed, not
  deliberately deferred -- nothing in the recovery record explains why.
- Checked whether the *library* itself (the actual photos, never stored in Postgres) survived
  anywhere: SeaweedFS's `pvs` bucket, the old CSI-backed volume location before the Aug 24
  migration, is now empty -- dropped by the migration itself, per its own commit message. No
  other copy exists in the vault. `kubectl get backupvolumes.longhorn.io` shows the FIRST
  Longhorn backup of `immich-library-lh` ever taken was 2026-09-04T05:29:37Z -- **today**,
  and almost certainly a side effect of this session's drift-triggered backup gate, not a
  scheduled run. From creation (2026-08-24) to that moment, the actual photo files had zero
  backup coverage of any kind, through both the incident and the ten days after it.

**What survives**: `immich-20260825T064440Z.sql.gz` (246,716 bytes, real -- verified readable,
not a phantom) in `db-backups/immich/`, about 30 hours before the incident. It carries users,
albums, people/faces and sharing metadata, but Immich's actual photo/video bytes were never in
Postgres, so this dump describes files that no longer exist anywhere. Restoring it live would
also collide with the admin account already recreated. Worth pulling into a throwaway database
to read off album names and dates if that context has any value -- not worth restoring live.

**Conclusion: the photos are gone.** No backup ever existed for the window that mattered.

**Two open questions this leaves**:

- `LonghornVolumeNeverBackedUp` (04-grafana/helmrelease.yaml) exists specifically to catch a
  volume with zero backups and did not fire for ten days on this one. Its query joins
  `longhorn_volume_last_backup_at == 0` against `backup_volume_policy_in_scope` `on(pvc)` --
  worth checking whether a newly-created PVC only enters the policy-scope metric on the
  reconciler's next scheduled pass, leaving a window right after any future migration where a
  brand-new volume is invisible to this alert too.
- `immich-library-lh` carries `recurring-job-group.longhorn.io/default: enabled`, so it should
  now be swept into the regular Sunday `backup-weekly` run going forward -- but that has not
  yet been observed to happen on its own. Worth confirming after this Sunday's run rather than
  assuming today's one-off manual trigger will repeat.

---

## Planned: hub-and-spoke Cilium network policy, replacing `allow-cluster-internal`

**Status (2026-09-04): designed, not started.** Deliberately not implemented yet -- recorded so
the design isn't lost before it is picked up.

`allow-cluster-internal`, a cluster-wide `CiliumClusterwideNetworkPolicy`, grants any pod
ingress from any other pod on any port (`docs/network-architecture.md` section 3, corrected
in PR #176). Every narrower per-app ingress rule elsewhere in the repo is therefore an additive no-op
under it -- verified live via `cilium-dbg` on Keycloak and the SeaweedFS filer. Concretely: a
single compromised pod anywhere in the cluster, including `kubeopencode`/`mcp-server` (LLM
agent tooling `security-review.md` already flags as prompt-injection/exfil surface), has direct
network reach to Keycloak and SeaweedFS S3 on every port, with nothing but each service's own
app-level auth in the way. It also means `CLAUDE.md`'s claim that SeaweedFS's Cilium policy is
"the primary auth boundary" for S3 access is not accurate as deployed.

**Proposed replacement** -- default-deny (already true) plus:

1. A same-namespace-only `CiliumNetworkPolicy` per namespace (`endpointSelector: {}` +
   `ingress: fromEndpoints: [{}]`) -- the pattern `keycloak` and `envoy-gateway-system` already
   use for both directions, and ~20 namespaces already use for egress.
2. Two clusterwide hub policies for the only things that genuinely need every namespace:
   `allow-envoy-gateway-ingress` (replacing dead `fromEntities: [ingress]` rules left over from
   before the Envoy Gateway cutover, ADR-001, and covering `immich`/`paperless`/`schenkmatch`
   and OPA's `ext_authz` path, none of which currently have their own ingress rule at all) and
   `allow-monitoring-scrape-ingress` (mirroring the existing `allow-vmagent-scrape-egress`
   shape for the ingress side).
3. An explicit caller list for SeaweedFS's `allow-seaweedfs-internal.yaml`, replacing its
   current any-pod `fromEndpoints: [{}]` grant on :8333 with `velero`, `talos-backup`, `zot` --
   live-verified that the CSI-driver rule some of this was written for doesn't apply (`kubectl
   get csidrivers` is empty; the only provisioner is `local-path-provisioner`).

**Staged rollout** (each stage additive-then-subtractive and independently verifiable, highest
blast radius last): PR1 adds every new policy while `allow-cluster-internal` stays untouched, so
nothing changes yet -- verify via `cilium-dbg endpoint get` that each new rule is realized. PR2
excludes single-purpose/single-pod namespaces (`schenkmatch`, `falco`, `trivy-system`, `zot`,
`external-dns`, `local-path-storage`, `cilium-secrets`, `kubescape`, `system-upgrade`,
`gateway-system`). PR3 excludes medium-risk namespaces with the new PR1 rules backing them
(`immich`, `paperless`, `monitoring`, `flux-system`, `kube-system`, `velero`, `talos-backup`,
`tailscale`, `cert-manager`, and `kyverno` if a wider Hubble check confirms no same-namespace
traffic). PR4 excludes `seaweedfs`, relying on its new caller-list policy -- verify Velero,
`talos-backup` and Zot's S3 access all still work. PR5, last: `keycloak`, `security` (OPA),
`envoy-gateway-system` -- verify every OIDC login path and every HTTPRoute end-to-end from a
genuinely external client before merging, with a ready revert. PR6 deletes
`allow-cluster-internal.yaml` once every namespace is excluded, and updates
`docs/network-architecture.md` section 3 to describe the new model as current rather than the
"why the broad-allow is mostly not hub-and-spoke" framing it carries today.

Full per-namespace classification (which ones were live-verified to need a same-namespace rule
vs. confirmed not to) was worked out via `cilium-dbg`, `kubectl`, and Hubble flow observation
across all 28 existing network-policy files in the repo, and is not reproduced here -- redo that
audit when this is picked up, since the namespace inventory will have moved on by then.

---

## Cluster review 2026-09-04: what was found and not fixed

A full pass over efficiency, security, maintainability and duplicate/stale mechanisms.
What was fixed went out as PRs #429-#431; what is recorded here either needs its own
investigation, needs a decision, or was deliberately left alone.

### 62 Trivy CRITICAL RBAC findings, never once reported

5 namespaced (`longhorn-system/role-longhorn` 2, `velero/role-velero-server` 2,
`tailscale/role-operator` 1) and 57 on ClusterRoles. Unreported because the rule stacked
three independent faults: a blackholed notifier, a metric name that does not exist
(`trivy_resource_rbacassessments`), and the wrong label case (`CRITICAL` vs `Critical`).
All three are fixed; the findings themselves are untriaged. Expect a large share of the 57
to be built-in ClusterRoles from Kubernetes and from charts, so the work is triage and
probably a narrowed query, not 62 fixes.

### local-path is still the default StorageClass

Deliberate, and worth keeping visible rather than changing right now. A PVC created without
an explicit `storageClassName` lands on unreplicated, node-local storage that no Longhorn
RecurringJob covers. `zot` (8Gi) and the SeaweedFS filer's Postgres both sit there today --
the filer metadata is mitigated by its hourly logical dump, `zot` is a rebuildable registry
cache, so neither is currently a data-loss risk. The risk is the next PVC someone adds
without thinking about the class. Flipping the default was deliberately deferred when
Longhorn was introduced ("keeps local-path as the sole default until workloads are migrated
and verified"); that condition is now largely met, so this is a decision that can be made
rather than a blocker.

### Memory limits are overcommitted 121% on a single node

36.9Gi of limits against 31Gi allocatable, with nowhere to reschedule. Requests are only
35%, so it is latent rather than active, and nothing is currently being OOM-killed. The
four largest are `seaweedfs-filer` (3Gi), `vmsingle`, `immich-server` and
`immich-machine-learning` (2Gi each). Tuning this needs per-workload measurement rather
than a guess -- the LimitRange trap that OOM-killed Immich is the standing reminder that a
number chosen without measuring is worse than no number.

### Corrected: Immich does NOT back its own database up

An earlier draft of this review recorded Immich's built-in database backup as a third
mechanism duplicating the two that already cover that database, and claimed it could only be
turned off through Immich's API because it is application state rather than a Helm value.

Both halves were wrong. It is set declaratively, through `IMMICH_CONFIG_FILE` mounted from
the `immich-oauth-config` secret, and it is already disabled:

    "backup": {"database": {"enabled": false, "cronExpression": "0 02 * * *",
                            "keepLastAmount": 1}}

Deliberately so, since #391 on 2026-08-31, with the reasoning recorded in that commit:
`postgres-backup-cronjob.yaml` owns this database's dump, and having both would write two
copies of the same data to two places. `keepLastAmount: 1` is there only because Immich
validates `isPositive` on that field even when the feature is off -- a 0 crash-loops the
server -- so it is a value the validator accepts, not a retention policy.

The 212MB of dumps that prompted the claim were real, but they were inside the 2026-08-25
Longhorn backup, which predates the fix by six days. The live directory is empty, which is
the state the manifest asks for.

### Left alone on purpose

- **`system-upgrade` namespace holds only the HelmRelease** while every workload it creates
  runs in `cattle-system`. Genuinely confusing, and already a trip hazard. Changing a Helm
  release's namespace means uninstall and reinstall of the very controller that runs the
  first unattended Talos/Kubernetes upgrade this Sunday. Not two days beforehand.
- **`cluster-admin` for `cilium-install` and `longhorn-support-bundle`.** Both look like
  over-grants for one-shot tooling and neither is safely removable: `cilium-install` is
  referenced by the Cilium bootstrap inlineManifest in the Talos machine config, so removing
  it breaks a rebuild, and `longhorn-support-bundle` is generated by the Longhorn chart and
  would simply be recreated.
- **Empty namespaces** (`gateway-system`, `cilium-secrets`, and `kubescape`, which holds only
  a weekly CronJob) are cheap and removing them risks more than it saves.

### Corrections to an earlier draft of this review

Recorded because the mistake is more instructive than the findings: an initial pass reported
`allow-cluster-internal` as still neutralising every narrower ingress policy, and
`allow-csi-seaweedfs-egress` as a stale rule for a CSI driver that does not exist. **Both were
already fixed** -- the hub-and-spoke migration completed on 2026-08-17 and neither object
exists live or in git; they survive only as references inside comments describing their own
removal. The claims came from trusting a planning document still sitting in context instead of
checking the cluster. Same shape as everything else in `Agent.md`'s "success is not the same as
having done the work": a stale artefact read as current state.

---

## Trivy CRITICAL RBAC findings: triaged 2026-09-04, 61 of 62 not actionable

Recorded so this is not re-derived. The findings became visible when the alert was moved
out of vmalert's blackhole and its metric name and label case were corrected (#430); all 62
had existed unreported for as long as the rule had.

| Owner | Roles | Findings | Actionable |
|---|---|---|---|
| Kubernetes built-ins (`system:*`, `cluster-admin`, `admin`, `edit`, `view`) | 13 | | No -- shipped by Kubernetes, reverted if edited |
| Upstream charts/operators | 26 | | No -- reverted on the next chart upgrade |
| Helm-managed namespaced Roles (longhorn, velero-server, tailscale operator) | 3 | 5 | No -- same |
| **`kubescape-scan`** (this repo) | 1 | 1 | **False positive** |

The upstream 26 are kyverno (6), cert-manager (6), victoria-metrics/vmstack (4),
envoy-gateway (2), flux (3), longhorn, cilium-operator, cloudnative-pg, reloader and
trivy-operator itself.

`kubescape-scan` is the only role this repo authors, and the finding is wrong on its own
terms: `AVD-KSV-0046` flags a wildcard on `resources` without looking at the verbs, and that
role holds `get`, `list`, `watch` and nothing else. A cluster posture scanner that cannot
read every resource cannot scan every resource. Left as-is.

The checks involved, for reference: `AVD-KSV-0041` manage secrets (23), `AVD-KSV-0046`
manage all resources (15), `AVD-KSV-0114` manage webhookconfigurations (8), `AVD-KSV-0050`
manage RBAC (7), `AVD-KSV-0045` wildcard verb (3), `AVD-KSV-0044` wildcard verb and resource
(1).

**What changed as a result**: `TrivyRBACCritical` now alerts on a 24h delta rather than an
absolute count, so the immovable baseline cancels and only a *rise* pages. Scoping by name
was considered and does not work -- the only distinguishing label Trivy puts on these series
is `name`, and it carries the hashed report name (`clusterrole-54cdc9b678`), not the
ClusterRole's. The trade is that a new finding stops alerting once 24h of baseline absorbs
it; that is acceptable precisely because the standing set is written down here instead of
depending on the rule to keep repeating it.

### Every trivy-auto-patch PR is created with zero CI

Found 2026-09-04 while asking why #411 -- a Grafana patch for an active Critical CVE, open
since 09-02 -- had never run a single check. `gh pr checks 411` reports "no checks reported
on the branch", and the same is true of #173, the previous Grafana CVE patch, **which was
merged anyway**.

The cause is not a broken workflow. `trivy-auto-patch.yml` opens its PR with `gh pr create`
authenticated by the ambient `GITHUB_TOKEN`, and GitHub deliberately does not raise workflow
events for actions taken with that token -- a documented recursion guard. Every CI workflow
here triggers on `pull_request`, so none of them ever fire for these branches.

The consequence is worth stating plainly: the CVE auto-patch path is the least validated
path in this repository, while being the one that changes image tags on security grounds,
and branch protection requires six checks that can never appear. The two previous patches
therefore reached `ops/talos_linux` without a single overlay build, kubeconform run, or
gitleaks scan.

Two real fixes, neither of which can be applied without a decision:

- Give the workflow a PAT or GitHub App token for `gh pr create`, so events propagate
  normally. Correct, and needs a secret to be created.
- Re-trigger by hand per PR. Closing and reopening from the GitHub UI works because the
  event then carries a human identity; doing the same from inside the workflow does not,
  because it would use the same suppressed token.

Until one is chosen, treat any open `trivy-auto-patch/*` PR as unreviewed by CI regardless
of what the checks column shows.
