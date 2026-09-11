# ADR-011: Recovery-Oriented Backup Policy

**Date:** 2026-09-12
**Status:** Accepted
**Related:** [ADR-003](0003-backup-immutability-versioning-only.md), [ADR-005](0005-two-stage-backup-relay.md), [ADR-008](0008-storage-mechanisms.md), [ADR-010](0010-backup-topology.md)

## Context

A recovery-oriented backup architecture was proposed on 2026-09-11. Its central claim is that
a backup system should not be optimised for successful jobs but for *continuously holding a
validated recovery point that meets an explicit guarantee*. It separates policy (what must be
protected, and how well), execution, the backup engines, and the recovery point, and it sets
hard invariants: RPO is measured against the newest **validated** point, only validated points
are promoted offsite, the offsite copy is verified without downloading it, and a server that
was switched off has not failed.

Measured against this cluster on 2026-09-12, several of its premises do not hold:

| The proposal assumes | This cluster |
| --- | --- |
| SeaweedFS holds primary image and object data, protected with Restic | SeaweedFS holds no primary data. Every bucket is a backup target, a cache, or empty. Photos and documents are Longhorn volumes. |
| Paperless runs on PostgreSQL | Paperless runs on SQLite |
| Nextcloud and custom stock-data applications | Not deployed |
| PostgreSQL protected by pgBackRest | Two CNPG clusters and one plain StatefulSet (Immich, with vector extensions), each dumped by `pg_dump` |
| No Velero | Velero exists, holds Kubernetes objects only, and writes locally since #574 |
| Argo Workflows as the execution engine | Not installed. CronJobs and Longhorn RecurringJobs. |

It also exposed gaps that were real:

- **No guarantee was written down.** RPO, RTO and retention existed only as the side effect of
  whichever schedules happened to be configured.
- **The only off-node copy of photos and documents was weekly.** The newest recovery point for
  the two datasets that matter most could be seven days old.
- **The weekly tier held two days of history.** On-demand backups were created from
  `backup-weekly`, so they consumed its retain slots; on 2026-09-12 the five "weekly" backups on
  every volume spanned 2026-09-04 to 2026-09-06.
- **No database dump had ever been restore-tested automatically.** The nightly restore-test
  covers Longhorn volumes only.
- **Unvalidated points went offsite.** The relay ran at 05:00, two hours before the restore
  test.
- **Monitoring asked whether jobs ran,** not whether a guarantee held.

## Decision

**Adopt the proposal's model on the mechanisms already here, and build nothing the cluster
already has.**

1. **The policy is one ConfigMap,** [`34-backup/backup-policy.yaml`](../../cluster/base/infrastructure/34-backup/backup-policy.yaml):
   protection profiles, applications with a criticality, the datasets that hold each
   application's state, the queries that prove a restored dump, and what is deliberately not
   backed up. The profiles are the proposal's, unchanged: RPO 24h; RTO 4h (critical) or 8h
   (important); a restore test at least weekly or monthly; offsite required; local retention
   7 daily / 3 weekly / 3 monthly; vault 1 weekly / 3 monthly.

2. **A build check binds the policy to the mechanisms.** [`scripts/check-backup-policy.py`](../../scripts/check-backup-policy.py),
   run by `gitops-lint`, fails when a producer runs less often than its dataset's RPO, writes
   somewhere other than where the dataset says, a retain count falls below a profile's
   minimum, or a database dataset has no restore check. The proposal's first invariant — every
   protected dataset has explicit requirements — is enforced at review time rather than stated.

3. **A recovery point is derived, never stored.** Each stage publishes evidence about a specific
   point: which backup or dump a restore test restored and whether it passed, and whether that
   same point is present in the vault with a matching hash. State — BACKED, VALIDATED,
   REMOTE_VERIFIED, PROMOTION_PENDING, OVERDUE — is computed from that evidence where it is
   read. There is no CRD, no controller, and no stored state machine to drift away from the
   evidence it summarises.

