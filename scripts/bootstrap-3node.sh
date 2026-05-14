#!/usr/bin/env bash
# Bootstrap script for the 3-node HA profile.
# Prerequisites: talosctl, kubectl, flux, sops, age, terraform installed locally.
#
# Usage:
#   NODE1_IP=192.168.1.10 NODE2_IP=192.168.1.11 NODE3_IP=192.168.1.12 \
#   VIP=192.168.1.100 \
#   GITHUB_OWNER=<your-github-user> GITHUB_REPO=homelab-cluster \
#   ./scripts/bootstrap-3node.sh

set -euo pipefail

: "${NODE1_IP:?NODE1_IP is required}"
: "${NODE2_IP:?NODE2_IP is required}"
: "${NODE3_IP:?NODE3_IP is required}"
: "${VIP:?VIP is required (virtual IP or first node IP)}"
: "${GITHUB_OWNER:?GITHUB_OWNER is required}"
: "${GITHUB_REPO:?GITHUB_REPO is required}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TALOS_DIR="${REPO_ROOT}/cluster/overlays/3-node/talos-machineconfigs"

echo "=== Phase 1: Key Generation ==="

if [[ ! -f "${REPO_ROOT}/.age.key" ]]; then
  age-keygen -o "${REPO_ROOT}/.age.key"
  echo "SOPS age key generated: .age.key"
  echo "  -> Add the PUBLIC key to .sops.yaml and commit"
  echo "  -> Store the PRIVATE key offline (password manager)"
fi

if [[ ! -f "${REPO_ROOT}/.talos-backup-age.key" ]]; then
  age-keygen -o "${REPO_ROOT}/.talos-backup-age.key"
  echo "talos-backup age key generated: .talos-backup-age.key"
  echo "  -> Store the PRIVATE key offline — needed to decrypt etcd snapshots"
fi

TALOS_BACKUP_PUBLIC_KEY=$(grep 'public key' "${REPO_ROOT}/.talos-backup-age.key" | awk '{print $4}')
echo "talos-backup public key: ${TALOS_BACKUP_PUBLIC_KEY}"

echo ""
echo "=== Phase 2: Talos Config Generation ==="

mkdir -p "${REPO_ROOT}/.talos"

if [[ ! -f "${REPO_ROOT}/.talos/secrets.yaml" ]]; then
  talosctl gen secrets -o "${REPO_ROOT}/.talos/secrets.yaml"
  echo "Talos secrets bundle generated: .talos/secrets.yaml"
  echo "  -> CRITICAL: store this in your password manager — needed to add nodes or recover"
fi

talosctl gen config homelab "https://${VIP}:6443" \
  --with-secrets "${REPO_ROOT}/.talos/secrets.yaml" \
  --config-patch-control-plane "@${TALOS_DIR}/controlplane.yaml" \
  --output-dir "${REPO_ROOT}/.talos/generated"

echo ""
echo "=== Phase 3: Apply Talos Config ==="

for node in "$NODE1_IP" "$NODE2_IP" "$NODE3_IP"; do
  echo "Applying config to ${node}..."
  talosctl apply-config --insecure \
    --nodes "${node}" \
    --file "${REPO_ROOT}/.talos/generated/controlplane.yaml"
done

echo "Waiting 60s for nodes to boot..."
sleep 60

echo ""
echo "=== Phase 4: Bootstrap etcd ==="

talosctl bootstrap --nodes "${NODE1_IP}" \
  --talosconfig "${REPO_ROOT}/.talos/generated/talosconfig"

echo "Waiting 120s for cluster to form..."
sleep 120

talosctl kubeconfig --nodes "${NODE1_IP}" \
  --talosconfig "${REPO_ROOT}/.talos/generated/talosconfig" \
  --force

echo ""
echo "=== Phase 5: Flux Bootstrap ==="

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
  --path="cluster/overlays/3-node" \
  --personal \
  --components-extra=image-reflector-controller,image-automation-controller

echo ""
echo "=== Phase 6: AWS S3 Setup ==="

cd "${REPO_ROOT}/terraform"
terraform init
terraform apply -auto-approve

echo ""
echo "=== Bootstrap Complete ==="
echo ""
echo "Next steps:"
echo "  1. Run ./scripts/post-deploy.sh to create SeaweedFS buckets"
echo "  2. Add SOPS-encrypted secrets for Cloudflare, Tailscale, Velero AWS creds"
echo "  3. Watch Flux reconcile: flux get all --watch"
echo "  4. Check cluster: kubectl get nodes && kubectl get pods -A"
echo ""
echo "IMPORTANT: Delete local key files after storing offline:"
echo "  rm .age.key .talos-backup-age.key"
echo "  (keep .talos/secrets.yaml in password manager, then delete local copy too)"
