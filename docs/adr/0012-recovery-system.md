# ADR-012: The Recovery System

**Date:** 2026-09-12
**Status:** Accepted — under construction. The current pipeline (ADR-005, ADR-010, ADR-011) stays
authoritative until cutover, and is removed then.
**Supersedes at cutover:** [ADR-005](0005-two-stage-backup-relay.md), [ADR-010](0010-backup-topology.md),
the mechanism half of [ADR-011](0011-recovery-oriented-backup-policy.md)
**Related:** [ADR-003](0003-backup-immutability-versioning-only.md)

## Context

[ADR-011](0011-recovery-oriented-backup-policy.md) adopted the recovery-oriented architecture's
*model* — policy, validated recovery points, promotion, per-guarantee alerts — on the mechanisms
already running. Doing that exposed where those mechanisms stop:

- **The vault cannot keep less than local.** Longhorn backups share blocks, and only Longhorn can
  prune them, against its own target. The architecture's 1 weekly / 3 monthly offsite retention
  is unreachable.
- **Databases have no point-in-time recovery.** A dump every 24 hours is the whole RPO.
- **Application namespaces hold backup-store credentials.** Each dump job carries the S3
  credential into the namespace whose data it dumps, the opposite of the architecture's §54.
- **Catch-up after an outage is unordered** (ADR-011, Open). Independent CronJobs cannot say
  "validate after the backup, promote after the validation".

So the decision was taken to build the architecture as specified, as a separate system, and
replace the current one with it.

## Decision

| Concern | Choice |
| --- | --- |
| Policy | A ConfigMap of plain tables in `backup-system`, as in ADR-011. No CRD. |
| Execution | **Argo Workflows**, with a namespaced controller that manages `backup-system` and nothing else. Each adapter is a WorkflowTemplate. Each recovery point is one DAG: backup, plausibility, integrity, restore test, VALIDATED. Promotion is a separate workflow. |
| Photo library, documents | **restic**, file-level, read from a Longhorn **snapshot clone** mounted in `backup-system`. That gives a crash-consistent point in time without touching the application's namespace. |
| Paperless' SQLite | The same clone. SQLite's online backup API runs on the clone, and restic stores the consistent copy. |
| PostgreSQL | **CNPG barman-cloud plugin**: continuous WAL archiving and base backups to the local object store. Immich's database moves onto a CNPG cluster using the vectorchord image. |
| Recovery Point | A JSON record per point, stored in the repository next to the data it describes and promoted with it, so it survives the loss of the cluster. It carries the state machine (REQUESTED … REMOTE_VERIFIED) and its evidence. Metrics are derived from it. |
| Local repository | SeaweedFS bucket `recovery`. |
| AWS repository | A **new** versioned Object-Lock bucket with two identities. The *promoter* may put, get and list, and delete only restic's own lock files. *Retention* may delete, which on a versioned bucket creates delete markers only. Anything else needs the admin, with MFA. |
| Retention | Local 7 daily / 3 weekly / 3 monthly, AWS 1 weekly / 3 monthly, applied per repository by `restic forget` and per object store by barman. |
| Credentials | Every backup-store credential lives in `backup-system`. Application namespaces hold none. |
| Velero | Removed at cutover. GitOps recreates cluster state. |
| Reconciler | Decided after the end-to-end tests, as the architecture's phase 11 says. Ordered catch-up after an outage is the known requirement. |

## Why this reverses ADR-010

ADR-010 kept each dump in the namespace that owns the data. Its reason was that centralising
would create one namespace with network reach to every database and a copy of every database
credential. This design needs neither:

- Snapshots are taken through Longhorn's API.
- File data is read from clones.
- PostgreSQL is archived by CNPG from inside the database pods.

The central namespace holds backup-store credentials and nothing that opens an application. The
namespaces running application code lose the credentials they hold today.

## Consequences

**Positive.**

- The architecture's guarantees are reachable, including a shallower vault and point-in-time
  recovery for PostgreSQL.
- Every dataset follows one validation and promotion path.
- Backup credentials leave application namespaces.

**Negative.**

- **Two new components to operate:** Argo Workflows and the barman-cloud plugin.
- **A second secret that must outlive the cluster.** Without the restic password, the AWS
  repository cannot be read. It belongs with the age key, in the operator's password manager.
- **Clone cost.** A snapshot clone copies the whole volume on every run. That is cheap at today's
  sizes (under 1 GiB per volume); revisit near 50 GiB.
- **Immich's database is migrated,** a dump and restore with a short downtime.
- **Two pipelines run in parallel until cutover,** which doubles the backup load for that period.

## Phases

| Architecture phase | Here | Status |
| --- | --- | --- |
| Foundation | `backup-system` namespace and network reach, Argo Workflows, `recovery` bucket, local restic repository | This change |
| 1–2. Policy, datasets | Policy ConfigMap for the new system | Follows |
| 3. Recovery point | JSON record per point, state machine, metrics | Follows |
| 4, 6. Restic, SQLite | Snapshot clone → restic; SQLite online backup on the clone | Follows |
| 5. PostgreSQL | barman-cloud plugin; Immich onto CNPG | Follows |
| 7. Restore tests | restic restore with checksums; CNPG recovery clusters with the restore checks | Follows |
| 8. AWS | New bucket and identities (Terraform), promotion and remote verification | Follows |
| 9. Argo | Namespaced controller | This change |
| 10. End-to-end | Including power loss and partial promotion | Follows |
| 11. Reconciler | Decided after phase 10 | Open |
| Cutover | Old pipeline, relay, reconciler and Velero removed | Follows |
