# Backup recovery runbook

Two backup systems run side by side until cutover, and this runbook covers both:

- **Part A — the recovery system** ([ADR-012](../adr/0012-recovery-system.md)): validated
  recovery points in restic, locally and in the AWS vault `homelab-recovery-vault`, plus barman
  point-in-time recovery for PostgreSQL. **Restore from this system first.** Its procedures were
  drilled against the real repositories (A9).
- **Part B — the current pipeline** ([ADR-005](../adr/0005-two-stage-backup-relay.md)): Longhorn
  backups and logical dumps, relayed to the vault `homelab-backup-vault`. It stays until cutover,
  and Part B is deleted with it.

Everything here is deliberately manual: these are the steps that need a human, either because they
require MFA or because they are the ones you want to have practised before you need them.

---

# Part A — The recovery system (ADR-012)

Every procedure below was run against the real repositories on **2026-09-13**, restoring into
scratch targets: a scratch cluster, a scratch pod, a scratch prefix (A9). Steps that write into a
live volume or database were not, because that means overwriting production data; they are marked
**not drilled**, and are built only from commands that were.

Everything is `kubectl` from a workstation, plus two manifests in [`recovery/`](recovery/), which
Flux never applies:

- [`recovery-shell.yaml`](recovery/recovery-shell.yaml): a pod in `backup-system` with restic for
  both repositories (containers `restic` and `restic-aws`) and rclone for both buckets (`rclone`),
  the credentials already in place. No key is typed, and none leaves the cluster.
- [`objectstores.yaml`](recovery/objectstores.yaml): the barman ObjectStores a PostgreSQL restore
  reads from (A5).

```bash
kubectl apply -f docs/runbooks/recovery/recovery-shell.yaml
kubectl wait -n backup-system pod/recovery-shell --for=condition=Ready
# ... A2 to A5 ...
kubectl delete -f docs/runbooks/recovery/recovery-shell.yaml
```

On Git Bash for Windows, `export MSYS_NO_PATHCONV=1` first. Otherwise every argument that starts
with `/` is rewritten into a Windows path, and restic answers `path ... not found`.

## A1. What exists, and where

| Data | Dataset | A snapshot holds | Restore |
|---|---|---|---|
| Immich photos | `immich-media` | `/upload`, `/library`, `/profile` of `immich-library-lh` | A3 |
| Paperless documents | `paperless-media` | `/documents/originals`, `/documents/archive` of `paperless-media-lh` | A3 |
| Paperless database | `paperless-db` | `/.recovery/paperless-db.sqlite3`, SQLite's online backup of `db.sqlite3` | A4 |
| Keycloak, Immich, filer databases | `keycloak-db`, `immich-db`, `filer-db` | `/<cluster>/`: one barman base backup and the WAL that makes it consistent | A5 |

**A snapshot's root is the volume's root.** `restic ls` shows `/documents/originals/...`, while
`restic snapshots` shows `/data/documents/originals`, because it prints the directory the backup
ran from. Commands take the first form; the second is `path not found`.

Not in any snapshot, on purpose: Immich's thumbnails and encoded video, Paperless' thumbnails and
search index. The applications rebuild them (A3). Everything else in the cluster comes back from
Git; `recovery-policy`'s `derived` table says what is left out and why.

| What | Where | Holds | Read with |
|---|---|---|---|
| Local restic repository | `s3:http://seaweedfs-s3.seaweedfs.svc:8333/recovery/restic` | 7 daily, 3 weekly, 3 monthly points | `restic` (Secret `restic-local`) |
| AWS restic repository | `s3:s3.eu-central-1.amazonaws.com/homelab-recovery-vault/restic` | 1 weekly, 3 monthly; versioned, Object Lock 21 days | `restic-aws` (Secret `recovery-aws-promoter`: read and write, no delete) |
| Recovery-point records | `recovery-points/<app>/<point>.json`, in **both** buckets | each point's state and snapshots | `rclone`: `local:recovery/...`, `aws:$AWS_BUCKET/...` |
| barman archives | `s3://recovery/postgres/<cluster>/`, local only | every WAL segment and base backup of the last **7 days** | ObjectStore `restore-archive` |

One restic password opens both repositories. **Without it, neither can be read** (A8).

**The records are the index**, one per point:

