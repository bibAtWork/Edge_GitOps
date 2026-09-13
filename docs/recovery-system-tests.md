# Recovery system: end-to-end tests

Phase 10 of [ADR-012](adr/0012-recovery-system.md): the whole recovery system exercised on the
live cluster on **2026-09-13**, first as intended, then under every failure the architecture
names. Faults were injected through the real code paths -- the production templates, the real
repositories, the real vault -- and every restore went into a scratch target. The production
repository was never corrupted: corruption was tested in a scratch repository.

The verdicts are what happened, including the two gaps the tests found. Both are fixed; the
fixes were verified by repeating the tests that found them (V1-V3).

## The happy path

A Paperless recovery point, requested by hand and followed through:

```text
REQUEST -> BACKUP -> VALIDATE -> RESTORE TEST -> VALIDATED -> PROMOTE -> REMOTE VERIFY -> REMOTE_VERIFIED
```

Both datasets backed up from snapshot clones, restore-tested (every file's SHA-256 against the
clone; the SQLite copy opened, `integrity_check`, the policy's restore checks), the point
recorded VALIDATED, then promoted: copied, found in the vault as the `original` of its copy,
`restic check` clean, and REMOTE_VERIFIED in the record in both buckets. Promotion took 42 s.

## The testing matrix

The architecture's matrix (§71), each row with the test that answered it.

| Failure | Expected | How it was provoked | Observed | Verdict |
| --- | --- | --- | --- | --- |
| Backup pod restart | the workflow retries or resumes | a clone pod, and later a scratch-cluster recovery pod, force-deleted mid-run, each together with the Argo controller (E4b) | **not retried**: the killed clone ended `Error: pod deleted` and was not tried again (F1). The recovery pod's `exit 137` turned out to be the kernel's, a second before the kill: kubectl had run out of memory (F6). The point was honestly recorded FAILED and every clone removed. After both fixes: PENDING-V1 | fixed |
| Home server power loss | the next reconciliation restores the RPO | the controller killed during an Immich point (E4); the controller down across two scheduled slots (E5b) | E4: the run resumed and VALIDATED, clones removed, repository check clean. E5b: exactly one late run when the controller returned, for the most recent slot; the earlier slot not replayed. A night that FAILED is only retried at the next schedule -- F4, phase 11 | pass |
| Network interruption | retry | covered by the rows either side: a lost pod (E4b, V1-V3) and an unreachable AWS (E2) | the run fails safe and the next run completes; a step that loses its pod is retried after F1 | pass |
| AWS unavailable | the local point stays VALIDATED | a Cilium egress-deny to the internet on the promotion's pods (E2) | promotion failed at its first AWS call (`i/o timeout`); the record stayed VALIDATED + PROMOTION_PENDING; nothing written to the vault | pass |
| AWS promotion interrupted | promotion resumes | one of an Immich point's two snapshots copied to the vault by hand, the record left pending (E3a); the copy pod killed mid-run (V3) | E3a: only the missing snapshot was copied, both verified, REMOTE_VERIFIED. V3, after the fixes: the copy pod killed as its copy began -- a 100 KB copy had already finished, and the promotion completed with no lock left behind | pass |
| Remote verification fails | the remote copy stays untrusted | promotion's record step given an empty verification result (E8) | "not every snapshot was copied; stays PROMOTION_PENDING", exit 1; record unchanged, nothing in the vault | pass |
| PostgreSQL restore fails | the point is not VALIDATED | a Keycloak restore check no restore can pass, via a test policy (E7) | the scratch cluster recovered, the check failed: dataset and point FAILED, never promoted | pass |
| Object restore fails | the point is not VALIDATED | 64 bytes overwritten inside a data pack of a scratch repository (E6b); a restore step killed (E4b) | `restore --verify` -- what every file restore test runs -- failed with "ciphertext verification failed"; `check --read-data` failed too. A metadata-only `check` passed (F3) | pass |
| Retention job fails | recovery data stays protected | local retention with `forget-max 0` (E9a) | "RETENTION REFUSED ... Nothing was forgotten", exit 64; snapshot counts identical. The verification gate also held `immich-db` live, its newest snapshot belonging to a FAILED point | pass |
| GitOps application deleted | reconstructed from Git | a Flux-applied WorkflowTemplate deleted (E10a); a Helm-rendered Deployment deleted (E10b) | E10a: back within 6 s, identical. E10b: **not** recreated by a plain reconcile; a forced one did (F5) | pass, F5 |
| Local repository lost | recovered from AWS | the restore drills from the vault alone ([runbook](runbooks/backup-recovery.md), A9); a new local repository created with the vault's chunker parameters | every drill passed; chunker polynomial identical | pass |
| Kubernetes cluster lost | rebuilt from Git, data restored | -- | **not run.** It needs a second machine or wiping the only one. The data half is drilled from AWS; the RTO is unmeasured | open |

The phase's own list, where the matrix does not already cover it:

| Failure | How | Observed | Verdict |
| --- | --- | --- | --- |
| Corrupt backup | Paperless' database read from a scratch volume holding a SQLite file with torn pages (E6a) | the consistent copy refused it: `paperless-db` FAILED while `paperless-media` VALIDATED, the point FAILED and never promoted | pass |
| Expired retention | the production local retention, dry run then for real (E9b, E9c) | 21 snapshots forgotten, repository pruned and checked; datasets whose snapshot went are EXPIRED in their record, the rest of the point still restorable; records with nothing left locally removed with their evidence, their AWS copies untouched | pass |
| Guarantee alerting | the FAILED points above | `recovery_point_validated` went to 0 for exactly the failed applications; RecoveryPointFailed fired at its next evaluation | pass |

## Findings

**F1 -- an interruption was not retried.** Argo's `retryStrategy` defaults to
`retryPolicy: OnFailure`, which never retries an Error -- and a pod that is deleted, evicted or
lost with its node ends in Error, or exits 137/143 when its container is killed. So even the
steps that had a retryStrategy gave up on the first interruption, and most steps had none.
Fixed: every leaf step retries interruptions -- the ones that already retried now also on errors,
the rest only on errors and kills, so a real failure still stands.

**F2 -- a dead restic process blocked the repository.** restic never passes a stale lock by
itself: `checkForOtherLocks` does not consult `Stale()`, and only `restic unlock` removes a stale
lock. A backup that met a lock left by a killed prune failed at once, and waiting did not help
until someone ran `unlock`. During the tests a killed promotion left such a lock in the local
repository; that night's retention would have waited two hours on it and failed. Fixed:
backups, promotion and retention remove stale locks first and wait up to 40 minutes for a live
one; `unlock` only removes locks no running process has refreshed for 30 minutes.
Confirmed on a scratch repository: a lock 32 minutes old still failed a backup with
`--retry-lock` ("lock was created ... 32m ago"); `restic unlock` removed it and the next backup
succeeded.

**F3 -- remote verification reads metadata, not bytes.** Promotion's `restic check` confirms the
vault's indexes, snapshots and trees; E6b shows it passes over a corrupted data pack that
`restore --verify` and `check --read-data` catch. That is the architecture's own choice --
"verify without downloading the complete backup" -- and every local point's data is read back by
its restore test. A periodic `check --read-data-subset` against the vault would sample the
offsite bytes at a small egress cost; not added.

**F4 -- a failed or missed night waited a day.** Nothing retried a FAILED point before the next
01:00, so the RPO was missed by up to a day. This is the one behaviour the schedules cannot
express; phase 11 answers it (ADR-012).

**F6 -- the kubectl steps ran out of memory.** kubectl loads the discovery data of every API
group before it applies anything; with the ~200 CRDs this cluster serves, that is about
260 MB (224 MB anonymous + 35 MB file RSS in the kernel's OOM reports), and the five steps that
run kubectl had a 256Mi limit. The kernel OOM-killed them five times on the test day -- the
first such kills since its log began three days earlier -- and a retry of a killed step died
the same way. Found by reading the node's kernel log after retries kept failing: the exit 137
that looked like a test's kill was the OOM killer's. Fixed: 512Mi (#610).

**F7 -- a step that creates objects could not run twice.** After a kill, Argo runs the step
again -- and every step that creates Kubernetes objects failed on the second run: `kubectl
apply` found the object the first run made, admission defaulting turned the re-apply into a
patch, and the workflow identity may create but never patch ("cannot patch resource
clusters"). Found when a killed scratch-cluster recovery was re-run. Fixed without granting
patch: those steps create what is missing and keep what exists, tolerating only
AlreadyExists.

**F5 -- a deleted Helm-rendered object is not healed.** Without drift detection, helm-controller
only acts on a change of chart or values, so a deleted Deployment of the Argo release stayed gone
until a forced reconcile. Objects Flux applies itself are healed within its interval. Two of the
cluster's 36 HelmReleases enable drift detection; whether to enable it more widely is a
cluster-wide decision, not made here.

## Observations

- RecoveryPointFailed is evaluated every 10 minutes: a FAILED point followed by a VALIDATED one
  within that window does not alert. Detection delay is at most 10 minutes.
- A recovery point's Longhorn snapshot lingers, marked removed, until Longhorn's next purge of that
  volume -- the next point's snapshot. On a detached volume, deletion is not even marked until it
  is attached again. Not a leak.
- Never keep a recovery point's pods (`podGC.deleteDelayDuration`): a kept pod that mounted a clone
  pins its claim, and the exit handler's clone removal waits on it.

## Not covered

- A real power cut of the node. The tests simulated it as the workflow sees it: the controller
  and every running pod killed at once, and the controller down across scheduled runs.
- The total-local-loss drill (§72) and its RTO measurement.
- Writes into live volumes and databases, which the runbook marks as not drilled.

## How the faults were injected

Enough to repeat any row:

- **A test policy**: a copy of `recovery-policy` with one line changed, passed as the Workflow's
  `policy` volume (`spec.volumes`); the plan and the PostgreSQL restore checks read the caller's
  policy.
- **AWS down**: a CiliumNetworkPolicy with `egressDeny: toEntities: [world]` for a pod label, set
  on the promotion with `spec.podMetadata`; a `podSpecPatch` shortened rclone's timeouts so the
  outage failed in seconds rather than half an hour.
- **Power loss**: force-deleting the workflow's running pods and the controller's pod together;
  for missed schedules, the controller Deployment scaled to 0 across a slot of a CronWorkflow that
  had already run once (Argo only catches up a CronWorkflow with a `lastScheduledTime`).
- **A dead lock**: `restic prune` killed with `-9` the moment its lock existed, and the backup run
  from another pod -- restic treats a dead process's lock on its own host as stale.
- **Verification that yields nothing**: the promote template with `entrypoint: record` and an
  empty `copied` argument.