4. **VALIDATED means restored.** Every Longhorn dataset's newest backup is already restored and
   checked nightly. Database dumps get the same: each is replayed into a throwaway server
   inside the test's own pod, with no network listener and no credential for any production
   database, and the policy's restore checks are run against it. That is stronger than the
   proposal's weekly or monthly cadence and affordable because the data is small; the profile's
   restore-test age is the floor that is alerted on, not the schedule.

5. **Only validated points are promoted.** The relay runs after the restore tests and copies
   only what existed when the last passing test started. A failed or missing test holds
   promotion: the local point stays valid, the vault falls behind, and that reads as
   PROMOTION_PENDING rather than as a failure. The unit is a restore run, not a single
   backup: older backups of a volume go offsite with the newest one, which was restored and
   whose blocks they share.

6. **The vault is verified without downloading it.** Each validated point is confirmed in the
   vault by comparing listing metadata — the vault's ETag against the MD5 of the bytes the
   restore test actually restored. Objects here are single-part (Longhorn blocks are 2 MiB,
   dumps a few MB), so the ETag is the MD5. A multipart ETag is reported as unverifiable, never
   assumed, which is the proposal's own caution.

7. **Offline is not failure — mostly handled by the CronJob controller.** No backup CronJob
   sets `startingDeadlineSeconds`, so after the node has been off Kubernetes runs each job's
   most recent missed schedule once and does not replay the rest — the proposal's preferred
   model. RPO is measured from when a point was taken, so what alerts is catch-up not
   happening, not the outage. What catch-up does not do is run the stages *in order*; see
   Open.

8. **Monitoring is per guarantee.** The alert that matters is "this dataset has no validated
   point younger than its RPO", raised per dataset. Job-level alerts stay, as diagnostics.
   Thresholds come from the policy, published as metrics, so no rule restates an RPO. A point
   is validated hours after it is taken, so it ages to the RPO plus that lag before the next
   one replaces it; the rule allows 4 hours for that.

## What is not adopted, and why

**Argo Workflows.** The dependencies here are linear and daily — produce, validate, promote,
verify — and schedule order plus result gates express them. The CronJob controller already
provides retries, catch-up and history. The proposal's own last phase asks whether Kubernetes
primitives can express the behaviour before anything is built on top; they can. Argo would add a
controller, CRDs, a UI and an upgrade path to a single node for no capability this uses. Revisit
if a workflow needs fan-out and fan-in, or per-dataset retries that a CronJob cannot give.

**A reconciler — not yet.** The one reconciliation the proposal needs — "the RPO was missed,
take a point now" — is what catch-up does, as long as the stages run in order. After an outage
they do not (see Open), and that is exactly the case the proposal's last phase exists to
decide. Its warning, *do not accidentally build a backup operator*, is why recovery-point state
is derived rather than stored, and why that decision is left open here rather than made.

**Restic.** Its role in the proposal is object data held in SeaweedFS, and there is none.
Longhorn's backupstore already deduplicates at block level, and the dumps are self-contained
objects. Restic would add a repository format and a second encryption key to protect, for no
dataset.

**pgBackRest.** Two of the three PostgreSQL databases are CNPG clusters, whose native equivalent
is barman-cloud; pgBackRest would sit beside the operator rather than inside it. Continuous
archiving and point-in-time recovery remain an open evaluation in `docs/backlog.md`. A 24h RPO is
met by the dumps, and because the policy names the engine per dataset, changing it later changes
one row.

**Removing Velero.** Kept, and classified RECONSTRUCTABLE: it is not a dataset, no guarantee
depends on it, and its objects are relayed as they are. The proposal's reasoning — GitOps
recreates cluster state — is accepted. Removing Velero is a separate decision.