```json
{"id":"recovery-point-paperless-manual-sl4wn","application":"paperless",
 "taken":1789285381,"recorded":1789285513,"state":"VALIDATED",
 "remote":{"state":"REMOTE_VERIFIED","verified":1789285989},
 "datasets":{"paperless-db":{"state":"VALIDATED","restic_snapshot":"32c0d386..."},
             "paperless-media":{"state":"VALIDATED","restic_snapshot":"c041c9a6..."}}}
```

- `state` is `VALIDATED` only if every dataset's restore test passed, `FAILED` otherwise.
- `remote.state` stays `PROMOTION_PENDING` until promotion has verified the copy in AWS, then
  becomes `REMOTE_VERIFIED`.
- A dataset whose snapshot retention removed becomes `EXPIRED`; the rest of the point stays
  restorable.
- `restic_snapshot` is the **local** repository's ID. The copy in AWS has an ID of its own and
  carries the tag `rp=<point>`.
- Times are Unix seconds: `date -u -d @1789285381`.

**Schedules (UTC):** recovery points 01:00, promotion 03:00, local retention 04:00, AWS retention
Sundays 04:30. Point-in-time recovery reaches back 7 days, locally only. Beyond that, and after
total loss, the unit of recovery is a daily point.

## A2. Choosing a point

Restore all of an application's datasets from **one** point: `immich-db` with `immich-media`,
`paperless-db` with `paperless-media`. They were taken together; a database from one day with
files from another points at files that are not there.

List the records, locally, or in AWS, which after total loss is the only copy:

```bash
kubectl exec -n backup-system recovery-shell -c rclone -- sh -c \
  'P=local:recovery/recovery-points/immich; for f in $(rclone lsf $P --files-only); do rclone cat $P/$f; echo; done'
kubectl exec -n backup-system recovery-shell -c rclone -- sh -c \
  'P=aws:$AWS_BUCKET/recovery-points/immich; for f in $(rclone lsf $P --files-only); do rclone cat $P/$f; echo; done'
```

rclone's notice about a missing config file is expected: its remotes come from the environment.
Take the newest `VALIDATED` point from before the damage; after total loss, one that is also
`REMOTE_VERIFIED`. A dataset's snapshot is its `restic_snapshot` locally; in AWS:

```bash
kubectl exec -n backup-system recovery-shell -c restic-aws -- \
  restic snapshots --no-lock --tag rp=<point> --host <dataset>
```

PostgreSQL within the last 7 days needs no point: route A in A5 reaches any moment.

## A3. Files: Immich photos, Paperless documents

Stream the snapshot as a tar into the application's own container. It already mounts the volume
and runs as the application's user, so the files land with the right owner; the stream goes
through the API server, so no network policy is crossed and no volume changes hands.

| Dataset | Container | Volume at |
|---|---|---|
| `immich-media` | `-n immich deploy/immich-server` | `/data` |
| `paperless-media` | `-n paperless deploy/paperless-ngx` | `/usr/src/paperless/media` |

```bash
ID=<snapshot>   # A2
kubectl exec -n backup-system recovery-shell -c restic -- \
  restic dump --no-lock --archive tar "$ID" / \
  | kubectl exec -i -n paperless deploy/paperless-ngx -- tar -x -C /usr/src/paperless/media
```

- From AWS: `-c restic-aws`, with the AWS snapshot's ID.
- Part of it: replace `/` with a directory, such as `/documents/originals`. Entries keep their
  path from the volume root, so `-C` stays the same.
- One file, to your machine: `restic dump --no-lock "$ID" /documents/originals/0000001.pdf > 0000001.pdf`.
- A restore adds and overwrites; it never deletes. Files created after the point stay.
- Afterwards let the applications rebuild what is not backed up. Immich: the thumbnail and video
  jobs for missing assets, on its admin Jobs page. Paperless:
  `kubectl exec -n paperless deploy/paperless-ngx -- document_thumbnails`, then
  `document_index reindex`; `document_sanity_checker` confirms every document resolves to a file.

Drilled: from AWS (`drill-files-aws`), and from the local repository streamed into a pod in
another namespace, byte-identical to restic's own verified restore. **Not drilled:** into the live
containers.

## A4. The Paperless database

