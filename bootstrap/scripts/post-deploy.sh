#!/usr/bin/env bash
# Post-deployment tasks: create SeaweedFS buckets and configure routing.
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

SEAWEEDFS_ENDPOINT="http://seaweedfs-s3.seaweedfs.svc:8333"
SEAWEEDFS_KEY=$(kubectl get secret seaweedfs-s3-secret -n seaweedfs -o jsonpath='{.data.admin_access_key_id}' | base64 -d)
SEAWEEDFS_SECRET=$(kubectl get secret seaweedfs-s3-secret -n seaweedfs -o jsonpath='{.data.admin_secret_access_key}' | base64 -d)

echo "Creating velero-seaweedfs-credentials secret..."
kubectl create secret generic velero-seaweedfs-credentials \
  -n velero \
  --from-literal=cloud="$(printf '[default]\naws_access_key_id = %s\naws_secret_access_key = %s\n' "${SEAWEEDFS_KEY}" "${SEAWEEDFS_SECRET}")" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Creating S3 buckets..."
kubectl run aws-cli --rm -it \
  --image=amazon/aws-cli \
  --restart=Never \
  --env="AWS_ACCESS_KEY_ID=${SEAWEEDFS_KEY}" \
  --env="AWS_SECRET_ACCESS_KEY=${SEAWEEDFS_SECRET}" \
  -- sh -c "
    aws s3 mb s3://etcd-backups   --endpoint-url ${SEAWEEDFS_ENDPOINT} &&
    aws s3 mb s3://velero-backups --endpoint-url ${SEAWEEDFS_ENDPOINT} &&
    aws s3 mb s3://zot-registry   --endpoint-url ${SEAWEEDFS_ENDPOINT} &&
    echo 'Buckets created successfully'
  "

if [[ "${PROFILE}" == "1-node" ]]; then
  echo ""
  echo "=== 1-node: Configuring bucket-to-collection routing ==="
  echo "This pins backup buckets to the backup disk volume server."

  kubectl exec -n seaweedfs seaweedfs-master-0 -- weed shell <<'EOF'
s3.bucket.create -name pvs            -collection primary
s3.bucket.create -name zot-registry   -collection primary
s3.bucket.create -name etcd-backups   -collection backup
s3.bucket.create -name velero-backups -collection backup
EOF
  echo "Bucket-to-collection routing configured."
fi

echo ""
echo "=== Verification ==="

echo "Running quick backup test..."
velero backup create post-deploy-test --wait --ttl 1h

echo "Checking Velero backup locations..."
velero backup-location get

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
