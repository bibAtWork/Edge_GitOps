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
- **CNI**: Cilium (Gateway API, Hubble, kube-proxy replacement, WireGuard + SPIRE mTLS)
- **GitOps**: FluxCD v2 + SOPS/Age encrypted secrets
- **Storage**: SeaweedFS (S3-compatible, CSI driver)
- **Observability**: OpenTelemetry + VictoriaMetrics stack + Grafana
- **Backup**: Velero (SeaweedFS local) + talos-backup etcd (AWS S3 offsite)
- **Registry**: Zot (OCI-native) + Trivy Operator (vulnerability scanning)
- **VPN**: Tailscale Kubernetes Operator
- **Certs**: cert-manager + Let's Encrypt DNS-01 via Cloudflare
- **Auto-upgrade**: system-upgrade-controller (Talos OS) + Renovate (Helm charts + CVE alerts)

## Repository Layout

```
cluster/
├── base/                    # Shared by both profiles
│   ├── 00-bootstrap/        # Namespaces, SOPS, talos-backup CronJob
│   └── infrastructure/      # HelmReleases 01-15
├── overlays/
│   ├── 3-node/              # ← Flux path for HA cluster
│   └── 1-node/              # ← Flux path for single node
bootstrap/
├── ansible/                 # Ansible orchestrator + tool installer roles
├── scripts/                 # Bootstrap, AWS provisioning, secret rotation, DR
└── terraform/               # AWS S3 + KMS + IAM for offsite backups
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

### 2. Prepare manually

#### Nodes

- Boot each machine from the [Talos Linux ISO](https://github.com/siderolabs/talos/releases)
- Note the IP address(es) of the node(s)
- For 1-node only: the bootstrap script will query the node for available disks and prompt you to choose. You can enter either a simple device path (`/dev/nvme0n1`, `/dev/sda`) or a stable by-id path (`/dev/disk/by-id/nvme-eui...`). By-id paths are preferred for production as they survive reboots without changing, but simple paths work fine.
- Update the `advertisedSubnets` in the machine config patches to match your network before bootstrapping:
  - 1-node: `cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml`
  - 3-node: `cluster/overlays/3-node/talos-machineconfigs/controlplane.yaml`

  ```yaml
  cluster:
    etcd:
      advertisedSubnets:
        - 192.168.178.0/24  # ← replace with your actual subnet
  ```

#### GitHub

- Create (or choose) a GitHub repository to store the cluster state
- Create a **fine-grained personal access token** scoped to that repo with:
  - **Contents** — Read and Write
  - **Metadata** — Read

#### Cloudflare (for TLS certs and DNS)

- Create a **Custom API Token** at `dash.cloudflare.com/profile/api-tokens` with:
  - **Zone > Zone — Read**
  - **Zone > DNS — Edit**
  - Scoped to the specific zone(s) you're managing (use the "Edit zone DNS" template as a starting point)
- You will add this token to `cluster/base/infrastructure/06-cert-manager/operator/cloudflare-secret.yaml` and `cluster/base/infrastructure/08-external-dns/cloudflare-secret.yaml` (SOPS-encrypted)
- Update the `REPLACE_WITH_YOUR_EMAIL` placeholder in `cluster/base/infrastructure/06-cert-manager/config/clusterissuer.yaml` with your email before pushing

#### Tailscale (for VPN access)

- Create an **OAuth client** at `login.tailscale.com/admin/settings/oauth` with:
  - **Devices — Read & Write**
  - **Auth Keys — Write**
- Add the device tag (e.g. `tag:k8s`) to your tailnet's ACL `tagOwners` before the operator starts
- You will add the `client_id` / `client_secret` to `cluster/base/infrastructure/14-tailscale-operator/operator/tailscale-oauth-secret.yaml` (SOPS-encrypted)

#### AWS (for offsite backups)

- Ensure you have an AWS account with permissions to create S3 buckets, KMS keys, and IAM users

#### Environment variables

Export these before running the bootstrap:

```bash
export GITHUB_TOKEN=<your-token>
export AWS_ACCESS_KEY_ID=<your-key>
export AWS_SECRET_ACCESS_KEY=<your-secret>
export AWS_DEFAULT_REGION=eu-central-1
```

If your organization uses AWS SSO, generate temporary credentials via the CLI instead:

```bash
aws sso login --profile <your-profile>
export $(aws configure export-credentials --profile <your-profile> --format env | xargs)
```

---

### 3. Run bootstrap

```bash

# 3-node HA
export NODE1_IP=192.168.1.10 NODE2_IP=192.168.1.11 NODE3_IP=192.168.1.12
export VIP=192.168.1.100
export GITHUB_OWNER=<your-org> GITHUB_REPO=<your-repo>
./bootstrap/scripts/bootstrap-3node.sh