The snapshot holds `/.recovery/paperless-db.sqlite3`: SQLite's online backup of the live file,
taken on a snapshot clone, so it is consistent on its own. Check it before it goes anywhere
(`drill-sqlite-aws` does this, plus the policy's restore checks):

```bash
kubectl exec -n backup-system recovery-shell -c restic -- \
  restic dump --no-lock "$ID" /.recovery/paperless-db.sqlite3 > paperless-db.sqlite3
sqlite3 paperless-db.sqlite3 'pragma integrity_check'      # ok
```

Paperless runs SQLite in WAL mode. The live `db.sqlite3-wal` and `db.sqlite3-shm` belong to the
live file and must move with it: left beside the restored file, SQLite would apply the old WAL to
it. **Not drilled:** stop Paperless (A6), then put the file in place from a helper pod on the data
volume.

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: restore-helper
  namespace: paperless
spec:
  restartPolicy: Never
  securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000}
  containers:
    - name: helper
      image: docker.io/library/alpine:3.22
      command: ["sleep", "3600"]
      resources: {limits: {memory: 128Mi}}
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
      volumeMounts: [{name: data, mountPath: /data}]
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: paperless-data-lh}
EOF
kubectl wait -n paperless pod/restore-helper --for=condition=Ready
kubectl exec -n backup-system recovery-shell -c restic -- \
  restic dump --no-lock "$ID" /.recovery/paperless-db.sqlite3 \
  | kubectl exec -i -n paperless restore-helper -- sh -c 'cat > /data/db.sqlite3.restored'
kubectl exec -n paperless restore-helper -- sh -c 'cd /data &&
  for f in db.sqlite3 db.sqlite3-wal db.sqlite3-shm; do [ -e $f ] && mv $f before-restore.$f; done;
  mv db.sqlite3.restored db.sqlite3'
kubectl delete pod -n paperless restore-helper
```

The old database stays as `before-restore.db.sqlite3`, its WAL still paired with it, until you are
satisfied. Start Paperless (A6), restore `paperless-media` from the same point (A3), and run
`document_sanity_checker`.

## A5. PostgreSQL

Every route restores into a **new** cluster in `backup-system`, never over the live one, and then
copies the database across. The live cluster keeps its identity and its WAL archive, its manifest
in Git is untouched, and a failed attempt costs nothing.

| Route | Reads | Reaches | When |
|---|---|---|---|
| A | the live barman archive, locally | any moment in the last 7 days | the cluster and SeaweedFS work, but the data is wrong: a bad migration, a deletion |
| B | a point's restic copy, locally | that point | the moment is older than 7 days, or the archive is damaged |
| C | a point's restic copy, in AWS | that point | SeaweedFS cannot serve: after total loss, or when the filer database itself is lost |

When `filer-db` is lost, C is its only route: its archive and its local copy both live in the store
it indexes.

A restore cluster runs its source's image, which means the same major version and the same
extensions:

| Dataset | Source | Database, owner | Image |
|---|---|---|---|
| `keycloak-db` | `keycloak/keycloak-pg` | `keycloak`, `keycloak` | `ghcr.io/cloudnative-pg/postgresql:16.15` |
| `immich-db` | `immich/immich-pg` | `immich`, `immich` | `ghcr.io/tensorchord/cloudnative-vectorchord:17-0.3.0`, plus `postgresql: {shared_preload_libraries: [vchord.so]}` |
| `filer-db` | `seaweedfs/filer-meta-pg` | `seaweedfs_filer`, `seaweedfs` | `ghcr.io/cloudnative-pg/postgresql:16.15` |

Renovate moves the images; check the source's `spec.imageName` rather than trusting this table.

```bash
kubectl apply -f docs/runbooks/recovery/objectstores.yaml   # restore-archive (A), restore-aws (C)
```

### Route A: a moment in time

```bash
kubectl apply -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: restore-keycloak
  namespace: backup-system
  labels:
    recovery-scratch: "true"
spec:
  imageName: ghcr.io/cloudnative-pg/postgresql:16.15
  instances: 1
  enablePDB: false
  storage:
    size: 5Gi
    storageClass: local-path    # the Longhorn classes Retain; local-path cleans up after itself
  resources:
    requests: {cpu: 50m, memory: 128Mi}
    limits: {memory: 1Gi}
  bootstrap:
    recovery:
      source: origin
      recoveryTarget:
        targetTime: "2026-09-13 09:22:20+00"
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: restore-archive
          serverName: keycloak-pg    # the source cluster