**A vault that keeps less than local.** The proposal keeps 1 weekly and 3 monthly offsite
against 3 and 3 locally. The vault here is a copy of local state. For Longhorn, one backup's
blocks are shared with every other backup of the same volume, so removing "one weekly" from the
vault means knowing which blocks nothing else references. That is Longhorn's own garbage
collection, which only runs against its own target. Pruning the vault independently would need
a delete-capable identity next to the relay, which ADR-003 and ADR-005 exclude on purpose. So the
vault keeps what local keeps, plus the reconciler's 14-day grace, under Object Lock. That
exceeds the vault minimum, and the build check refuses a local retain count below it.

## Retention and immutability

Longhorn datasets keep 7 daily, 3 weekly and 3 monthly backups locally, enforced by RecurringJob
retain counts. Database dumps are never pruned locally; at their size that exceeds the policy
at no meaningful cost. The vault mirrors local state. The proposal's immutable-retention
semantics already hold: Object Lock in Governance mode, and deletion before expiry only by an
administrator with MFA (ADR-003, ADR-005).

## Consequences

**Positive.** Guarantees are written down and checked at build time. The RPO for photos and
documents drops from up to seven days to 24 hours. Database dumps are restore-tested for the
first time. Unvalidated points stop leaving the node. Alerts name the dataset whose guarantee is
broken. No new controller runs on the node.

**Negative.** Promotion now waits on validation, so a flaky restore test delays the offsite copy
by a day. That is deliberate, and it is the trade the proposal asks for. The restore tests cost
a few minutes of compute a night, and the photo library's restore grows with the library; the
Longhorn test runs within a time budget and reports volumes it could not reach instead of
dropping them. The daily tier adds each day's changed blocks to the relay. The policy is a
plain-text table parsed by shell, which is less expressive than a CRD, on purpose.

## Open

- **The monthly tier held no backup on 2026-09-12,** although its 2026-09-01 run completed. Its
  logs have aged out and no backup older than 2026-09-04 survives on any volume. The next data
  point is 2026-10-01. The RPO alerts do not measure tier depth.
- **RTO has no evidence yet.** It stays an acceptance criterion until a timed restore drill
  exists.
- **Stale etcd snapshots** sit in a bucket nothing writes to or relays. Under this policy
  etcd is reconstructable; they should be classified and removed.
- **Catch-up after an outage is unordered.** When the node comes back, the CronJob controller
  starts every missed job at once. A restore test can run before that day's dumps exist and
  validate the previous day's points, and a Longhorn backup that fires before workloads have
  attached their volumes skips them, because Longhorn does not back up a detached volume. The
  next scheduled night repairs both, so the cost is up to a day of BackupRecoveryPointOverdue
  after an outage. Closing it needs ordering the CronJob controller cannot express: the
  proposal's own trigger for building the smallest possible reconciler, or a workflow engine.
  A wait on host uptime in each stage would fix the first half and not the second.

## Implementation

| Proposal phase | Here | Status |
| --- | --- | --- |
| 1. Policy | `profiles`, `applications` | In place |
| 2. Datasets | `datasets`, `not-backed-up`; daily Longhorn tier to meet the RPO | In place |
| 3. Recovery point | Derived from per-point evidence: each restore test records the point it validated, `backup-verify` confirms that point in the vault, alerts compare both against the policy | In place |
| 4. Restic | — | Not adopted |
| 5. PostgreSQL | `pg_dump` kept; `backup-db-restore-test` replays every dump into a server of its own major | In place |
| 6. SQLite | Online-backup API (existing); `integrity_check` and restore checks on the copy | In place |
| 7. Restore tests | Longhorn volumes (existing); databases (`backup-db-restore-test`) | In place |
| 8. Promotion and remote verification | Relay gated on both restore tests, promoting only what they validated; each validated point confirmed in the vault from listing hashes | In place |
| 9. Argo Workflows | — | Not adopted |
| 10. End-to-end test | Restore-test, promotion and vault-verification failure cases exercised live; outage catch-up not | Partly |
| 11. Reconciler | Needed only for ordered catch-up after an outage | Open |
