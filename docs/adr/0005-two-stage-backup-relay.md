# ADR-005: Two-Stage Backup — Local Longhorn Target with One-Way Relay to an Immutable Vault

**Date:** 2026-08-24
**Status:** Accepted (partially implemented — see *Implementation status*)
**Supersedes:** [ADR-003](0003-backup-immutability-versioning-only.md) on the Object Lock question
**Related:** [ADR-001](0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md), [ADR-004](0004-longhorn-v1-storage-engine.md)

## Context

The cluster uses Longhorn v1 for block storage and SeaweedFS for object storage. A remote
copy is needed for disaster recovery in addition to local rollback. The requirements:

- Local: multiple retained versions for rollback, disk-efficient — a home server, not a datacentre.
- Remote (AWS S3): versioned, immutable, recovery-oriented rather than rollback-oriented.
- **No in-cluster process may hold any S3 delete permission.** Deletion is performed by AWS
  itself (Lifecycle) or by an interactive admin. This is absolute.
- Retention schedules configurable without editing manifests.
- etcd is out of scope.
- A single part-time operator. Toil and false-positive alerts are themselves failure modes.

### Why the obvious design does not work

Pointing Longhorn's backup target directly at AWS fails on three counts, each discovered
rather than predicted:

- Longhorn's backupstore acquires and releases a `.lck` object around **every** operation, so
  it requires delete permission to function at all.
- Its `.blk` objects are deduplicated, shared between backups, and always current versions.
  Lifecycle can neither reach them safely nor age them meaningfully.
- Longhorn backups exist only in the backup target, so restore-testing one always reads
  remotely — egress on every verification.

A narrow lock-prefix delete grant resolves the first and leaves the rest. It also puts an
exception into a rule whose entire value is having none.

## Decision

**Longhorn's backup target is a local SeaweedFS S3 endpoint. A separate relay job mirrors
that backupstore one-way to AWS, using a credential with no delete permission of any kind.**

This removes the problem rather than working around it:

- The AWS credential reduces to `PutObject`/`GetObject`/`ListBucket`. No exceptions, no
  policy subtleties to get wrong.
- Restore-testing becomes genuinely egress-free, because a real local backup exists to
  restore from.
- Remote pruning becomes a mechanical set difference against the local backupstore, rather
  than something requiring knowledge of Longhorn's block reference graph.

### Remote pruning: the reconciler tags, and AWS deletes

Objects deleted at the source are never overwritten in S3, so they remain current versions
forever. Versioning and noncurrent expiration cannot reach them, and age-based expiration of
current versions would destroy live deduplicated blocks.

So the reconciler diffs Inventory against local contents, applies `lifecycle=prunable` to
objects absent locally past a grace period, and a tag-filtered Lifecycle rule expires them.
**AWS performs the deletion internally; no client credential is involved.** Because the
bucket is versioned, expiry writes a delete marker rather than destroying the locked version,
so a mistaken or malicious tagging run is reversible within the Object Lock window.

The restore-test result *gates* the pruning run; it does not mark objects to be pruned.
Tagging backups as "verified" and deleting verified objects would delete precisely the copies
worth keeping.

### Placement constraint, and why it is stricter than it first appears

The SeaweedFS volume servers behind the backup target must not sit on Longhorn PVCs —
otherwise the backup target rests on the storage it protects. They are on a dedicated
hostPath disk (`/var/mnt/seaweedfs`), verified 2026-08-24.

**The same rule extends to the filer's metadata database.** SeaweedFS serves S3 from the
filer, and the filer keeps its namespace in PostgreSQL. That database was on Longhorn, which
closed the loop from the other direction:

```
Longhorn -> SeaweedFS S3 -> filer -> Postgres -> Longhorn
```

