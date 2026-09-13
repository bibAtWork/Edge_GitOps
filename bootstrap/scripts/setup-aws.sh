#!/usr/bin/env bash
# Provision the AWS resources in bootstrap/terraform.
#
# Run once after bootstrap. Creates the recovery system's AWS side (ADR-012):
#   - the recovery vault: a versioned S3 bucket under Object Lock (Governance)
#   - two in-cluster identities: the promoter (writes, and deletes only restic's
#     own lock files) and retention (deletes, which only write delete markers)
#   - an interactive MFA admin identity, with no access key
#   - a monthly S3 cost budget with email alerts
#
# The older backup buckets (etcd, Velero and the ADR-005 vault) are being
# emptied and are removed once empty; see docs/backlog.md.
#
# Prerequisites:
#   terraform >= 1.6
#   AWS credentials in the environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION)
#   TF_VAR_budget_alert_email: the address the budget alerts go to
#
# Usage:
#   export AWS_REGION=eu-central-1
#   export CLUSTER_NAME=homelab
#   export TF_VAR_budget_alert_email=<address>
#   ./bootstrap/scripts/setup-aws.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="${SCRIPT_DIR}/../terraform"

# --- Validate environment ---

required_env=(AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION TF_VAR_budget_alert_email)
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
echo "1. Write the recovery system's two AWS credentials as SOPS-encrypted Secrets:"
echo "     ./scripts/make-recovery-credentials.sh"
echo ""
echo "2. Commit and push the two files it writes:"
echo "     git add cluster/base/infrastructure/37-backup-system/"
echo "     git commit -m 'feat(backup-system): recovery vault credentials'"
echo "     git push"
echo ""
echo "3. Create the admin identity's access key and MFA device by hand, and keep"
echo "   them with the age key -- see docs/runbooks/backup-recovery.md, Part A."