EOF
kubectl wait clusters.postgresql.cnpg.io -n backup-system restore-keycloak --for=condition=Ready --timeout=30m
kubectl exec -n backup-system restore-keycloak-1 -c postgres -- psql -U postgres -d keycloak -c 'select count(*) from realm'
```

- The restore cluster names no WAL archiver (no `plugins:`), so it never writes into the archive
  it reads.
- PostgreSQL stops before the first commit after `targetTime`, and needs one to stop at. A target
  past the last archived commit fails with `recovery ended before configured recovery target was
  reached`, and CNPG retries the recovery job indefinitely. Pick an earlier time, or drop
  `recoveryTarget` to replay everything archived. WAL reaches the archive at the latest every five
  minutes, so the last minutes before a failure may not be there.
- Delete a failed restore cluster before trying again:
  `kubectl delete clusters.postgresql.cnpg.io -n backup-system restore-keycloak`.

### Routes B and C: a recovery point

Stage the point's archive where an ObjectStore can read it, then recover its one backup:

```bash
ID=<snapshot>; N=restore-filer     # N names both the staged prefix and the cluster
kubectl exec -n backup-system recovery-shell -c restic-aws -- restic restore --no-lock "$ID" --target /restore
kubectl exec -n backup-system recovery-shell -c rclone -- ls /restore/filer-meta-pg/base   # the backup ID
kubectl exec -n backup-system recovery-shell -c rclone -- \
  sh -c "rclone copy /restore/filer-meta-pg aws:\$AWS_BUCKET/restore-drill/$N"
```

Route B is the same from the local repository: `-c restic`, and
`rclone copy /restore/<cluster> local:recovery/restore-test/$N`.

The cluster is route A's, with `name: restore-filer` and this in place of its `bootstrap` and
`externalClusters`:

```yaml
  bootstrap:
    recovery:
      source: origin
      recoveryTarget:
        backupID: "20260913T074324"    # from base/
        targetImmediate: true
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: restore-aws    # route B: recovery-restore
          serverName: restore-filer        # $N
```

Remove the staged copy once the database is across. A 403 about `GetBucketVersioning` is expected;
rclone carries on without it.

```bash
kubectl exec -n backup-system recovery-shell -c rclone -- \
  sh -c "rclone purge retention:\$AWS_BUCKET/restore-drill/$N"    # route B: local:recovery/restore-test/$N
```

Nothing else would ever remove it: the promoter key cannot delete, and Lifecycle never expires a
current version. The `retention:` remote writes delete markers; the versions under them expire on
their own.

### Copying the database into the live cluster

**Not drilled** against a live cluster. The same commands were drilled between two databases of a
restore cluster, into an empty database and into a dropped and recreated one.

1. Stop the application (A6).
2. Recreate the database, then restore into it:

   ```bash
   SRC=restore-keycloak-1
   DST=$(kubectl get clusters.postgresql.cnpg.io -n keycloak keycloak-pg -o jsonpath='{.status.currentPrimary}')
   kubectl exec -n keycloak "$DST" -c postgres -- psql -U postgres \
     -c 'drop database keycloak with (force)' -c 'create database keycloak owner keycloak'
   kubectl exec -n backup-system "$SRC" -c postgres -- pg_dump -U postgres -Fc -d keycloak \
     | kubectl exec -i -n keycloak "$DST" -c postgres -- \
         pg_restore -U postgres -d keycloak --exit-on-error --single-transaction
   ```

   Drop and recreate rather than `pg_restore --clean`: `--clean` drops only what the dump contains,
   so a table a later migration created would survive beside the restored schema. The dump carries
   its own ownership; restoring as `postgres` over the local socket hands every object back to the
   application's role.
3. Start the application (A6) and check it by using it.
4. Clean up: `kubectl delete clusters.postgresql.cnpg.io -n backup-system -l recovery-scratch=true`,
   then `kubectl delete -f` both manifests.

## A6. Stopping an application for a restore

Keycloak and Paperless pin `replicas: 1` in Git, and Flux puts it back within ten minutes; the
Immich and SeaweedFS HelmReleases put theirs back at their next upgrade. Suspend the owner first.

| Application | Stop | Start |
|---|---|---|
| Keycloak | `flux suspend kustomization flux-system`, then `kubectl scale -n keycloak deploy/keycloak --replicas=0` | `flux resume kustomization flux-system` |
| Paperless | the same, with `-n paperless deploy/paperless-ngx` | the same |
| Immich | `flux suspend helmrelease -n immich immich`, then `kubectl scale -n immich deploy/immich-server --replicas=0` | scale back to 1, then `flux resume helmrelease -n immich immich` |
| SeaweedFS filer | `flux suspend helmrelease -n seaweedfs seaweedfs`, then `kubectl scale -n seaweedfs statefulset/seaweedfs-filer --replicas=0` | scale back to 1, then `flux resume helmrelease -n seaweedfs seaweedfs` |

While `flux-system` is suspended nothing in the cluster reconciles, so resume it as soon as the
application is back; resuming also restores its replicas. Stopping the filer stops all S3 in the
cluster, the local repository included.

## A7. Order after total loss

```text
escrow (A8): age key, restic password, AWS vault keys
  -> the cluster from Git (disaster-recovery.md, Scenario B); SeaweedFS comes up empty
  -> pause the recovery schedules
  -> recovery shell: every source is now AWS (restic-aws, route C)
  -> keycloak-db, before anything that logs in through it
  -> immich-db, then Immich's photos (A3)
  -> Paperless' database (A4), then its documents (A3)
  -> start the applications; check each by using it
  -> a new local repository, then resume the schedules
