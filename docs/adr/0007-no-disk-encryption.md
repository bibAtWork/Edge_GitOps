# ADR-007: No Disk Encryption — Secret-Scoped Encryption at Rest Only

**Date:** 2026-09-07
**Status:** Accepted
**Related:** [ADR-003](0003-backup-immutability-versioning-only.md), [ADR-005](0005-two-stage-backup-relay.md)

## Context

Talos can encrypt the node's system partitions with LUKS2 through
`machine.systemDiskEncryption`. Nothing in this repository configures it. Whether it should has
surfaced repeatedly, and each time it has become entangled with a second, unrelated mechanism —
which is the reason this ADR exists at all.

**Two different things are routinely confused.**

| | Disk encryption (LUKS) | Secret encryption in etcd |
| --- | --- | --- |
| Protects | bytes on a physical device | `Secret` objects before the API server writes them |
| Configured by | `machine.systemDiskEncryption` | `cluster.secretboxEncryptionSecret` |
| Covers | STATE and EPHEMERAL partitions | only `resources: [secrets]` |
| Threat addressed | someone holding the drive | etcd contents read without the machine config |
| State here | **not configured** | **on, by Talos default** |

Only the first addresses physical disk access. The second is frequently offered as though it
did, and it does not — for a reason spelled out under Consequences.

### What is actually on disk

Verified on the live node. No partition is `crypto_LUKS`; every one is a plain filesystem:

| Device | Size | Filesystem | Holds |
| --- | --- | --- | --- |
| `sda3` STATE | 105 MB | xfs | the machine config, including the secretbox key |
| `sda4` EPHEMERAL | 498 GB | xfs | container images, local-path PVCs, etcd's data directory |
| `nvme0n1p1` `u-seaweedfs` | 129 GB | xfs | S3 objects, including backups |
| `nvme0n1p2` `u-longhorn0` | 365 GB | xfs | Longhorn replicas — Immich and Paperless data |

`systemDiskEncryption` covers the system partitions. The two NVMe user volumes are separate
volume configurations and are not reached by it, so enabling disk encryption fully is three
decisions rather than one — and the bulk of what anyone would actually want protected (photos,
documents, object storage) sits on the volumes the system setting does not cover.

### What Secret encryption is doing

Confirmed by function rather than by reading configuration, on 2026-09-07:

- The apiserver runs with `--encryption-provider-config`; the file declares `secretbox` with an
  `identity` fallback, scoped to `resources: [secrets]`.
- A canary `Secret` was written and then searched for in a raw etcd snapshot: **0 plaintext
  hits**. A `ConfigMap` holding the same sentinel — deliberately outside the encryption scope —
  was found **twice**, proving the search would have located plaintext had any existed.
- 174 `k8s:enc:secretbox:v1:` envelopes against 171 `Secret` objects (the surplus is retained
  etcd revisions), and no other provider prefix anywhere. Nothing predates the encryption, and
  no stale key material remains.

## Decision

**Do not enable disk encryption. Keep Secret-scoped encryption in etcd.**

Everything on the four filesystems above is plaintext at rest, and that is accepted. The single
exception is Kubernetes `Secret` objects, which are encrypted inside etcd.

## Rationale

**1. The threat ranks below the ones being actively defended.** The node is a single machine in
a residence. The attack surface receiving investment — network policy, admission control, OIDC
at the Gateway, runtime detection — targets remote compromise, which is both far likelier and
the only vector that scales. Physical possession of the drive is a narrower scenario.

**2. The protection is narrower than it sounds.** Keyed by `nodeID` or `tpm`, LUKS defends the
case where the *drive* leaves the machine — resale, RMA, disposal, a pulled disk. It does not
defend the case where the *machine* is taken, because the key travels with it and the node
simply boots. For a single box, whole-machine theft is the more plausible of the two, and
encryption does nothing about it.

**3. The cost lands on the hot path.** Encrypting EPHEMERAL taxes every write on the partition
carrying etcd's data directory, every container image unpack, and every local-path PVC — on one
consumer-grade node, with no AES offload guarantee and no second node to absorb the loss.

**4. It adds a failure mode to a recovery story that is already thin.** TPM sealing is what
makes the protection real, and it also lets a firmware update or board change render the disk
unopenable. This cluster's database recovery path is a single untested route; adding a way to
lose the whole disk is a poor trade against a threat ranked third.

**5. Disposal has a better answer.** The one case LUKS genuinely covers — a drive leaving the
building — is handled by secure erase or physical destruction at decommissioning, which costs
nothing during the years the disk is in service.

## Consequences

**Anyone with physical possession of a drive reads everything on it.** Container images, every
Longhorn replica (Immich photos, Paperless documents), all SeaweedFS objects including backups,
and all of etcd's non-`Secret` contents — ConfigMaps, and the metadata of every object in the
cluster.

**Secret encryption does not survive that, and must not be cited as though it does.** The
secretbox key lives in the machine config on STATE, unencrypted, on the same disk as the
ciphertext. Whoever holds the drive holds both halves. Its value is confined to etcd contents
that travel *without* STATE — principally an etcd snapshot copied elsewhere, which is why
offsite snapshots are separately age-encrypted with the private key stored offline.

**Decommissioning is a procedure, not a property.** Because no drive can be assumed unreadable
once it leaves, secure erase or destruction is required for any of the four devices above.

**Reversal is not in-place.** Enabling `systemDiskEncryption` for STATE or EPHEMERAL requires a
wipe and reinstall, and the user volumes would need their own configuration and their own data
migration. This is a decision to revisit at a rebuild, not one to flip on a running node.

## Alternatives Considered

**LUKS keyed by `nodeID`.** Rejected. Protects only against a disk separated from its machine,
carries the write cost in full, and — since the key derives from the node — offers nothing
against whole-machine theft, the likelier scenario here.

**LUKS sealed to the TPM.** Rejected. The strongest option available, and the one that makes the
protection genuine, but it introduces an unopenable-disk failure mode on firmware or hardware
change. Against a third-ranked threat, on a cluster with one untested database recovery path,
the new failure mode outweighs the gain.

**Encrypting EPHEMERAL only.** Rejected as the worst of both. It carries the full write cost on
the busiest partition while leaving the user volumes — where the data actually worth protecting
lives — in plaintext, and yields a claim of "encrypted at rest" that is mostly untrue.

**Disabling Secret encryption for symmetry.** Rejected. It is not the mechanism being declined
here; its scope is `secrets` alone, so there is no measurable cost to reclaim; and removing the
key from a running cluster makes every existing `Secret` undecryptable unless all are rewritten
first. It also remains the only thing protecting an etcd snapshot that travels without the
machine config.
