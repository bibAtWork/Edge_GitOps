#!/usr/bin/env bash
# Post-deployment tasks: create SeaweedFS buckets and Velero credentials.
# Run after Flux has reconciled SeaweedFS (check: kubectl get helmrelease -n seaweedfs).
#
# Usage:
#   PROFILE=3-node ./scripts/post-deploy.sh
#   PROFILE=1-node ./scripts/post-deploy.sh

set -euo pipefail

PROFILE="${PROFILE:-3-node}"

echo "=== Post-deploy: SeaweedFS bucket setup (profile: ${PROFILE}) ==="

# Wait for SeaweedFS filer to be ready
echo "Waiting for SeaweedFS filer..."
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/component=filer \
  -n seaweedfs \
  --timeout=300s

SEAWEEDFS_KEY=$(kubectl get secret seaweedfs-s3-secret -n seaweedfs -o jsonpath='{.data.admin_access_key_id}' | base64 -d)
SEAWEEDFS_SECRET=$(kubectl get secret seaweedfs-s3-secret -n seaweedfs -o jsonpath='{.data.admin_secret_access_key}' | base64 -d)

echo "Creating velero-seaweedfs-credentials secret..."
kubectl create secret generic velero-seaweedfs-credentials \
  -n velero \
  --from-literal=cloud="$(printf '[default]\naws_access_key_id = %s\naws_secret_access_key = %s\n' "${SEAWEEDFS_KEY}" "${SEAWEEDFS_SECRET}")" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Creating S3 buckets via weed shell..."
printf 's3.bucket.create -name pvs\ns3.bucket.create -name zot-registry\ns3.bucket.create -name etcd-backups\ns3.bucket.create -name velero-backups\ns3.bucket.list\n' \
  | kubectl exec -i -n seaweedfs seaweedfs-master-0 -- weed shell

echo ""
echo "=== Verification ==="

echo "Checking Velero backup locations..."
kubectl get backupstoragelocation -n velero

echo "Checking talos-backup CronJob..."
kubectl get cronjobs -n talos-backup

echo "Checking vulnerability reports (may be empty on first run)..."
kubectl get vulnerabilityreports --all-namespaces 2>/dev/null | head -20 || true

echo "Checking Tailscale operator..."
kubectl get pods -n tailscale

echo ""
echo "=== Post-deploy complete ==="
echo ""
echo "Access your services via Tailscale MagicDNS:"
echo "  Grafana: https://homelab-gateway.<your-tailnet>.ts.net (route: grafana.yourdomain.com)"
echo "  Zot:     https://homelab-gateway.<your-tailnet>.ts.net (route: zot.yourdomain.com)"
