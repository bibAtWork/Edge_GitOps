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
- **Storage**: Longhorn (block storage for application PVCs) + SeaweedFS (S3-compatible object storage: Velero backup target, database dumps, Zot registry, Longhorn backup target)
- **Observability**: OpenTelemetry + VictoriaMetrics stack + Grafana
- **Backup**: Longhorn snapshots/backups to local SeaweedFS, relayed one-way to an immutable AWS S3 vault ([ADR-005](./docs/adr/0005-two-stage-backup-relay.md)) + Velero for Kubernetes objects + per-database logical dumps
- **Registry**: Zot (OCI-native) + Trivy Operator (vulnerability scanning), with a Trivy/Renovate bridge that reports images carrying critical CVEs
- **Ingress**: Envoy Gateway (Gateway API), fronting every application ([ADR-001](./docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md))
- **Identity**: Keycloak (OIDC) with flattened group-based RBAC ([ADR-002](./docs/adr/0002-flattened-hierarchical-rbac.md)), enforced at the Gateway via OPA
- **Databases**: CloudNativePG operator (Keycloak, SeaweedFS filer metadata)
- **Policy & runtime security**: Kyverno (admission policy), Falco (runtime detection), Kubescape (NSA/MITRE posture scanning)
- **VPN**: Tailscale Kubernetes Operator
- **Certs**: cert-manager + Let's Encrypt DNS-01 via Cloudflare
- **Auto-upgrade**: system-upgrade-controller (Talos OS) + Renovate (Helm charts + CVE alerts)

## Repository Layout

```
cluster/
├── base/                    # Shared by both profiles
│   ├── 00-bootstrap/        # Namespaces, LimitRanges, SOPS
│   └── infrastructure/      # Components 00-34
├── overlays/
│   ├── 3-node/              # ← Flux path for HA cluster
│   └── 1-node/              # ← Flux path for single node
bootstrap/
├── config.json.template     # ← fill this in once; bootstrap reads it
├── ansible/                 # Ansible orchestrator + tool installer roles
├── scripts/                 # Bootstrap, config apply, secret rotation, DR
└── terraform/               # AWS S3 + IAM for the offsite backup vault
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

Ensure you have an AWS account with permissions to create S3 buckets, KMS keys, and IAM users. Bootstrap will run Terraform to provision these automatically.

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
6. **Terraform** (AWS S3 + IAM) — Velero IAM credentials captured from output and written to `cluster/base/infrastructure/07-velero/aws-secret.yaml` automatically

After the script completes, commit and push the encrypted secrets Flux needs:

```bash
git add cluster/
git commit -m "chore: apply cluster config"
git push
```

---

### 6. Run post-deploy tasks (SeaweedFS)

After Flux has reconciled SeaweedFS (check: `kubectl get helmrelease -n seaweedfs`):

```bash
PROFILE=1-node ./bootstrap/scripts/post-deploy.sh
# or: PROFILE=3-node ./bootstrap/scripts/post-deploy.sh
```

This creates the SeaweedFS S3 buckets (`etcd-backups`, `velero-backups`, `zot-registry`) and the `velero-seaweedfs-credentials` Kubernetes secret that Velero uses to authenticate against the local SeaweedFS S3 endpoint. On 1-node it also pins each bucket to a SeaweedFS volume collection for disk isolation.

---

### Updating a single credential later

If you need to rotate or add a credential without re-running the full bootstrap, update the value in `config.json` and run `apply-config.py` directly:

```bash
# Re-apply everything (e.g. after rotating the Cloudflare token)
python3 bootstrap/scripts/apply-config.py

# Re-apply only the Velero IAM credentials (after Terraform re-run)
python3 bootstrap/scripts/apply-config.py \
  --velero-access-key <key> \
  --velero-secret-key <secret>
```

Then commit and push the re-encrypted files.

---

### AWS offsite backup targets

AWS resources (S3 buckets, KMS key, IAM users) are provisioned automatically by the bootstrap script (step 5, `terraform apply`). Velero IAM credentials are captured from Terraform output and written to the encrypted secret without any manual copy-paste.

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
| AWS S3 — backup vault | SSE-S3 ([ADR-005](./docs/adr/0005-two-stage-backup-relay.md) — SSE-KMS was declined here: a KMS outage or key-policy error makes the vault unreadable exactly when it is needed) |
| AWS S3 — Velero offsite bucket | SSE-KMS (managed key, auto-rotation) |

### Network policies

Default deny-all ingress/egress with explicit allow rules:

- **Cluster-internal**: all pod-to-pod and pod-to-service traffic within the cluster (required for DNS, Flux controllers, and service mesh)
- DNS (port 53)
- Velero egress (AWS S3)
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
# Restore a single namespace from Velero backup
python3 bootstrap/scripts/dr.py namespace

# Full cluster recovery from etcd snapshot + Velero
python3 bootstrap/scripts/dr.py full

# Add a new node to an existing cluster
python3 bootstrap/scripts/dr.py add-node
```

## Backup Strategy (3-2-1)

| Copy | Storage | Retention | Tool |
|---|---|---|---|
| Primary (live data) | Longhorn (local NVMe, ext4 block devices) | — | — |
| Local snapshots | Longhorn, same replica disk | 7 daily | Longhorn RecurringJob |
| Local backups | SeaweedFS `longhorn-backups` bucket | 5 weekly + 6 monthly | Longhorn RecurringJob |
| Local object copy | SeaweedFS buckets staged onto Longhorn | 14 daily | staging CronJob |
| Off-site | AWS S3 `homelab-backup-vault`, Object Lock 21d | matches local | relay CronJob |
| Databases | SeaweedFS `db-backups` bucket | hourly/daily dumps | per-app CronJob |

Velero backs up PVCs through its node-agent DaemonSet (Kopia file-system backup), **not**
CSI VolumeSnapshots — no CSI driver or VolumeSnapshotClass is registered on this cluster.

Longhorn never talks to AWS. Its backup target is the local SeaweedFS endpoint, where it
holds full delete rights and runs retention normally; a separate relay mirrors that
backupstore one-way to a vault whose credentials hold no delete permission of any kind.
See [ADR-005](./docs/adr/0005-two-stage-backup-relay.md) for why, and
[the recovery runbook](./docs/runbooks/backup-recovery.md) for restore order.

## Architecture

See [`docs/architecture.md`](./docs/architecture.md) for full design decisions and ADRs.

## Technical Debt

_No open items._
