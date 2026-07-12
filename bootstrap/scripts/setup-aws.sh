#!/usr/bin/env bash
# Provision AWS S3 + KMS + IAM resources for offsite backups.
#
# Run once after bootstrap. Creates:
#   - Two S3 buckets (etcd-backups-offsite, velero-backups-offsite)
#   - KMS key for SSE encryption with automatic rotation
#   - IAM users: velero (S3 + KMS on velero bucket), talos-backup (S3 + KMS on etcd bucket)
#   - S3 Intelligent Tiering → DEEP_ARCHIVE_ACCESS at 180 days (cost saving)
#
# Prerequisites (all in ansible/group_vars/all.yml):
#   terraform >= 1.6
#   AWS credentials in environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION)
#
# Usage:
#   export AWS_REGION=eu-central-1
#   export CLUSTER_NAME=homelab
#   ./scripts/setup-aws.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="${SCRIPT_DIR}/../terraform"

# --- Validate environment ---

required_env=(AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION)
missing=()
for var in "${required_env[@]}"; do
  [[ -z "${!var:-}" ]] && missing+=("$var")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "ERROR: Missing required environment variables: ${missing[*]}" >&2
  echo "       Export them before running this script." >&2
  exit 1
fi

CLUSTER_NAME="${CLUSTER_NAME:-homelab}"

echo "==================================================="
echo " AWS Resource Provisioning"
echo "==================================================="
echo " Cluster:    ${CLUSTER_NAME}"
echo " Region:     ${AWS_REGION}"
echo " Terraform:  ${TERRAFORM_DIR}"
echo "==================================================="
echo ""

cd "$TERRAFORM_DIR"

# --- Init ---
echo ">>> terraform init"
terraform init -upgrade

echo ""

# --- Plan ---
echo ">>> terraform plan"
terraform plan \
  -var="cluster_name=${CLUSTER_NAME}" \
  -var="aws_region=${AWS_REGION}" \
  -out=tfplan

echo ""
read -rp "Apply this plan? [yes/N]: " confirm
if [[ "${confirm,,}" != "yes" ]]; then
  echo "Aborted."
  rm -f tfplan
  exit 0
fi

# --- Apply ---
echo ""
echo ">>> terraform apply"
terraform apply tfplan
rm -f tfplan

echo ""
echo "==================================================="
echo " Outputs"
echo "==================================================="
terraform output

echo ""
echo "==================================================="
echo " Next steps"
echo "==================================================="
echo ""
echo "1. Encrypt and store IAM credentials as Kubernetes secrets:"
echo "   For Velero (key 'cloud' must be an AWS credentials file, not key:secret):"
echo "     kubectl create secret generic velero-aws-credentials \\"
echo "       --from-literal=cloud=\$'[default]\\naws_access_key_id = <id>\\naws_secret_access_key = <secret>\\n' \\"
echo "       -n velero --dry-run=client -o yaml | kubectl apply -f -"
echo ""
echo "   For talos-backup:"
echo "     Add AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY to"
echo "     cluster/base/00-bootstrap/talos-backup/cronjob.yaml env vars"
echo "     (SOPS-encrypted in the secret referenced by the CronJob)."
echo ""
echo "2. Update bucket names in:"
echo "   - cluster/base/infrastructure/07-velero/helmrelease.yaml (BSL aws-s3)"
echo "   - cluster/base/00-bootstrap/talos-backup/cronjob.yaml (S3_BUCKET env var)"
echo ""
echo "3. Commit and push the updated secret files:"
echo "   git add cluster/"
echo "   git commit -m 'chore: configure AWS offsite backup targets'"
echo "   git push"
