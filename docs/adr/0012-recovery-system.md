# ADR-012: The Recovery System

**Date:** 2026-09-12
**Status:** Accepted — built and tested end to end (phases 1–11); cutover follows. The current
pipeline (ADR-005, ADR-010, ADR-011) stays authoritative until cutover, and is removed then.
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
| PostgreSQL | **CNPG barman-cloud plugin**: continuous WAL archiving and base backups to the local object store. Each recovery point stages its base backup, with the WAL that makes it consistent, into restic, so PostgreSQL is validated, promoted and retained like every other dataset. Its restore test recovers a scratch cluster from that restic copy. Immich's database moved onto a CNPG cluster using the vectorchord image. |
| Recovery Point | A JSON record per point, stored in the repository bucket next to the data it describes and promoted with it, so it survives the loss of the cluster. It carries the point's state (VALIDATED, or FAILED if any dataset failed), each dataset's state and restic snapshot, and the remote state: PROMOTION_PENDING, then REMOTE_VERIFIED, or NOT_PROMOTED if local retention removed the data first. Each validated dataset leaves an evidence object beside it. Metrics are derived from the record. |
| Local repository | SeaweedFS bucket `recovery`. |
| AWS repository | A **new** versioned Object-Lock bucket with two identities. The *promoter* may put, get and list, and delete only restic's own lock files. *Retention* may delete, which on a versioned bucket creates delete markers only. Anything else needs the admin, with MFA. |
| Retention | Local 7 daily / 3 weekly / 3 monthly, AWS 1 weekly / 3 monthly, applied per repository by `restic forget` behind a verification gate and a dry-run cap. Each barman archive keeps a 7-day point-in-time window; a database's longer history is its base backups in restic. |
| Credentials | Every AWS credential lives in `backup-system`, and so does the local store's credential for file and SQLite data. **The exception is PostgreSQL:** the barman-cloud plugin reads its object-store credential from the database's own namespace, so each namespace with a CNPG cluster holds the local store's credential. It never holds an AWS one; only promotion, in `backup-system`, reaches AWS. |
| Velero | Removed at cutover. GitOps recreates cluster state. |
| Reconciler | The smallest one, built from Argo itself: an hourly CronWorkflow that submits the recovery point or promotion a missed guarantee needs (phase 11, below). Ordered catch-up after an outage needs nothing more: each run is one DAG. |

## Why this reverses ADR-010

ADR-010 kept each dump in the namespace that owns the data. Its reason was that centralising
would create one namespace with network reach to every database and a copy of every database
credential. This design needs neither:

- Snapshots are taken through Longhorn's API.
- File data is read from clones.
- PostgreSQL is archived by CNPG from inside the database pods.

The central namespace holds the backup-store credentials and nothing that opens an application.
Namespaces with only file data lose the credentials they hold today. A namespace with a CNPG
database keeps one local-store credential, now read by the database's own archiver instead of a
dump job. No application namespace ever holds an AWS credential.

## Consequences

**Positive.**

- The architecture's guarantees are reachable, including a shallower vault and point-in-time
  recovery for PostgreSQL.
- Every dataset follows one validation and promotion path.
- Backup credentials leave application namespaces, except the local-store credential each
  PostgreSQL namespace keeps.

**Negative.**

- **Two new components to operate:** Argo Workflows and the barman-cloud plugin.
- **A second secret that must outlive the cluster.** Without the restic password, the AWS
  repository cannot be read. It belongs with the age key, in the operator's password manager.
- **Clone cost.** A snapshot clone copies the whole volume on every run. That is cheap at today's
  sizes (under 1 GiB per volume); revisit near 50 GiB.
- **Immich's database was migrated,** a dump and restore with a short downtime (138 s on
  2026-09-13).
- **Two pipelines run in parallel until cutover,** which doubles the backup load for that period.
- **PostgreSQL namespaces keep a local-store credential** (see Credentials). That is the plugin's
  design, not a choice made here. The Cilium policy on SeaweedFS remains the boundary for it, as
  for every S3 client in the cluster.

## Phases