```

**Do not restore `filer-db` after total loss.** The volume servers' chunks went with everything
else, and the filer's database alone lists objects that fail on read (Part B, section 1).
SeaweedFS starts empty from Git. `filer-db` is for losing the filer's database while its volumes
survive.

**Pause the schedules before the first 01:00.** A rebuilt Keycloak passes its own restore checks,
because the realm import creates realms, users and clients. Its first nightly point would be
validated and promoted, and AWS retention could then thin the last good point out of its week or
month. Git does not set `suspend`, so Flux leaves the patch alone, and resuming is up to you:

```bash
for C in $(kubectl get cronworkflows -n backup-system -o name); do
  kubectl patch -n backup-system "$C" --type merge -p '{"spec":{"suspend":true}}'
done
```

**Create the local repository before resuming.** Nothing creates it by itself. Give it the AWS
repository's chunker parameters, so data splits into the same blobs and promotion deduplicates
against what AWS already holds instead of uploading everything again. restic reaches the local
store through rclone, inside the shell:

```bash
kubectl exec -n backup-system recovery-shell -c rclone -- \
  rclone serve restic local:recovery/restic --addr 127.0.0.1:8000 &
kubectl exec -n backup-system recovery-shell -c restic-aws -- sh -c \
  'RESTIC_FROM_PASSWORD="$RESTIC_PASSWORD" restic -r rest:http://127.0.0.1:8000/ init --from-repo "$RESTIC_REPOSITORY" --copy-chunker-params'
