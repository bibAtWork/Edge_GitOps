# Backup recovery runbook

This runbook covers the recovery system ([ADR-012](../adr/0012-recovery-system.md)), the only backup
system since the cutover on 2026-09-13. It keeps validated recovery points in restic, locally and in
the AWS vault `homelab-recovery-vault`, plus barman point-in-time recovery for PostgreSQL. Its
procedures were drilled against the real repositories (A9). The pipeline it replaced (ADR-005), and
that pipeline's procedures, are in Git history.

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
      image: docker.io/library/alpine:3.24
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
else, and the filer's database alone lists objects that fail on read.
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

**Create the local repository before resuming, with the AWS repository's chunker parameters.**
Restic backup steps now initialise a missing local repository themselves on first use
(restic-dataset.yaml, cnpg-dataset.yaml), but with restic's own default parameters -- correct for
a genuinely new deployment, wrong here: AWS already holds this application's real data, chunked
under the OLD parameters, and a repository created with different ones deduplicates against
nothing. Doing it here first, before the schedules resume (they are still suspended), is what
keeps that working -- give it the AWS repository's chunker parameters, so data splits into the
same blobs and promotion deduplicates against what AWS already holds instead of uploading
everything again. restic reaches the local store through rclone, inside the shell:

```bash
kubectl exec -n backup-system recovery-shell -c rclone -- \
  rclone serve restic local:recovery/restic --addr 127.0.0.1:8000 &
kubectl exec -n backup-system recovery-shell -c restic-aws -- sh -c \
  'RESTIC_FROM_PASSWORD="$RESTIC_PASSWORD" restic -r rest:http://127.0.0.1:8000/ init --from-repo "$RESTIC_REPOSITORY" --copy-chunker-params'
kill %1
```

Then resume the schedules: the same loop, with `"suspend":false`.

## A8. Escrow and the admin identity

Without the escrow, the AWS copy is unreadable after total loss, and nothing automated can provide
it: anything that copied these items automatically would have to hold them somewhere, and that
somewhere is what the escrow exists to survive. Store it off-site, outside AWS and outside the
cluster: paper in a safe, or a hardware token plus a printed copy at a second location. Read it
back at each quarterly drill (A9).

**Required -- unrecoverable if lost:**

- **`.age.key`**, the SOPS/age private key. Without it every encrypted manifest in the repository
  is noise, including the vault credentials below. Identify it by its public half, the recipient
  in `.sops.yaml`.
- **The restic password**, `RESTIC_PASSWORD` in
  `cluster/base/infrastructure/37-backup-system/restic-secret.yaml` (`sops -d` shows it). It
  encrypts both repositories. With the age key it can be read from Git, but only while the
  repository is reachable. Escrowed on its own, it and the vault's keys open the AWS copy from any
  machine.
- **The admin identity's access key and MFA recovery seed** (below). They exist nowhere on disk,
  by design.
- **Vault coordinates:** bucket `homelab-recovery-vault`, region `eu-central-1`, prefixes `restic/`
  and `recovery-points/`. The promoter and retention keys are in `recovery-aws-promoter.yaml` and
  `recovery-aws-retention.yaml`.

**Optional -- recoverable, but slowly:**

- `bootstrap/terraform/terraform.tfstate`. Not the `.backup` file, which lags one change behind.
  It holds the promoter and retention secret keys in plaintext, so treat it like the age key.
- `bootstrap/config.json`.
- A clone of this repository, if GitHub access is part of what was lost.

**Never store any of it in the vault.**

### The admin identity (MFA)

`backup-admin` is the identity meant for this: interactive-only, its key lives in the password
manager, never in a Secret, and not in Terraform state. Its own policy is conditioned on
`aws:MultiFactorAuthPresent`, which **evaluates false for a long-lived access key used directly**,
so exchange the key for session credentials first. It is not, strictly, the *only* identity that
can delete an object version or bypass Governance retention: since PR #632 the bucket policy's
deny exempts `backup-admin`, root, and any other principal while using an MFA session (the same
`aws:MultiFactorAuthPresent` condition), if that principal's own permissions grant the action --
`backup-admin` is simply the identity actually set up to hold it. No long-lived key, from any
identity, can ever do this.

```bash
aws sts get-session-token \
  --serial-number arn:aws:iam::<account>:mfa/<device> \
  --token-code <code> \
  --duration-seconds 3600
```

Export the returned `AccessKeyId`, `SecretAccessKey` and `SessionToken`, then work normally.
Without this step, the first emergency deletion looks like a broken policy rather than a working
one.

## A9. Drills

The drills are Argo Workflows in [`recovery/`](recovery/). Each restores into scratch targets and
removes what it made. The end-to-end tests of the whole system, failures included, are in
[`recovery-system-tests.md`](../recovery-system-tests.md).

Each file here only submits its drill -- it names a `workflowTemplateRef` (required by
`controller.workflowRestrictions.templateReferencing: Strict`, argo-workflows.yaml) pointing at the
Flux-managed WorkflowTemplate of the same name (`37-backup-system/workflow-templates/drill-*.yaml`),
which carries the actual steps.

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