| Architecture phase | Here | Status |
| --- | --- | --- |
| Foundation | `backup-system` namespace and network reach, Argo Workflows, `recovery` bucket, local restic repository | Done (#588) |
| 1–2. Policy, datasets | Policy ConfigMap for the new system | Done (#591); cost and growth limits (#595) |
| 3. Recovery point | JSON record per point, state machine, metrics | Done (#591) |
| 4, 6. Restic, SQLite | Snapshot clone → restic; SQLite online backup on the clone | Done: clones and their guardrail (#590), adapters (#591) |
| 5. PostgreSQL | barman-cloud plugin; Immich onto CNPG | Done: archiving for Keycloak and the filer (#600), PostgreSQL datasets in the recovery system (#601), Immich's CNPG cluster (#603) and the switch to it (#604) |
| 7. Restore tests | restic restore with checksums; CNPG recovery clusters with the restore checks | Done: files and SQLite (#591); PostgreSQL through a scratch cluster recovered from the restic copy (#601) |
| 8. AWS | New bucket and identities (Terraform), promotion and remote verification | Done: vault (#592), promotion and remote verification (#596), guarantee alerts (#597) |
| Retention | `restic forget` per repository; AWS only behind a verification gate and a dry-run cap | Done (#599) |
| 9. Argo | Namespaced controller | Done (#588) |
| 10. End-to-end | Including power loss and partial promotion | Done: [recovery-system-tests.md](../recovery-system-tests.md). Five gaps found and fixed: interrupted steps were not retried, a dead restic process's lock blocked the repository, the kubectl steps ran out of memory (#610), steps that create objects could not run twice, and the exit handler left base-backup requests behind. Two proposals are open (below) |
| 11. Reconciler | Decided after phase 10 | Decided: the smallest reconciler, an hourly CronWorkflow (below; #612) |
| Cutover | Old pipeline, relay, reconciler and Velero removed; the old Immich StatefulSet kept as rollback until then | Follows |

## Phase 11: the reconciler decision

Once the workflow works, the architecture asks one question: can Argo Workflows, Kubernetes
resources and GitOps express the desired-state behaviour sufficiently? If yes, build no
reconciler; if no, build the smallest possible one. The end-to-end tests
([recovery-system-tests.md](../recovery-system-tests.md)) answered it behaviour by behaviour:

| Behaviour the architecture requires | Expressed by | Phase 10 |
| --- | --- | --- |
| Order: validate after the backup, promote after the validation | the recovery-point DAG; promotion takes only VALIDATED points | the happy path; FAILED points never promoted |
| Retry an interrupted step | Argo's `retryStrategy`, once it also covers interruptions (F1) | a killed pod retried |
| Resume after a power loss mid-run | the controller resumes a Workflow from its stored state; every step is idempotent | controller killed mid-run |
| Resume an interrupted promotion | `restic copy` skips what the vault holds; records are written AWS-first | partial promotion |
| Offline across the schedule: one late run, no replay | CronWorkflow `startingDeadlineSeconds`, for a CronWorkflow that has run before | one run for the latest missed slot, none replayed |
| One writer at a time | Argo mutexes; restic's locks, which a dead process no longer holds forever (F2) | a backup meeting a dead lock |
| **A failed or missed point retried before the next night** | **nothing** | a FAILED point waited for the next 01:00 (F4) |

Every row but the last is expressed by what already runs. The last is the reconciliation the
architecture itself defines: act on the recovery guarantee, not on the clock -- "the newest
validated point is 27 h old, RPO 24 h: a new point is required", and "a failure is retryable;
the next reconciliation retries it". A schedule cannot say that.

**Decision: the smallest possible reconciler, built from Argo itself** (#612). One CronWorkflow,
hourly, compares the policy's RPO with the newest VALIDATED record of each application, and
the age of a point waiting for AWS with a limit, and submits the one workflow that is missing
-- a recovery point or a promotion -- unless that workflow is running or was started within a
back-off (3 hours). It decides and submits; Argo executes, retries, orders and locks exactly as
for the scheduled runs. It has its own identity, which can list and create workflows and
nothing else. No CRD, no controller, no leader election, no queue, no state of its own: the
records are the observed state and the policy the desired one.

Verified live on 2026-09-13. With its defaults it submitted nothing: every application was
within its RPO. With a point's allowed wait for AWS set to zero, it submitted one promotion per
application, and each ended REMOTE_VERIFIED. With a profile's RPO and the grace set to zero, it
submitted a recovery point for each application of that profile; both VALIDATED, and a second
run at once declined to submit them again ("not now -- running").

The guardrail stands: if it ever needs more than evaluating and submitting, stop and revisit --
"do not accidentally build a backup operator".

## Open

Two findings of phase 10 are proposals, not yet decided. Neither blocks cutover.

**Remote verification reads metadata only (F3).** Promotion ends with `restic check` on the vault.
That loads every index, confirms that each pack the indexes name exists at its recorded size, and
reads every snapshot and tree to confirm that each blob they reference is indexed. It never
downloads a data blob, so it passes a pack whose bytes are corrupt (E6b). What it proves is that
the vault is complete and consistent, not that its data is readable. Locally, every point's
restore test closes that gap. Offsite, nothing does.

Proposal: the weekly AWS retention run gains a last step, `restic check --read-data-subset=5G`,
which runs whether or not anything was thinned. restic picks packs at random up to 5 GiB,
downloads them, and checks each pack's hash and each blob's decryption and hash. Until the vault
holds 5 GiB, that reads all of it every week (89 MiB today). At the 250 GiB cap it samples 2% a
week: enough to catch damage to many packs within a week, and isolated damage eventually. The
vault is S3 Standard, so there is no retrieval fee. About 22 GiB of egress a month fits within
AWS's free 100 GB of monthly transfer out, and costs about $2 a month without it. The
alternative, `--read-data-subset=n/12` rotated by week, reads the whole vault every quarter, but
its cost grows with the vault: close to 100 GB a month at the cap.

It needs one more thing: an alert on `recovery_retention_last_success_timestamp`, which nothing
reads today. A retention run that fails, rather than refusing or holding, alerts nobody; the
repository only grows until it reaches a cap.

**A deleted Helm-rendered object is not healed (F5).** helm-controller acts on a change of chart or
values. Without drift detection, a deleted Deployment of a HelmRelease stays gone until a
reconcile is forced (E10b).

Proposal: `driftDetection: {mode: enabled}` on the four releases the recovery path runs on --
Argo Workflows, the CNPG operator, the barman-cloud plugin and SeaweedFS -- as Longhorn already
has it. No ignore rules are needed:

- The restore playbook suspends a HelmRelease before scaling its workload down (A6), and a
  suspended release is not corrected.
- Fields that controllers fill in and the charts do not render are not compared, such as the
  CNPG webhooks' CA bundles.

A dry run as helm-controller of the four releases' stored manifests (2026-09-13) found no
difference beyond Helm's own metadata, so enabling it would change nothing on the day. Enabling
it for the other releases is a cluster-wide decision. Changes to cilium, longhorn and tailscale
stay reviewed before they merge.
