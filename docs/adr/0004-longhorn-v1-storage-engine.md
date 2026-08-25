# ADR-004: Longhorn V1 as the Block Storage Engine

**Date:** 2026-08-23
**Status:** Accepted — implementation in progress
**Supersedes in part:** the storage placement decisions in
[docs/backup-architecture.md](../backup-architecture.md), which recorded databases
staying on `local-path` because no safe block storage existed.

## Context

Databases in this cluster run on `local-path` — hostPath volumes on the system
disk. That was never a choice so much as the absence of one. When everything else
moved to SeaweedFS, the databases stayed behind for a specific reason: PostgreSQL
depends on POSIX `fsync` durability, and a network FUSE filesystem is not a safe
place to assume it. If `fsync` returns before data is durable, a crash loses
committed transactions or corrupts the database — silently, and only discoverable
at restore time.

That left three problems unresolved:

- `local-path` pins a pod to the node holding its data, so nothing can reschedule.
- Volume backup never worked for these volumes: Velero's file-system backup skips
  hostPath by design, producing no `PodVolumeBackup` at all.
- The databases have no point-in-time recovery, only daily logical dumps.

Talos ships minimal by design and has no shell. Storage backends needing iSCSI,
DRBD or SPDK require **system extensions**, which are baked into the OS image at
build time and cannot be installed at runtime.

## Decision

Use **Longhorn V1** with the `siderolabs/iscsi-tools` and
`siderolabs/util-linux-tools` Talos system extensions.

## Rationale

**Longhorn V2 (SPDK)** reached GA recently and has an open bug causing SPDK
initialization failures on Talos specifically, plus a separate open bug causing
I/O errors on PostgreSQL pods during V2 volume backups — precisely the workload
this decision exists to serve. It also needs hugepages, a dedicated core, and
PCI/IOMMU access, and still requires the *same* extensions as V1. It is a strict
superset of V1's requirements, not a lighter alternative.

**Mayastor** does not support arm64, and requires a disk used 100% by a DiskPool
— a hard blocker here, where the NVMe must be shared with SeaweedFS.

**LINSTOR/DRBD** has the best resource efficiency of everything evaluated and no
known PostgreSQL-specific bugs, but needs a 3-node minimum for real quorum, cannot
do single-node at all, and requires both a custom Talos image *and* a separate
operator with its own upgrade loop. Worth revisiting if this cluster ever
stabilizes at 3+ nodes; this ADR does not rule it out permanently.

**Decisive for this cluster:** Longhorn V1 imposes no minimum node count,
allocates from a directory rather than claiming a whole disk, and works unchanged
whether the node has one disk or several. Nothing else evaluated is true on a
single node with a shared disk.

## Consequences

**A custom Talos installer image is now required, and must be regenerated on every
Talos version bump.** This is unavoidable rather than a cost of Longhorn
specifically — every evaluated backend except local-path/NFS needs at least one
extension. It is a property of Talos's immutable design.

**Upgrading a single-node Talos cluster requires `--stage`.** Learned the hard
way: `talosctl upgrade` cordons and drains by default, and on a single node there
is nowhere to drain to. Every eviction times out against the client rate limiter,
the drain error aborts the upgrade before the new image is written — and the
command still **exits 0** with the node returning `Ready`. `--force` does not skip
the drain either; it was tried and produced the identical failure.

`--stage` is the mechanism that works: it writes the upgrade to META and applies
it at boot, before Kubernetes starts, so no drain occurs. Staging alone applies
nothing — the node must then be rebooted.

**Verify with `talosctl services`, looking for `ext-` entries.** Do not trust the
exit code, and do not verify by reading `/proc/modules` from Git Bash on Windows:
MSYS rewrites POSIX-looking arguments into Windows paths, so `talosctl read
/proc/modules` silently reads the wrong path and returns nothing — indistinguishable
from a genuine absence of modules. Set `MSYS_NO_PATHCONV=1` and pass `TALOSCONFIG`
as a Windows path, or the tooling misleads in both directions. iSCSI modules also
load on demand, so their absence is weak evidence even when read correctly; an
`ext-iscsid` service is not.

**`longhorn-system` runs with `enforce: privileged`.** Inherent to any CSI driver
managing host block devices. Mitigated, not eliminated, by a network policy and by
not running the UI — which ships with **no authentication at all**, so anyone
reaching its Service could detach volumes or repoint the backup target.

**Longhorn must not go on the system disk.** `/var/lib/longhorn` would sit on
EPHEMERAL beside `/var/lib/etcd`, and sustained replica writes push etcd's fsync
p99 up — presenting as leader elections and slow API responses, i.e. looking like
a control-plane fault rather than a storage one. Longhorn goes on the NVMe and
etcd stays on the SATA disk, which separates them at the device level.

**One replica, not two.** This node has a single Longhorn disk, so a second
replica has nowhere to go; `replicaSoftAntiAffinity` would only protect against a
disk failure that cannot be protected against here anyway. Durability comes from
backups, and that makes the backup path load-bearing rather than optional.

**Block replication must not stack on application replication.** Any workload that
replicates at the application layer — CNPG streaming replication — gets the
`longhorn-db` class with one replica and `strict-local` locality. Stacking them
multiplies physical copies of every write for no added durability and adds a
network round trip on commit.

**Revisit V2** once the Talos SPDK/IOMMU bug and the PostgreSQL backup I/O bug are
resolved upstream and live-upgrade support lands for V2 volumes.

## Rejected outright

**SeaweedFS FUSE for PGDATA.** Object storage with a filesystem view, not block
storage. This is the specific thing the ADR exists to fix. SeaweedFS remains the
right home for S3-compatible backup targets and application blobs — just not a
database's data directory.