kill %1
```

Then resume the schedules: the same loop, with `"suspend":false`.

## A8. Escrow

The recovery system adds one item that is **unrecoverable if lost**, to be kept with the age key
(Part B, section 5; that section moves here at cutover):

- **The restic password**, `RESTIC_PASSWORD` in
  `cluster/base/infrastructure/37-backup-system/restic-secret.yaml` (`sops -d` shows it). It
  encrypts both repositories. With the age key it can be read from Git, but only while the
  repository is reachable. Escrowed on its own, it and the vault's keys open the AWS copy from any
  machine.
- **Vault coordinates:** bucket `homelab-recovery-vault`, region `eu-central-1`, prefixes `restic/`
  and `recovery-points/`. The promoter and retention keys are in `recovery-aws-promoter.yaml` and
  `recovery-aws-retention.yaml`; the MFA admin identity (Part B, section 2) covers this bucket
  too.

## A9. Drills

The drills are Argo Workflows in [`recovery/`](recovery/). Each restores into scratch targets and
removes what it made.

| Drill | Proves |
|---|---|
| [`drill-files-aws.yaml`](recovery/drill-files-aws.yaml) | Immich's photos from AWS alone: the newest `REMOTE_VERIFIED` point, found from its record in the vault, restored with `--verify` |
| [`drill-sqlite-aws.yaml`](recovery/drill-sqlite-aws.yaml) | Paperless' database from AWS: `integrity_check` and the policy's restore checks |
| [`drill-filer-aws.yaml`](recovery/drill-filer-aws.yaml) | route C end to end: `filer-db` from AWS, staged in the vault, recovered into a scratch cluster, checked, the staged copy removed |
| [`drill-pitr.yaml`](recovery/drill-pitr.yaml) | route A: Keycloak thirty minutes back, from the live archive |

```bash
kubectl apply -f docs/runbooks/recovery/objectstores.yaml
kubectl create -f docs/runbooks/recovery/drill-filer-aws.yaml
kubectl get workflows -n backup-system -l recovery-scratch=true
kubectl delete -f docs/runbooks/recovery/objectstores.yaml    # once they are done
```

Run them at least quarterly, and read their output, not just their phase.

| Date | Drill | Result |
|---|---|---|
| 2026-09-13 | `drill-files-aws` | passed: 6 files of `recovery-point-immich-manual-rkqt8`, each re-read against its hashes |
| 2026-09-13 | `drill-sqlite-aws` | passed on the second run: `integrity_check` ok, every restore check above its minimum |
| 2026-09-13 | `drill-filer-aws` | passed: base backup `20260913T074324` recovered from the vault, 4000 `filemeta` rows (minimum 1000), staged copy removed |
| 2026-09-13 | `drill-pitr` | passed on the second run: Keycloak recovered to 09:22 UTC, every restore check above its minimum |
| 2026-09-13 | by hand: A2, A3 streamed from the local repository, A5 route A and the copy, A7's repository | passed: stream byte-identical to restic's verified restore; dump and restore into an empty and into a recreated database, row counts identical, ownership kept; the new repository's chunker polynomial identical to AWS's |

What the first run found, each fixed in the procedures above:

- A point-in-time target ten minutes back failed: idle Keycloak had committed nothing since, and
  PostgreSQL needs a commit after the target to stop at (A5, route A).
- The SQLite drill's check found no file: the database sits under `.recovery/`, which Python's
  `glob` skips. The backup was fine; the check was not.
- Paths written as `/data/...`, the way `restic snapshots` prints them, are not found: a
  snapshot's root is the volume's root (A1).
- Git Bash rewrote every `/path` argument (the note at the top of this part).

---

# Part B — The current pipeline (ADR-005), until cutover

ADR-005 records the design and the invariants these procedures exist to protect; read it
first if you need to know *why* a step is shaped the way it is. It deliberately carries no
status, so for what is currently outstanding see `docs/backlog.md`.

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

Create the relay credential:

```bash
./scripts/make-relay-credential.sh
```

This reads the Terraform outputs directly and writes
`cluster/base/infrastructure/34-backup/backup-relay-credential.yaml`, already
SOPS-encrypted, and adds it to the kustomization. The relay's AWS secret key never
appears in a terminal, a shell history, or a chat window -- it goes from
`terraform output` into a file that is encrypted before it is ever placed inside the
repository. Encryption needs only the age *public* key, which is committed in
`.sops.yaml`, so no private key material is required to run it.

It fails closed. Plaintext is written to a temp file outside the working tree and
shredded on every exit path, and the script refuses to place anything in the repo
unless it can confirm both that the output contains ciphertext and that the
plaintext secret does not appear in it. `sops` exiting 0 is not by itself proof the
values were encrypted -- a `path_regex` that does not match produces a passthrough
copy with no error.

Commit and merge that, and let Flux apply the secret **before** unsuspending. Then
unsuspend as a separate change:

```
cluster/base/infrastructure/34-backup/backup-relay.yaml  ->  suspend: false
```

The order is not cosmetic. Unsuspending first leaves the CronJob firing against a
missing secret, which presents as `CreateContainerConfigError` rather than anything
naming the real cause.

Trigger the first run by hand rather than waiting for 05:00, and confirm objects
actually arrive in the vault rather than trusting the exit code:

```bash
kubectl create job -n longhorn-system relay-test --from=cronjob/backup-relay
kubectl logs -n longhorn-system -l job-name=relay-test --tail=40
aws s3 ls s3://homelab-backup-vault/longhorn/ --recursive | head
```

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

**Assembled 2026-08-25.** Verify it again at each quarterly drill -- an escrow is only
as good as the last time somebody read it back, and the contents below drift: the
`.age.key` fingerprint changes if SOPS is ever re-keyed, and Terraform state changes on
every apply.

Without this the remote copy is unreadable after total homelab loss, and nothing else
in this design addresses that. What follows is the record of what it holds and why.

Nothing automated can do this. Every other part of the backup chain runs on a schedule;
this one is a deliberate manual act, because anything that copied these files
automatically would have to hold them somewhere -- and that somewhere is what the escrow
exists to survive.

Store off-site, outside AWS and outside the cluster: paper in a safe, or a hardware token
plus a printed copy at a second location.

The recovery system adds the restic password and its own vault's coordinates to this escrow:
see Part A, A8.

### Required -- unrecoverable if lost

**`.age.key`** (189 bytes). The SOPS/age **private** key. Without it every encrypted
manifest in the repo is noise: the relay and auditor AWS credentials, the Longhorn
backup-target credential, the SeaweedFS S3 secret, and the rest. There is no way to
reconstruct it and no second copy anywhere.

Identify it by its public half, `age1wgk7g6...`, which is the recipient `.sops.yaml`
encrypts to. Named explicitly because a second age key sits beside it on the same machine
(`.talos-backup-age.key`, a different key for etcd snapshots, which ADR-005 scopes out).
Escrowing the wrong one would look identical until a recovery was attempted.

It is 189 bytes of ASCII, so printing it on paper is entirely practical and survives
things a USB stick does not.

**The `backup-admin` access key and MFA recovery seed.** These exist nowhere on disk by
design -- Terraform deliberately creates the admin user without an access key, because
state holding the one credential able to destroy locked backups would put it on the
machine whose loss this vault exists to survive.

**Bucket coordinates**: `homelab-backup-vault`, `eu-central-1`, prefixes `longhorn/`,
`seaweedfs/`, `inventory/`. Plus a printed copy of section 1, which is the restore order.

### Optional -- recoverable, but slowly

These are worth escrowing for speed, not survival. A recovery is possible without them; it
is just longer and more error-prone at the worst possible moment.

**`bootstrap/terraform/terraform.tfstate`** (~88 KB). Maps Terraform to the live AWS
resources. Rebuildable with `terraform import`, one resource at a time, against a bucket
you can still see in the console -- tedious rather than impossible.

Take `terraform.tfstate`, **not** `terraform.tfstate.backup`. The `.backup` file is
Terraform's own copy of the *previous* state, written before each apply; it lags by at
least one change and has already been observed missing a vault resource the current state
had. Note that state contains the relay and auditor secret keys in plaintext, so it needs
the same handling as the age key.

**`bootstrap/config.json`** (~2.3 KB). AWS credentials, GitHub and Cloudflare tokens.
Every value in it can be rotated and reissued, so this is pure convenience -- it saves
reissuing half a dozen credentials while already recovering from a disaster.

**A clone or bundle of the Flux repository**, or at minimum its URL plus credentials. The
repository lives on GitHub, so this only matters if GitHub access is part of what was
lost.

**Do not store any of this in the backup bucket.** An escrow that requires the thing it
protects is not an escrow.

Verify readability during each quarterly drill. An escrow nobody has ever read is a guess.

---

## 6. Restoring a database

Every database in this cluster is protected by a logical dump and by nothing else. There is
no volume-level copy of any of them, deliberately: the dumps are small,
`backup-db-restore-test` replays the newest of each into a throwaway server every night and
checks that the data came back (ADR-011), and a Longhorn snapshot of a Postgres data volume was
either unrestorable or redundant depending on the database. See
the `not-backed-up` list in `34-backup/backup-policy.yaml` for which, and why.

The practical consequence is that **a database is never restored by attaching a volume.**
Create an empty cluster, then replay.

| Database | Dump | Written by | Restore into |
|---|---|---|---|
| Immich | `s3://db-backups/immich/immich-<ts>.sql.gz` | `immich-postgres-backup`, 02:40 | `immich-pg` CNPG cluster (since 2026-09-13) |
| Keycloak | `s3://db-backups/keycloak/keycloak-<ts>.sql.gz` | `keycloak-postgres-backup`, 02:50 | `keycloak-pg` CNPG cluster |
| Paperless | `s3://db-backups/paperless/paperless-db-<ts>.sqlite3.gz` | `paperless-sqlite-backup`, 02:45 | the Paperless data volume |
| SeaweedFS filer | `s3://filer-metadata/filer-<ts>.sql.gz` | `seaweedfs-filer-postgres-backup`, hourly | `filer-meta-pg` CNPG cluster |