A Longhorn failure would have made the backupstore meant to repair it unreadable. Moved to
`local-path`, a third failure domain (#265). Volume servers were compliant; the metadata
store on the same critical path was not.

Staging SeaweedFS onto Longhorn is the mirror image and is **not** circular: each system
holds the other's copy, so either can be rebuilt from the survivor. Only a system holding its
own copy is circular.

## Reversal of ADR-003

ADR-003 recorded a decision to rely on **versioning only, without Object Lock**, on the
grounds that this is a homelab rather than a production system. This ADR reverses that: the
vault uses Object Lock in **Governance** mode with 21-day default retention.

Governance rather than Compliance: no override exists for Compliance, including for account
root, so a mistaken retention value would be unfixable for its full duration. Governance can
be bypassed by an MFA admin, which is the intended escape hatch.

The reversal matters because the relay design changes the threat model that ADR-003 was
reasoning about. Under ADR-003 the offsite copy was written by credentials that also held
delete rights, so Object Lock was one control among several against the same credential.
Here the relay credential cannot delete at all, and Object Lock's job is narrower and
different: it bounds the damage of a compromised **auditor** credential tagging objects
prunable. That is a real, specific risk the tag-gated pruning mechanism introduces, and it is
what the lock window exists to make recoverable.

## Retention

| Tier | Default | Purpose |
|---|---|---|
| Longhorn snapshot | daily, retain 7 | Fine-grained local rollback; same-disk, cheap |
| Longhorn backup (weekly) | Sunday, retain 5 | ~5 weeks of recoverable points |
| Longhorn backup (monthly) | 1st of month, retain 6 | ~6 months of coverage |
| SeaweedFS local staging | daily, retain 14 | Object-store rollback |
| Relay to AWS | daily | Mirrors whatever the local backupstore holds |

Snapshot chains are kept short deliberately: Longhorn snapshots live on the replica disk, and
a long chain costs both space and read performance.

Retain counts here are **real and enforced**, unlike a design where Longhorn writes to AWS —
Longhorn holds full delete rights against its local target and runs retention as designed.

**Remote retention equals local retention**, a direct consequence of reconciliation-based
pruning. Extending the remote horizon would need independent remote retention logic and a
second pruning criterion. Not implemented; a known limit.

### Retention is configuration

Values live in one ConfigMap and reach manifests through Kustomize `replacements`.

Flux `postBuild.substituteFrom` was the obvious mechanism and is deliberately **not** used.
This repo has a single cluster-wide Flux Kustomization, so substitution reaches every manifest
it renders — including eight whose shell scripts contain `${VAR}`. It would blank `${SIZE}` in
the filer dump guard, the check that stops a truncated dump being uploaded as good. A
retention knob is not worth breaking integrity guards to obtain.

## Verification

- **Continuous, free:** daily S3 Inventory reconciliation — counts, sizes, freshness, Object
  Lock coverage, unexplained delete markers.
- **Per-run, local, no egress:** restore the latest local backup, `fsck`, mount, application
  liveness check.
- **Monthly, bounded:** restore the smallest volume from AWS, to catch remote-path breakage
  within a month.
- **Quarterly:** full drill — pull from AWS, restore, bring the real application up, verify
  escrow readability.

Entropy and malware scanning of backup contents are out of scope: raw block data and
SOPS-encrypted files are indistinguishable from ransomware output by entropy, and scanning
restored artefacts detects an infection strictly later than scanning the live source.

### What listing-based verification misses

Implementing this surfaced four objects in `db-backups` — all three Immich database dumps and
one Keycloak dump — that list with correct names, correct sizes and correct timestamps, and
**fail on read**. Their filer metadata survived an earlier restore while their chunks did not.
Immich therefore had no usable database backup at all, while its backup job reported success
daily.

No size guard, listing comparison, or job exit status can detect this. Only a real `GET` can.
Backup jobs now verify the gzip stream before upload and **read the object back after upload**
(#271), because a successful PUT is not a readable object.

## Consequences

**Positive**

- The no-in-cluster-delete rule holds with no exceptions, verifiable by reading four IAM policies.
- Pruning is automatic, with no manual step and no credential swapping.
- Longhorn runs in its supported configuration, with working retention and no lock friction.
- Local restore-testing tests the actual backup artefact rather than a snapshot.
- Retention is tunable without touching manifests.

**Negative / accepted**

- Local disk must hold the entire backupstore in addition to live data and staging.
- A remote restore requires downloading the backupstore before Longhorn can read it — a
  slower RTO than a direct remote target.
- SeaweedFS is dual-purpose, with the backup role on dedicated disks. More placement
  discipline required, and the filer metadata constraint above shows how easy that is to miss.
- A compromised `backup-auditor` can tag objects prunable and trigger delete markers.
  Recoverable within the Object Lock window, but disruptive. Mitigated by a tagging-rate cap
  and alert, not eliminated.
- S3 Inventory does not report object tags, so tagging activity is observable only via the
  job's own metrics and CloudTrail — weaker than the other reconciliation signals.
- Remote retention cannot exceed local retention.
- Tag-to-deletion latency is up to ~48h, since Lifecycle evaluates daily.

## Implementation status

| Task | Status |
|---|---|
| 1 — Local SeaweedFS backup target | Done. Verified with two overlapping backups; zero `.lck` accumulation |
| 2 — Retention ConfigMap | Done, via Kustomize replacements |
| 3 — Longhorn RecurringJobs | Done (snapshot / weekly / monthly, `default` group) |
| 4 — SeaweedFS staging | Done, hardlinked daily snapshots |
| 5–8 — AWS vault, Lifecycle, IAM, bucket policy | Written as Terraform, validated, **not applied** |
| 9 — Relay | Done, ships **suspended** until the vault exists |
| 10 — Local restore-test | Not implemented |
| 11 — Reconciler and prune tagging | Not implemented |
| 12 — Monthly remote probe | Not implemented |
| 13 — Quarterly drill | Runbook written; never executed |
| 14 — Off-site escrow | Runbook written; **not assembled** |
| 15 — Monitoring | Partial — chain alerts done; Longhorn RecurringJob alerts pending real series |

Tasks 5–8 are unapplied because Terraform needs AWS admin credentials that the automation
environment does not hold, and creating an Object Lock bucket is a commitment worth making
deliberately. Tasks 11 and 12 depend on the vault and Inventory existing.

**Until Tasks 5–9 are live, there is no off-site copy of anything.** Everything implemented so
far is local, and a total loss of this node loses all of it.

## References

- Longhorn `backupstore`: `lock.go`, S3 driver, deduplicated `.blk` layout
- AWS S3: Object Lock on existing versioned buckets (since Nov 2023); Lifecycle tag filters;
  Inventory optional fields (tags not included)
- NIST SP 800-53 Rev. 5, CP-4, CP-9, CP-9(1), CP-9(2), CP-9(7)
- NIST SP 1800-11, *Data Integrity: Recovering from Ransomware and Other Destructive Events*
