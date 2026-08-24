# Backup recovery runbook

Operational procedures for the two-stage backup architecture in
[ADR-005](../adr/0005-two-stage-backup-relay.md). Everything here is deliberately manual:
these are the steps that need a human, either because they require MFA or because they are
the ones you want to have practised before you need them.

Read the *Implementation status* table in ADR-005 first. Several procedures below describe
components that are written but not yet live.

---

## 1. Restore order after total loss

The order matters. Each step depends on the one before it, and getting it wrong wastes the
scarcest resource in a real recovery, which is your own attention.

```
AWS vault
  -> download the backupstore to local disk
  -> stand up SeaweedFS and point Longhorn at it as a local backup target
  -> restore Longhorn volumes
  -> start applications
  -> restore databases from logical dumps
```

**Why the backupstore must come down first.** Longhorn never talks to AWS in this design. Its
backup target is always a local S3 endpoint, so a remote restore means fetching the
backupstore and serving it locally before Longhorn can read a single block. The on-disk format
is identical either way, so this is mechanical rather than risky — but it is an extra step
that does not exist with a direct remote target, and it is easy to forget under pressure.

**Restore the filer metadata before expecting any object to be readable.** SeaweedFS volume
servers hold anonymous chunks; only the filer's PostgreSQL database maps them back to names.
With that database empty, a fully intact set of volume servers lists nothing at all.

The inverse failure is just as real and less intuitive: metadata restored without its chunks
produces objects that list correctly, report correct sizes, and fail on read. Four dumps were
in exactly that state on 2026-08-24. **Verify a restore by reading bytes back, never by
comparing listings.**

---

## 2. Obtaining admin credentials (MFA)

The `backup-admin` identity is the only one that can destroy a locked object version or
bypass Governance retention. It is interactive-only and lives in the operator's password
manager — never in a Kubernetes Secret, and deliberately not in Terraform state, since state
lives on the machine whose loss this vault exists to survive.

Its policy is conditioned on `aws:MultiFactorAuthPresent`, which **evaluates false for a
long-lived access key used directly**. Every call will be denied until you exchange the key
for session credentials:

```bash
aws sts get-session-token \
  --serial-number arn:aws:iam::<account>:mfa/<device> \
  --token-code <code> \
  --duration-seconds 3600
```

Export the returned `AccessKeyId`, `SecretAccessKey` and `SessionToken`, then work normally.

Without this step the first emergency deletion looks like a broken policy rather than a
working one — which is the worst possible moment to start debugging IAM.

---

## 3. Enabling the AWS vault

The vault is written as Terraform but not applied. To bring it up:

```bash
cd bootstrap/terraform
terraform plan          # with admin credentials
terraform apply
```

Then verify what AWS is actually enforcing, rather than what Terraform believes it applied —
a console edit or a partially-failed apply leaves those disagreeing:

```bash
./scripts/verify-backup-vault.sh homelab-backup-vault
```

Set `RELAY_ACCESS_KEY_ID` / `RELAY_SECRET_ACCESS_KEY` and the `AUDITOR_*` pair before running
it to additionally confirm that `DeleteObject` is denied for both. ADR-005 requires this to be
*attempted*, not inferred — a policy that reads correctly and evaluates differently is the
entire reason the test exists.

Create the relay credential and unsuspend the relay:

```bash
kubectl create secret generic backup-relay-aws-credential -n longhorn-system \
  --from-literal=AWS_ACCESS_KEY_ID="$(terraform output -raw backup_relay_access_key_id)" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$(terraform output -raw backup_relay_secret_access_key)" \
  --from-literal=AWS_BUCKET="$(terraform output -raw backup_vault_bucket)"
```

Re-create that secret as a SOPS-encrypted manifest under `34-backup/` so Flux owns it, then
set `suspend: false` in `backup-relay.yaml`.

---

## 4. Quarterly drill

Not a CronJob, on purpose. This is the only end-to-end validation of the scenario the remote
copy exists for, and it is the one that catches assumptions no automated check encodes.

1. Obtain admin session credentials (section 2).
2. Pull a real backup from the vault — not a listing, the actual objects.
3. Download the backupstore, stand up a local backup target, point Longhorn at it.
4. Restore a volume and **bring the real application up against it**. A volume that mounts is
   not a volume that works.
5. Verify the escrow (section 5) is readable and current.
6. Record the date and outcome below.

| Date | Outcome | Notes |
|---|---|---|
| — | never run | |

An untested restore is not a backup. This table being empty is itself a finding.

---

## 5. Off-site escrow

**Not yet assembled.** Without this, the remote copy is unreadable after total homelab loss
and nothing else in this design addresses that.

Escrow off-site, outside AWS and outside the cluster — paper in a safe, or a hardware token
plus a printed copy at a second location:

- The SOPS/age **private** key. Without it every encrypted manifest in the repo is noise.
- A clone or bundle of the Flux repository, or at minimum its URL plus credentials.
- Terraform state, or enough to reconstruct it.
- The `backup-admin` access key and MFA recovery seed.
- Bucket name, region, prefix layout, and a printed copy of section 1.

**Do not store any of this in the backup bucket.** An escrow that requires the thing it
protects is not an escrow.

Verify readability during each quarterly drill. An escrow nobody has ever read is a guess.

---

## 6. Diagnosing a backup that "succeeded" but did not work

The failure modes in this cluster have consistently been silent. Each of these reported
success while being wrong:

| Symptom | Reality | How it was found |
|---|---|---|
| Backup job green daily | Dumping a superseded database, frozen at migration time | Nothing links an app's DB to its backup job (#263) |
| Objects list with correct sizes | Chunks gone; every `GET` fails | Real read of every object |
| Velero backups completing | No PVC data at all — fs-backup skips hostPath | Restore attempt |
| Alert group configured | Filtered on a label that does not exist; matched nothing for weeks | Reading the raw metric's label set |
| Longhorn healthy | Not scraped at all; zero metrics collected | `up{namespace="longhorn-system"}` empty |

The common thread: **every one of these passed the check that was supposed to catch it.**
When verifying a backup, prefer the test that consumes the artefact — read the bytes, restore
the volume, run the query — over any test that inspects metadata about it.

Quick readability sweep of the backup buckets:

```bash
# Lists, then actually fetches, every object. HEAD is not sufficient:
# it reads filer metadata, which is exactly the half that survives.
for k in $(aws --endpoint-url http://seaweedfs-s3.seaweedfs.svc:8333 \
             s3api list-objects-v2 --bucket db-backups \
             --query 'Contents[].Key' --output text | tr '\t' '\n'); do
  aws --endpoint-url http://seaweedfs-s3.seaweedfs.svc:8333 \
      s3 cp "s3://db-backups/$k" /dev/null --quiet 2>/dev/null \
    && echo "OK   $k" || echo "FAIL $k"
done
```
