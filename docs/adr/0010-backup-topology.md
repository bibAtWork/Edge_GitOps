# ADR-010: Dump Where the Data Is, Relay and Verify Centrally

**Date:** 2026-09-09
**Status:** Accepted
**Related:** [ADR-003](0003-backup-immutability-versioning-only.md), [ADR-005](0005-two-stage-backup-relay.md), [ADR-008](0008-storage-mechanisms.md)

## Context

Four databases are backed up here, and the work is split across two places without that split
ever having been written down:

| stage | where it runs |
| --- | --- |
| dump | the namespace that owns the data — `keycloak`, `immich`, `paperless`, `seaweedfs` |
| relay, verify, retention, restore-test | centrally, in `longhorn-system` |

[ADR-005](0005-two-stage-backup-relay.md) settled where backups *go* — a local SeaweedFS target
relayed one-way to an immutable vault. It says nothing about where the jobs *run*, and the
question surfaced directly: would one backup namespace with access into the others be better than
four per-namespace dump jobs?

It is a fair question. Four dump jobs is four sets of manifests, and the upload half of each was
duplicated until recently. Consolidating looks like the obvious cleanup.

## Decision

**A dump runs in the namespace that owns the data. Everything after the dump runs centrally.**

The seam is the local object store: a dump job's only job is to produce a verified object in
SeaweedFS. Relay to the vault, read-back verification, retention and restore-testing are then
namespace-agnostic and already centralised.

## Rationale

**1. One of the four cannot be centralised at all.** Paperless is SQLite. Its backup mounts
`paperless-data-lh` — a ReadWriteOnce PVC in the `paperless` namespace — and reads
`/data/db.sqlite3` through SQLite's online backup API, deliberately, because the database runs in
WAL mode and a plain file copy can miss committed transactions. **PVCs are namespace-scoped.** A
pod in a central backup namespace cannot mount it. This is not a preference; it is the object
model.

**2. Centralising the other three concentrates reach and credentials.** Today each dump pod's
egress is one rule — `allow-db-backup-s3-egress`, permitting seaweedfs:8333 and nothing else — and
it reaches its database over intra-namespace traffic using that namespace's own secret.
Compromising the keycloak dump pod yields keycloak's database and an S3 write credential.

A central namespace would need network reach to `keycloak-pg`, `immich-postgresql` and
`filer-meta-pg`, plus all three credentials copied into it: one namespace whose compromise yields
**every database in the cluster**. That points the opposite way from everything else here — the
planned hub-and-spoke migration exists to narrow `allow-cluster-internal`, and `CLAUDE.md` treats
the network policy as the primary auth boundary for object storage.

**3. Failure domains stay separate.** A broken keycloak dump does not touch the other three. One
namespace running all four makes every backup share a blast radius, on the cluster whose database
recovery path is already its thinnest.

**4. The central half already exists, at the right seam.** `longhorn-system` runs `backup-relay`,
`backup-verify`, `backup-reconciler`, `backup-restore-test`, `backup-remote-probe`,
`backup-weekly` and `backup-monthly`. The architecture the centralising proposal is reaching for
is already in place for every stage that does not need to touch the data.

## Consequences

**Adding a database means touching its namespace.** A new database needs a dump job, a network
policy and a credential where it lives. That is the cost, and it is paid once per database.

**The per-namespace half must not be four independent implementations.** That was the real
problem behind the centralising instinct, and it is solved separately: the upload half is one file,
`cluster/base/infrastructure/_shared/backup-upload.sh`, generated into each namespace as a
ConfigMap, with `gitops-lint`'s `backup-upload-contract` job failing the build if a job stops using
it, omits a required parameter, or generates it from anywhere else. Duplication is not what keeps
these jobs correct; validation is.

**The dump is the only stage that knows what it is backing up.** Everything downstream treats the
result as an opaque object, which is why relay and verification generalise and dumping does not.

## Alternatives Considered

**One backup namespace with cross-namespace access.** Rejected for the four reasons above.
Impossible for paperless, and for the rest it trades a narrow blast radius for a wide one to save
manifest boilerplate that has already been addressed another way.

**Continuous WAL archiving instead of periodic logical dumps** — CNPG's barman for the two CNPG
clusters, pgBackRest or WAL-G for immich, Litestream for paperless. This is a change of
*mechanism* rather than of *placement*, and it would not move where the work runs: barman archives
from the database pod, which is in the database's namespace, so it agrees with this decision
rather than replacing it.

It is not adopted here because it is a larger question than backup topology and deserves its own
evaluation: it covers two of the four databases natively, it changes the relay economics from one
object per hour to a continuous stream of WAL segments, and retention by deletion sits awkwardly
with a vault that deliberately grants no delete rights ([ADR-003](0003-backup-immutability-versioning-only.md)).
Recorded in `docs/backlog.md` rather than decided here.

**Keep the four upload implementations separate for isolation.** Rejected, and worth naming
because it is the argument that keeps duplication alive: copies do not buy stability, they buy the
appearance of isolation while removing the one place a guardrail could live. The four copies could
not be linted or tested as one thing, and drifted anyway.