The Postgres dumps are taken with `--clean --if-exists`, so they drop and recreate their own
objects and can be replayed into a database that already has content. Replay as `postgres` over
the pod's local socket, where CNPG authenticates by peer; the dump's ownership statements hand
every object back to the application's role:

```bash
gzip -dc immich-<ts>.sql.gz \
  | kubectl exec -i -n immich immich-pg-1 -c postgres -- psql -U postgres -d immich
```

**Immich carries one caveat, and it is not an error when you see it.** The dump excludes
`geodata_places` and `naturalearth_countries` -- reference data Immich ships and re-imports
on its own, roughly 15MB compressed that would otherwise dominate a 247KB dump. After a
restore, reverse geocoding returns nothing until Immich has re-imported them. Assets,
albums, faces and search are unaffected. Wait for the import rather than concluding the
dump was short.

**Order matters for Keycloak.** Restore its database *before* anything that authenticates
through it, or every OIDC login fails in a way that looks like a Keycloak fault rather than
an empty realm.

---

## 7. Diagnosing a backup that "succeeded" but did not work

The failure modes in this cluster have consistently been silent. Each of these reported
success while being wrong:

| Symptom | Reality | How it was found |
|---|---|---|
| Backup job green daily | Dumping a superseded database, frozen at migration time | Nothing links an app's DB to its backup job (#263) |
| Objects list with correct sizes | Chunks gone; every `GET` fails | Real read of every object |
| Velero backups completing | No PVC data at all — fs-backup skips hostPath | Restore attempt |
| Alert group configured | Filtered on a label that does not exist; matched nothing for weeks | Reading the raw metric's label set |
| Longhorn healthy | Not scraped at all; zero metrics collected | `up{namespace="longhorn-system"}` empty |
| Longhorn `Backup` CRs `Completed`, target `available` | Every block in the backupstore unreadable; nothing restorable | Fetching one block (§8) |

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