# Single node (disk selection is prompted interactively)
export NODE_IP=192.168.1.10
export GITHUB_OWNER=<your-org> GITHUB_REPO=<your-repo>
./bootstrap/scripts/bootstrap-1node.sh
```

The bootstrap scripts handle:
1. Age keypair generation (SOPS key + talos-backup key) — **store both offline and delete from disk**
2. Talos machine config generation + apply
3. etcd bootstrap, kubeconfig retrieval
4. SOPS secret injection, Flux bootstrap from Git
5. Terraform apply (AWS S3 + KMS + IAM for offsite backups)

---

### 4. Run post-deploy tasks

After Flux has reconciled SeaweedFS (check: `kubectl get helmrelease -n seaweedfs`):

```bash
PROFILE=1-node ./bootstrap/scripts/post-deploy.sh
# or: PROFILE=3-node ./bootstrap/scripts/post-deploy.sh
```

This creates the required SeaweedFS S3 buckets (`etcd-backups`, `velero-backups`, `zot-registry`) and the `velero-seaweedfs-credentials` Kubernetes secret that Velero uses to authenticate against the local SeaweedFS S3 endpoint. On 1-node it also configures bucket-to-collection routing to isolate backup data to the backup disk.

---

### 5. Activate secret encryption (post-bootstrap)

The bootstrap prints the generated age **public key**. Update `.sops.yaml` with it:

```yaml
creation_rules:
  - path_regex: .*.yaml
    encrypted_regex: ^(data|stringData)$
    age: age1<your-public-key>
```

Then fill in the placeholder secrets across `cluster/` (search for `REPLACE_WITH_*`):

- **`REPLACE_WITH_YOUR_EMAIL`** in `cluster/base/infrastructure/06-cert-manager/config/clusterissuer.yaml` — your Let's Encrypt registration email
- **Cloudflare API token** in `cluster/base/infrastructure/06-cert-manager/operator/cloudflare-secret.yaml` and `cluster/base/infrastructure/08-external-dns/cloudflare-secret.yaml`
- **Tailscale OAuth** (`client_id`, `client_secret`) in the tailscale namespace secret
- **Velero AWS credentials** in `velero/velero-aws-credentials` — the `cloud` key must be an AWS credentials file (not `key:secret`):

  ```ini
  [default]
  aws_access_key_id = <id from terraform output>
  aws_secret_access_key = <secret from terraform output>
  ```

Encrypt and push:

```bash
./bootstrap/scripts/encrypt-secrets.sh
git add cluster/
git commit -m "chore: add encrypted secrets"
git push
```

Flux will pick up the commit and finish reconciling the cluster.

---

### AWS offsite backup targets

AWS resources (S3 buckets, KMS key, IAM users) are provisioned by the bootstrap script (Phase 5 — `terraform apply`). The Terraform outputs include the IAM access key and secret for the Velero and talos-backup IAM users; store these as SOPS-encrypted secrets as described in the step above.

If you need to re-run Terraform independently (e.g. to add a second cluster):

```bash
export AWS_REGION=eu-central-1 CLUSTER_NAME=homelab
./bootstrap/scripts/setup-aws.sh
```

SeaweedFS buckets (`etcd-backups`, `velero-backups`, `zot-registry`) are created by `post-deploy.sh` (step 4 above), not by Flux. On 1-node, `post-deploy.sh` also pins each bucket to a specific SeaweedFS volume collection for disk isolation.

## Encryption & Security

### Traffic encryption

| Layer | Mechanism |
|---|---|
| Inter-node pod traffic | WireGuard (transparent, no per-app config) |
| Same-node pod traffic | Cilium SPIRE mutual TLS (SPIFFE identities) |
| Internet-bound egress | HTTPS-only enforced via `CiliumClusterwideNetworkPolicy` |
| etcd snapshots at rest | Age encryption (talos-backup keypair) |
| Git secrets at rest | SOPS + Age (SOPS keypair) |
| AWS S3 objects | SSE-KMS (managed KMS key, auto-rotation) |

### Network policies

Default deny-all ingress/egress with explicit allow rules:

- **Cluster-internal**: all pod-to-pod and pod-to-service traffic within the cluster (required for DNS, Flux controllers, and service mesh)
- DNS (port 53)
- Velero egress (AWS S3)
- SeaweedFS internal cluster traffic
- Monitoring scrape
- talos-backup API access
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

# Rotate the talos-backup age key (updates Kubernetes secret)
./bootstrap/scripts/rotate-secrets.py backup-age

# Update a single credential in a SOPS-encrypted secret file
./bootstrap/scripts/rotate-secrets.py credential \
  --file cluster/base/00-bootstrap/talos-backup/secret.yaml \
  --key AWS_SECRET_ACCESS_KEY
```

**Age key rotation rules:**
- SOPS key and talos-backup key are independent keypairs. Rotating one does not affect the other.
- During SOPS key rotation, keep both old and new private keys offline until Phase 2 is committed and deployed.
- During talos-backup key rotation, keep both keys offline until all snapshots encrypted with the old key have expired (7 days).

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
| Primary (live data) | SeaweedFS primary collection | — | SeaweedFS CSI |
| Local backup | SeaweedFS backup collection / second server | 4 days (96h) daily | Velero |
| Offsite backup | AWS S3 Glacier Deep Archive | 90 days (weekly), 365 days (monthly) | Velero |
| etcd snapshots | SeaweedFS local + AWS S3 | 7 days | talos-backup |

Velero runs CSI VolumeSnapshots for consistent PVC backups alongside manifest backups.

## Architecture

See [`docs/architecture.md`](./docs/architecture.md) for full design decisions and ADRs.
