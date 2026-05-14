#!/usr/bin/env bash
# Bootstrap script for the single-node profile.
# Prerequisites: talosctl, kubectl, flux, sops, age, terraform installed locally.
#
# Usage:
#   NODE_IP=192.168.1.10 \
#   GITHUB_OWNER=<your-github-user> GITHUB_REPO=homelab-cluster \
#   PRIMARY_DISK=/dev/disk/by-id/<your-primary-disk> \
#   BACKUP_DISK=/dev/disk/by-id/<your-backup-disk> \
#   ./scripts/bootstrap-1node.sh

set -euo pipefail

: "${NODE_IP:?NODE_IP is required}"
: "${GITHUB_OWNER:?GITHUB_OWNER is required}"
: "${GITHUB_REPO:?GITHUB_REPO is required}"
: "${PRIMARY_DISK:?PRIMARY_DISK is required (e.g. /dev/disk/by-id/ata-...)}"
: "${BACKUP_DISK:?BACKUP_DISK is required (e.g. /dev/disk/by-id/ata-...)}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TALOS_DIR="${REPO_ROOT}/cluster/overlays/1-node/talos-machineconfigs"

echo "=== Phase 1: Key Generation ==="

if [[ ! -f "${REPO_ROOT}/.age.key" ]]; then
  age-keygen -o "${REPO_ROOT}/.age.key"
  echo "SOPS age key generated: .age.key"
  echo "  -> Add the PUBLIC key to .sops.yaml and commit"
  echo "  -> Store the PRIVATE key offline"
fi

if [[ ! -f "${REPO_ROOT}/.talos-backup-age.key" ]]; then
  age-keygen -o "${REPO_ROOT}/.talos-backup-age.key"
  echo "talos-backup age key generated: .talos-backup-age.key"
  echo "  -> Store OFFLINE — this is the only way to decrypt etcd snapshots after full machine loss"
fi

TALOS_BACKUP_PUBLIC_KEY=$(grep 'public key' "${REPO_ROOT}/.talos-backup-age.key" | awk '{print $4}')

echo ""
echo "=== Phase 2: Talos Config Generation ==="

mkdir -p "${REPO_ROOT}/.talos"

if [[ ! -f "${REPO_ROOT}/.talos/secrets.yaml" ]]; then
  talosctl gen secrets -o "${REPO_ROOT}/.talos/secrets.yaml"
  echo "Talos secrets bundle generated. Store offline — needed for DR re-provisioning."
fi

talosctl gen config homelab "https://${NODE_IP}:6443" \
  --with-secrets "${REPO_ROOT}/.talos/secrets.yaml" \
  --config-patch-control-plane "@${TALOS_DIR}/controlplane.yaml" \
  --output-dir "${REPO_ROOT}/.talos/generated"

echo ""
echo "=== Phase 3: Apply Config and Bootstrap ==="

echo "Applying config to ${NODE_IP}..."
talosctl apply-config --insecure \
  --nodes "${NODE_IP}" \
  --file "${REPO_ROOT}/.talos/generated/controlplane.yaml"

echo "Waiting 60s for node to boot..."
sleep 60

talosctl bootstrap --nodes "${NODE_IP}" \
  --talosconfig "${REPO_ROOT}/.talos/generated/talosconfig"

echo "Waiting 120s for cluster to form..."
sleep 120

talosctl kubeconfig --nodes "${NODE_IP}" \
  --talosconfig "${REPO_ROOT}/.talos/generated/talosconfig" \
  --force

echo ""
echo "=== Phase 4: Flux Bootstrap ==="

kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey="${REPO_ROOT}/.age.key" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create namespace talos-backup --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic talos-backup-age \
  --namespace=talos-backup \
  --from-literal="public-key=${TALOS_BACKUP_PUBLIC_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

flux bootstrap github \
  --owner="${GITHUB_OWNER}" \
  --repository="${GITHUB_REPO}" \
  --path="cluster/overlays/1-node" \
  --personal \
  --components-extra=image-reflector-controller,image-automation-controller

echo ""
echo "=== Phase 5: AWS S3 Setup ==="

cd "${REPO_ROOT}/terraform"
terraform init
terraform apply -auto-approve

echo ""
echo "=== Bootstrap Complete ==="
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/post-deploy.sh --profile=1-node to create SeaweedFS buckets"
echo "     and configure bucket-to-collection routing for disk isolation"
echo "  2. PRIMARY_DISK=${PRIMARY_DISK}"
echo "     BACKUP_DISK=${BACKUP_DISK}"
echo "     Update overlays/1-node/patches/seaweedfs-single.yaml with your disk paths"
echo "  3. Add SOPS-encrypted secrets for Cloudflare, Tailscale, Velero AWS creds"
echo ""
echo "IMPORTANT: Delete local key files after storing offline:"
echo "  rm .age.key .talos-backup-age.key"