---

## 8. Repairing a phantom backupstore from the offsite vault

Section 7 covers individual objects. On 2026-08-31 the same failure was found to affect the
**entire Longhorn backupstore**: every block in `s3://longhorn-backups` listed at its correct
size and failed to download. Longhorn itself reported no problem — `BackupTarget` showed
`available: true`, and all six `Backup` CRs showed `state: Completed` with correct byte
counts, because Longhorn also trusts the metadata half. Every Longhorn backup in the cluster
was unrestorable while the CRs, the UI and the recurring jobs all said otherwise.

Detect it by fetching a block rather than listing one:

```bash
B=$(aws $E s3 ls s3://longhorn-backups/ --recursive | grep '\.blk' | head -1 | awk '{print $NF}')
aws $E s3 ls "s3://longhorn-backups/$B"          # reports a size
aws $E s3 cp "s3://longhorn-backups/$B" /tmp/b   # this is the test that matters
```

### Repair

The offsite relay's mirror is the recovery source. Run the relay's own copy in reverse, from
a Job built on the `backup-relay` CronJob's pod spec so both remotes are already configured:

```bash
rclone copy "aws:${AWS_BUCKET}/longhorn" s3:longhorn-backups \
  --transfers 4 --checkers 8 --ignore-times
```

**`--ignore-times` is mandatory.** The dead objects carry the correct size and a plausible
modtime, so rclone's default size+modtime comparison skips every one of them, transfers
nothing, and exits 0. This is the same trap as verifying a backup by its listing — the
comparison inspects metadata that survived, not the bytes that did not.

Afterwards, confirm the *same block* that previously failed now downloads and matches its
listed size.

### Restoring a volume without touching the app's PVCs

Longhorn's CSI restore path needs the snapshot CRDs, which are not installed here. Restore
into a temporary volume and copy the data across instead — this leaves the application's own
PVCs (plain manifests under `cluster/base/infrastructure/`) untouched, so Flux never fights
the recovery and no reclaim policy can delete live data:

1. Create a `Volume` in `longhorn-system` with `spec.fromBackup` set to the backup's
   `status.url` (`s3://longhorn-backups@us-east-1/?backup=<name>&volume=<pv>`), mirroring the
   original's `size`, `numberOfReplicas`, `dataEngine` and `frontend`.
2. Wait for `status.restoreRequired: false`.
3. Bind it with a `PersistentVolume` (`storageClassName: ""`, `volumeHandle` = the volume
   name, `claimRef` pointing at a temporary PVC) plus that PVC.
4. Scale the application to zero, then run one pod mounting the restored volume read-only and
   the live volume read-write, and `cp -a`. **Guard the copy on the source being non-empty** —
   a restore that silently produced nothing would otherwise wipe the destination.
5. Scale the application back up, then delete the temporary PVC, PV and `Volume`.

Verify with the application's own consistency check rather than a file count where one
exists; paperless-ngx logs `Sanity checker detected no issues`, which confirms every database
record resolves to a real file on disk.
