# Edge GitOps — Talos Kubernetes Home Lab

Production-grade, fully automated Kubernetes home lab using Talos Linux + FluxCD GitOps.

## Profiles

| | 3-node HA | 1-node |
|---|---|---|
| Control plane | 3-member etcd quorum | Single member |
| Node failure tolerance | 1 node | Total outage |
| SeaweedFS replication | Cross-node (`001`) | Dual-disk via collections |
| Monthly cost | ~$12–14 | ~$5–6 |

## Stack

- **OS**: Talos Linux (immutable, no SSH, API-driven)
- **CNI**: Cilium (Gateway API, Hubble, kube-proxy replacement, WireGuard configured)
- **GitOps**: FluxCD v2 + SOPS/Age encrypted secrets
- **Storage**: Longhorn (block storage for application PVCs, and the default StorageClass) + SeaweedFS (S3-compatible object storage: the recovery system's local repository and PostgreSQL WAL archives, and the Zot registry). local-path is retained for exactly one volume that can use neither — the SeaweedFS filer metadata database — see [ADR-008](./docs/adr/0008-storage-mechanisms.md)
- **Observability**: OpenTelemetry + VictoriaMetrics stack + Grafana
- **Backup**: the recovery system ([ADR-012](./docs/adr/0012-recovery-system.md)): restore-tested recovery points in restic, from Longhorn snapshot clones and CloudNativePG base backups, promoted to an Object-Lock AWS vault and orchestrated by Argo Workflows
- **Registry**: Zot (OCI-native) + Trivy Operator (vulnerability scanning), with a Trivy/Renovate bridge that reports images carrying critical CVEs
- **Ingress**: Envoy Gateway (Gateway API), fronting every application ([ADR-001](./docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md))
- **Identity**: Keycloak (OIDC) with flattened group-based RBAC ([ADR-002](./docs/adr/0002-flattened-hierarchical-rbac.md)), enforced at the Gateway: Envoy performs the OIDC flow, and OPA authorizes the resulting request via `ext_authz` ([ADR-006](./docs/adr/0006-policy-engines-by-layer.md))
- **Databases**: CloudNativePG operator (Keycloak, Immich, SeaweedFS filer metadata), with WAL archiving by the barman-cloud plugin
- **Policy & runtime security**: Kyverno (admission policy) and OPA (request authorization) split by layer rather than by function ([ADR-006](./docs/adr/0006-policy-engines-by-layer.md)), Falco (runtime detection), Kubescape (NSA/MITRE posture scanning)
- **VPN**: Tailscale Kubernetes Operator
- **Certs**: cert-manager + Let's Encrypt DNS-01 via Cloudflare
- **Auto-upgrade**: system-upgrade-controller (Talos OS) + Renovate (Helm charts + CVE alerts)

## Repository Layout

```
cluster/
├── base/                    # Shared by both profiles
│   ├── 00-bootstrap/        # Namespaces, LimitRanges, SOPS
│   └── infrastructure/      # Components 00-37
├── overlays/
│   ├── 3-node/              # ← Flux path for HA cluster
│   └── 1-node/              # ← Flux path for single node
bootstrap/
├── config.json.template     # ← fill this in once; bootstrap reads it
├── ansible/                 # Ansible orchestrator + tool installer roles
├── scripts/                 # Bootstrap, config apply, secret rotation, DR
└── terraform/               # AWS S3 + IAM for the recovery vault
docs/                        # Architecture decisions, disaster recovery
```

## Bootstrap

> **Prerequisites:** A Linux-based OS (or macOS) is required on the machine running the bootstrap. The Ansible playbook and shell scripts do not support Windows natively — use WSL2 if on Windows.

### 1. Install tools

```bash
ansible-galaxy collection install -r bootstrap/ansible/requirements.yml
ansible-playbook -i bootstrap/ansible/inventory.yml bootstrap/ansible/install-tools.yml
```

This installs `talosctl`, `kubectl`, `flux`, `sops`, `age`, `terraform`, and `helm` into `~/.local/bin`. Make sure that directory is on your PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

---

### 2. Prepare externally

Gather credentials from external services — nothing gets edited manually in the repo; everything goes into `bootstrap/config.json` in the next step.

#### Nodes

- Boot each machine from the [Talos Linux ISO](https://github.com/siderolabs/talos/releases) and note the IP address(es)
- For 1-node: note the disk WWIDs — the bootstrap will prompt if they aren't in config.json

#### GitHub

Create (or choose) a GitHub repository and a **fine-grained personal access token** scoped to it with **Contents — Read & Write** and **Metadata — Read**.

#### Cloudflare (for TLS certs and DNS)

Create a **Custom API Token** at [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens) (the "Edit zone DNS" template works) with:

- **Zone > Zone — Read**
- **Zone > DNS — Edit**

Scope it to your specific zone(s), not all zones.

#### Tailscale (for VPN access)

Create an **OAuth client** at `login.tailscale.com/admin/settings/oauth` with:

- **Devices — Read & Write**
- **Auth Keys — Write**

Also add the device tag (e.g. `tag:k8s`) to your tailnet ACL `tagOwners` before the operator starts.

#### AWS (for offsite backups)

Ensure you have an AWS account with permissions to create S3 buckets, IAM users and a budget. Bootstrap runs Terraform to provision the recovery vault ([ADR-012](./docs/adr/0012-recovery-system.md)); export `TF_VAR_budget_alert_email`, the address for its cost alerts, before running it.

---

### 3. Configure config.json

Copy the template and fill in every field:

```bash
cp bootstrap/config.json.template bootstrap/config.json
$EDITOR bootstrap/config.json
```

`bootstrap/config.json` is gitignored — it never gets committed. All the values you collected above go here:

```jsonc
{
  "cluster":   { "name": "homelab", "letsencrypt_email": "you@example.com" },
  "node":      { "ip": "192.168.1.10", "subnet": "192.168.1.0/24",
                 "primary_disk": "/dev/disk/by-id/...",
                 "backup_disk":  "/dev/disk/by-id/..." },
  "github":    { "owner": "...", "repo": "...", "branch": "main", "token": "..." },
  "aws":       { "region": "eu-central-1", "access_key_id": "...", "secret_access_key": "..." },
  "cloudflare":{ "api_token": "..." },
  "tailscale": { "oauth_client_id": "...", "oauth_client_secret": "..." },
  "grafana":   { "admin_password": "..." },
  "seaweedfs": { "admin_access_key_id": "", "admin_secret_access_key": "" }
}
```

`seaweedfs` credentials are auto-generated and saved back to `config.json` if left empty.

`node.primary_disk` and `node.backup_disk` can be omitted — the bootstrap will prompt interactively if they're missing.

---

### 4. Configure SOPS age key (one-time)

SOPS needs an age key to encrypt secrets before committing. Generate one, update `.sops.yaml`, then point the environment at it so `encrypt-secrets.sh` can find it at runtime:

```bash
age-keygen -o .age.key
# prints: Public key: age1...
export SOPS_AGE_KEY_FILE="$(pwd)/.age.key"
```

Update `.sops.yaml` with the printed public key:

```yaml
creation_rules:
  - path_regex: .*.yaml
    encrypted_regex: ^(data|stringData)$
    age: age1<your-public-key>
```

Keep `.age.key` present until bootstrap finishes — the script uses it to create the `sops-age` Kubernetes secret. Store it offline and delete the local copy after bootstrap completes.

---

### 5. Run bootstrap

```bash
# Single node
./bootstrap/scripts/bootstrap-1node.sh

# 3-node HA
./bootstrap/scripts/bootstrap-3node.sh
```

No environment variables to export — everything comes from `config.json`. The script is idempotent; re-running it resumes from where it left off.

The bootstrap handles end-to-end:

1. **All `REPLACE_WITH_*` placeholders** filled from `config.json` via `apply-config.py`
2. **SOPS encryption** of every secret file in `cluster/`
3. **Talos machine config** generation, apply, etcd bootstrap, kubeconfig retrieval
4. **talosconfig** injected into the system-upgrade-controller secret automatically
5. **Flux bootstrap** from the GitHub repo
6. **Terraform** (AWS S3 + IAM) — the recovery vault, then its two AWS credentials, written SOPS-encrypted by `scripts/make-recovery-credentials.sh`

After the script completes, commit and push the encrypted secrets Flux needs:

```bash
git add cluster/
git commit -m "chore: apply cluster config"
git push
```

---

### 6. Run post-deploy checks

After Flux has reconciled (check: `flux get kustomizations`):

```bash
PROFILE=1-node ./bootstrap/scripts/post-deploy.sh
# or: PROFILE=3-node ./bootstrap/scripts/post-deploy.sh
```

Flux creates the SeaweedFS buckets itself (`01-seaweedfs/bucket-init-job.yaml`); this waits for SeaweedFS, then lists the buckets and the recovery system's schedules.

---

### Updating a single credential later

If you need to rotate or add a credential without re-running the full bootstrap, update the value in `config.json` and run `apply-config.py` directly:

```bash
# Re-apply everything (e.g. after rotating the Cloudflare token)
python3 bootstrap/scripts/apply-config.py
```

Then commit and push the re-encrypted files.

---

### AWS offsite backup target

The recovery vault, its two in-cluster identities and a cost budget are provisioned by the bootstrap script (step 5, `terraform apply`), and `scripts/make-recovery-credentials.sh` writes the two credentials as SOPS-encrypted Secrets without any manual copy-paste.

To re-run Terraform independently (e.g. to add a second cluster):

```bash
export AWS_REGION=eu-central-1 CLUSTER_NAME=homelab
./bootstrap/scripts/setup-aws.sh
```

## Encryption & Security

### Traffic encryption

| Layer | Mechanism |
|---|---|
| Inter-node pod traffic | WireGuard configured but no-op on single-node (no inter-node traffic) |
| Same-node pod traffic | No pod-to-pod encryption (SPIRE mTLS disabled — races with Cilium bootstrap) |
| Internet-bound egress | HTTPS-only enforced via `CiliumClusterwideNetworkPolicy` |
| Git secrets at rest | SOPS + Age (SOPS keypair) |
| AWS S3 — recovery vault | SSE-S3, and restic encrypts every blob client-side with the escrowed repository password ([ADR-012](./docs/adr/0012-recovery-system.md)) |

### Storage encryption (at rest)

| Layer | Mechanism |
|---|---|
| Node disks (STATE, EPHEMERAL, both NVMe user volumes) | **None** — plaintext at rest by decision ([ADR-007](./docs/adr/0007-no-disk-encryption.md)) |
| Kubernetes `Secret` objects in etcd | secretbox via Talos `cluster.secretboxEncryptionSecret`, scoped to `resources: [secrets]` |
| Everything else in etcd (ConfigMaps, object metadata) | Not encrypted — outside the provider scope |
| Offsite recovery points | restic, client-side; the repository password is escrowed offline |

Disk encryption and Secret encryption are separate mechanisms and are easy to conflate. Only
the former protects against physical possession of a drive, and it is deliberately not enabled;
the secretbox key sits in the machine config on the same unencrypted partition as the
ciphertext, so Secret encryption does **not** substitute for it. See
[ADR-007](./docs/adr/0007-no-disk-encryption.md) for the threat ranking and the disposal
procedure this implies.

### Network policies

Default deny-all ingress/egress with explicit allow rules:

- **Cluster-internal**: all pod-to-pod and pod-to-service traffic within the cluster (required for DNS, Flux controllers, and service mesh)
- DNS (port 53)
- Recovery system egress to AWS S3, from `backup-system` only
- SeaweedFS internal cluster traffic
- Monitoring scrape
- **Internet egress: HTTPS (port 443) only** — pods needing plain HTTP must add an explicit per-namespace policy

## Automated Patching

### Normal updates (all dependencies)

Renovate Bot opens PRs for:
- Helm chart version bumps (via `helm-values` + Flux HelmRepository)
- Talos Linux version updates (via `regexManagers` on machineconfigs and SUC plans)
- Ansible tool versions (via comment-driven `regexManagers` in `group_vars/all.yml`)
- Docker image tags in cluster YAML files

All updates have a **1-day minimum release age** before a PR is opened, allowing time for release artifacts to stabilize.

Merge the Renovate PR → Flux reconciles → rolling upgrade begins.

### Security vulnerability alerts (CVE-driven)

Renovate's native `vulnerabilityAlerts` (backed by the OSV database) opens PRs for any dependency with a known vulnerability:

- **Minimum release age: 6 hours** (fast-track for critical patches)
- PRs are labeled `security` and `urgent`
- OSV vulnerability alerts enabled (`osvVulnerabilityAlerts: true`)

This covers Helm chart dependencies, Docker images, and Go/npm/Python packages referenced in the repo. For application-level vulnerability scanning, Trivy Operator runs continuously in the cluster.

### Talos OS auto-upgrade

system-upgrade-controller watches the Talos GitHub releases channel and applies upgrades node-by-node (`exclusive: true`, one node at a time). Control-plane upgrades cordon the node before starting.

## Secret Rotation

Use `bootstrap/scripts/rotate-secrets.py` for key and credential rotation:

```bash
# Rotate the SOPS age key (re-encrypt all Git secrets)
export SOPS_AGE_KEY_FILE=/path/to/sops.age.key
./bootstrap/scripts/rotate-secrets.py sops-age

# After updating .sops.yaml to remove the old key, finalize:
./bootstrap/scripts/rotate-secrets.py sops-age --phase2

# Update a single credential in a SOPS-encrypted secret file
./bootstrap/scripts/rotate-secrets.py credential \
  --file cluster/base/infrastructure/01-seaweedfs/s3-secret.yaml \
  --key admin_access_key_id
```

**Age key rotation rules:**
- During SOPS key rotation, keep both old and new private keys offline until Phase 2 is committed and deployed.

## Disaster Recovery

See [`docs/disaster-recovery.md`](./docs/disaster-recovery.md) for the full instruction plan.

Quick reference:

```bash
# Full cluster rebuild from Git; application data is then restored by hand
# from the recovery system (docs/runbooks/backup-recovery.md, A7)
python3 bootstrap/scripts/dr.py full

# Add a new node to an existing cluster
python3 bootstrap/scripts/dr.py add-node
```

## Backup Strategy (3-2-1)

The recovery system ([ADR-012](./docs/adr/0012-recovery-system.md)), run by Argo Workflows in
`backup-system`:

| Copy | Storage | Retention | Mechanism |
|---|---|---|---|
| Primary (live data) | Longhorn (local NVMe, ext4 block devices) | — | — |
| PostgreSQL point-in-time | SeaweedFS, one WAL archive per database | 7 days | CNPG barman-cloud plugin, continuous |
| Local recovery points | SeaweedFS `recovery` bucket, restic | 7 daily, 3 weekly, 3 monthly | recovery-point workflows, 01:00 |
| Off-site | AWS S3 recovery vault, Object Lock (Governance) | 1 weekly, 3 monthly | promotion, 03:00 |

A recovery point counts only once every dataset in it passed its restore test. Files are restored
and checked against their hashes, SQLite is integrity-checked, and each PostgreSQL copy is
recovered into a scratch cluster and queried. Promotion copies only validated points, and verifies
each one in AWS. Alerts fire on a missed RPO or a missing offsite copy, and an hourly reconciler
submits the run a missed guarantee needs. Restore procedures are in
[the recovery runbook](./docs/runbooks/backup-recovery.md).

## Architecture

See [`docs/adr/`](./docs/adr/) for architecture decision records, plus
[`docs/network-architecture.md`](./docs/network-architecture.md) and
[`docs/backup-architecture.md`](./docs/backup-architecture.md) for topic-specific deep dives.

## Technical Debt

_No open items._
