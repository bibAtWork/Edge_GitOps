#!/usr/bin/env bash
# Post-deployment checks. Run after Flux has reconciled the cluster
# (check: flux get kustomizations).
#
# SeaweedFS buckets are created by Flux itself (01-seaweedfs/bucket-init-job.yaml),
# so this only waits for SeaweedFS and reports on the pieces that take longest to
# settle.
#
# Usage:
#   PROFILE=3-node ./bootstrap/scripts/post-deploy.sh
#   PROFILE=1-node ./bootstrap/scripts/post-deploy.sh

set -euo pipefail

PROFILE="${PROFILE:-3-node}"

echo "=== Post-deploy checks (profile: ${PROFILE}) ==="

# Wait for SeaweedFS filer to be ready
echo "Waiting for SeaweedFS filer..."
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/component=filer \
  -n seaweedfs \
  --timeout=300s

echo ""
echo "=== Verification ==="

echo "SeaweedFS buckets:"
printf 's3.bucket.list\n' | kubectl exec -i -n seaweedfs seaweedfs-master-0 -- weed shell

echo "Recovery system schedules (ADR-012):"
kubectl get cronworkflows -n backup-system

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
