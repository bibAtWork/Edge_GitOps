# Edge GitOps — Talos Kubernetes Home Lab

Production-grade, fully automated Kubernetes home lab using Talos Linux + FluxCD GitOps.

## Profiles

| | 3-node HA | 1-node |
|---|---|---|
| Control plane | 3-member etcd quorum | Single member |
| Node failure tolerance | 1 node | Total outage |
| Monthly cost | ~$12-14 | ~$5-6 |

## Stack

- **OS**: Talos Linux (immutable, no SSH, API-driven)
- **CNI**: Cilium (Gateway API, Hubble, kube-proxy replacement)
- **GitOps**: FluxCD v2 + SOPS/Age encrypted secrets
- **Storage**: SeaweedFS (S3-compatible, CSI driver)
- **Observability**: OpenTelemetry + VictoriaMetrics stack + Grafana
- **Backup**: Velero (SeaweedFS local) + talos-backup etcd (AWS S3 offsite)
- **Registry**: Zot (OCI-native) + Trivy Operator (scanning)
- **VPN**: Tailscale Kubernetes Operator
- **Certs**: cert-manager + Let's Encrypt DNS-01 via Cloudflare
- **Auto-upgrade**: system-upgrade-controller (Talos OS) + Renovate (Helm charts)

## Repository Layout

```
cluster/
├── base/                    # Shared by both profiles
│   ├── 00-bootstrap/        # Namespaces, SOPS, talos-backup CronJob
│   └── infrastructure/      # HelmReleases 01-15
├── overlays/
│   ├── 3-node/              # ← Flux path for HA cluster
│   └── 1-node/              # ← Flux path for single node
terraform/                   # AWS S3 + KMS + IAM
scripts/                     # Bootstrap runbooks
```

## Bootstrap

```bash
# Prerequisites: talosctl, kubectl, flux, sops, age, terraform

# 3-node HA
./scripts/bootstrap-3node.sh

# Single node
./scripts/bootstrap-1node.sh
```

See [`scripts/`](./scripts/) for full step-by-step runbooks.

## Automated Patching

- **Talos OS**: system-upgrade-controller watches GitHub releases, applies upgrades node-by-node
- **Helm charts**: Renovate Bot opens PRs for chart version bumps; merge to apply
- **etcd backups**: talos-backup CronJob every 6h, age-encrypted, pushed to SeaweedFS S3

## Architecture

See [`docs/architecture.md`](./docs/architecture.md) for full design decisions, ADRs, and runbooks.
