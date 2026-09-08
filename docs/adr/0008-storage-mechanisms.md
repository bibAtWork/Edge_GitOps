# ADR-008: Longhorn for Block, SeaweedFS for Object, local-path for One Volume

**Date:** 2026-09-08
**Status:** Accepted
**Related:** [ADR-004](0004-longhorn-v1-storage-engine.md), [ADR-005](0005-two-stage-backup-relay.md)

## Context

Three storage mechanisms were in use, and only two of them were chosen deliberately.

- **Longhorn** — replicated block storage, three StorageClasses: `longhorn` (general),
  `longhorn-db` (`strict-local`, `Retain`, for databases that replicate at the application
  layer), and `longhorn-static` (`Delete`, for the throwaway volumes the nightly restore-test
  creates).
- **SeaweedFS** — S3 object storage, backed by its own Talos user volume on the NVMe.
- **local-path-provisioner** — hostPath, dynamic, and until this decision the **cluster
  default**.

Being the default is what made the third one a problem. A PersistentVolumeClaim that named no
class got node-local storage with no replication, no snapshots, no backups and
`reclaimPolicy: Delete` — and nothing reported it. zot's cache landed there that way: its
`volumeClaimTemplate` sets no `storageClassName`, so it inherited a default nobody had chosen
for it. The mechanism was being selected by omission rather than by intent.

The goal here is the smallest set of mechanisms that still provides block and object storage,
without pretending a constraint away.

## Decision

**Longhorn is the block layer and the default StorageClass. SeaweedFS is the object layer.
local-path is retained for exactly one volume, which must name it explicitly.**

That volume is the SeaweedFS filer metadata database (`filer-meta-pg`) and its WAL.

Everything else that needs a volume gets Longhorn, and gets it without asking.

## Why that one volume can use neither of the other two

This is the part that looks like an oversight and is not.

**Not SeaweedFS.** The filer metadata database is the index of every object SeaweedFS holds.
Recovering SeaweedFS from a copy stored inside SeaweedFS is not a recovery path — an empty
filer cannot list the bucket holding its own backup.

**Not Longhorn.** [ADR-005](0005-two-stage-backup-relay.md) makes Longhorn's backup target the
local SeaweedFS S3 endpoint, so putting the filer's database on Longhorn closes a loop:

```
Longhorn -> SeaweedFS S3 -> filer -> Postgres -> Longhorn
```

A Longhorn failure would then make the very backupstore meant to repair it unreadable. Note the
dependency is on Longhorn *being up*, not on Longhorn backups — so excluding the volume from
backup does not break the cycle, and neither does a single-replica or `strict-local` class.

Longhorn was the original answer here and was correct for the architecture that existed at the
time, because Longhorn then came up without the filer. ADR-005 closed the loop from the other
direction.

**So local-path.** `/var/lib/local-path-provisioner` is on the system disk (`sda`), while
SeaweedFS data and Longhorn replicas are on the NVMe — a third failure domain, which is the
actual requirement. It gives up expansion and snapshots for nothing measurable: durability for
this database has never come from the storage layer, but from hourly logical dumps to the
`db-backups` bucket, relayed offsite. Those dumps are what the nightly restore-test replays.

## Consequences

**Three storage systems run, but only two can be reached by choice.** local-path is now reachable
only by naming it, and exactly one manifest does. It is an exception with an address, not a
default anyone can fall into.

**A PVC that names no class is protected.** Longhorn stamps
`recurring-job-group.longhorn.io/default` on every new volume and all three RecurringJobs are
bound to that group, so forgetting to classify a volume now means it is snapshotted and backed
up rather than silently exposed. The cost is that a reconstructible volume is backed up until
someone adds it to `volume-backup-policy`'s `ephemeral-pvcs` list. Against a ~1.2 GiB backed-up
baseline that is cheap; the reverse mistake is silent data loss.

**local-path-provisioner must keep running,** including the busybox helper pod it schedules to
create and remove directories. The provisioner image is distroless and cannot do this itself.
That helper image is pinned; an unpinned one is a live dependency in the rebuild path.

**The filer database's protection is the dump, not the volume.** If the hourly dump or the
restore-test that verifies it stops, this database has nothing, and the storage layer will not
say so.

**This is reversible in one direction.** If the filer's metadata store ever moves off Postgres,
local-path loses its last consumer and can be removed entirely.

## Alternatives Considered

**Static PV instead of the provisioner, keeping the volume.** Removes the helper pod and its
image pull. Rejected: CNPG names PVCs per instance and generates a new instance number when it
replaces one, so a static PV — which exists for exactly one name — would leave a replacement
instance with nothing to bind, precisely when recovery is needed. It also hardcodes node
affinity, which does not survive a rebuild under a different hostname or the 3-node overlay.
Trading a bootstrap-time dependency for a failure-time one is the wrong direction on the
database whose recoverability is the entire reason for this ADR.

**Move the filer metadata into SeaweedFS's embedded store (leveldb/rocksdb).** This is the only
change that removes the *reason* for a third mechanism, and it is genuinely less to maintain: it
deletes a CNPG cluster, two PVCs and a backup CronJob. Rejected because it collapses metadata
and data onto the same disk — today metadata is on `sda` and data on `nvme0n1` — and swaps a
verified `pg_dump` path for a different backup story, on the component whose failure is hardest
to recover from. Fewer mechanisms bought with less separation is not a saving.

**Point Longhorn's backup target at AWS directly, breaking the cycle.** Rejected: it undoes
ADR-005. Longhorn needs delete rights on its backupstore to run retention, and the offsite vault
deliberately grants none.

**Leave local-path as the default and pin the exceptions instead.** Rejected as backwards. It
keeps the failure mode where an unclassified volume is unprotected and nothing reports it, in
exchange for not having to name one volume.
