# ADR-003: Offsite Backup Immutability — Versioning Only, No Object Lock

**Date:** 2026-08-19
**Status:** Accepted

## Context

The industry-standard backup framework is **3-2-1-1-0**: three copies, on two media types,
one offsite, **one immutable or air-gapped**, and **zero errors** proven by tested restores.
The two trailing digits are the modern additions, and both exist specifically to survive
ransomware and credential compromise rather than ordinary hardware failure.

This cluster currently satisfies the first three:

| Requirement | Status |
|---|---|
| 3 copies | primary data, SeaweedFS local buckets, AWS offsite |
| 2 media | local NVMe, cloud object storage |
| 1 offsite | `homelab-*-offsite` buckets in `eu-central-1` |
| 1 immutable | **not satisfied** |
| 0 errors | **not satisfied** (no restore has been tested) |

The offsite buckets are Terraform-managed with `aws_s3_bucket_versioning` set to `Enabled`
plus lifecycle rules including `noncurrent_version_expiration`. Versioning protects against
accidental overwrite and deletion: a delete creates a delete marker, and prior versions
remain recoverable until lifecycle ages them out.

Versioning is **not** immutability. Any credential holding `s3:DeleteObjectVersion` can purge
version history permanently. The accepted control for that is **S3 Object Lock** in
governance or compliance mode, which enforces write-once-read-many at the storage layer so
that no principal -- including the account root -- can delete an object inside its retention
window. That is the specific threat the "1" in 3-2-1-1-0 addresses: an attacker who obtains
backup credentials and destroys the backups before or alongside the primary data.

Note the current offsite IAM user (`homelab-velero`) is already scoped tightly enough that it
cannot read bucket configuration at all -- `GetBucketVersioning`, `GetObjectLockConfiguration`
and `GetLifecycleConfiguration` are all denied. It can write objects and it can delete them.

## Decision

**Do not enable S3 Object Lock on the offsite buckets.** Immutability is deliberately left
unimplemented; versioning plus lifecycle retention is accepted as the offsite protection
level.

This is a homelab, not a production system. The data at risk is family photos, scanned
documents, and cluster configuration -- not regulated records, and not something with a
contractual recovery obligation. Object Lock brings real operational weight: retention
periods cannot be shortened once set (compliance mode cannot be disabled at all, even by the
account root), storage costs grow because locked versions cannot be deleted early, and a
mistake in the retention configuration is unfixable rather than merely inconvenient. That
trade is worth making when the alternative is a regulatory finding or a business outage. It
is not obviously worth making here.

The gap is recorded rather than silently carried, so the residual risk is a choice and not an
oversight.

## Consequences

**Accepted risk.** An attacker or a bug with valid offsite credentials can delete the offsite
backups, including all prior versions. In that scenario the surviving copies are the local
SeaweedFS buckets and whatever is reconstructible from Git. There is no ransomware-resistant
copy of this cluster's data, and there will not be one until this ADR is revisited.

**Versioning still earns its place** and stays enabled: it covers the far more likely failures
-- a bad sync run overwriting good objects, a mistaken bulk delete, a backup job writing a
corrupted artifact over a healthy one. Those are recoverable today.

**Retention windows become the recovery contract.** With no immutability layer, the lifecycle
rules in `bootstrap/terraform/lifecycle.tf` are the only thing bounding how far back recovery
is possible. They should be treated as a deliberate RPO statement rather than a cost setting.

**The "0" is not covered by this decision and remains open.** Untested backups are not
backups. Verifying that an artifact decodes correctly -- which the database dump jobs do --
is weaker evidence than performing a restore. A periodic restore drill is tracked separately
in `docs/backlog.md`; it is cheap, it is the more valuable of the two missing digits, and
declining Object Lock is not a reason to decline it as well.

**Revisit if any of the following change:** the cluster starts holding data that is not
reproducible and not also stored elsewhere; it is exposed to untrusted users or workloads;
it takes on an availability or recovery obligation for someone else; or a realistic
ransomware path to the offsite credentials appears. Enabling Object Lock later is possible,
but some modes must be set at bucket creation -- so revisiting may mean creating new buckets
and migrating, not flipping a flag.
