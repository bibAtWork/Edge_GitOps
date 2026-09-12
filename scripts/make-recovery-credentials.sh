#!/usr/bin/env bash
# Write the recovery system's two AWS credentials as SOPS-encrypted Secrets
# (ADR-012), from the outputs of bootstrap/terraform/recovery-vault.tf.
#
#   recovery-aws-promoter   restic copy / barman copy into the vault; no delete
#                           beyond restic's own lock files
#   recovery-aws-retention  restic forget --prune / barman retention; deletes
#                           are delete markers under Object Lock
#
# Both carry the AWS repository's location. The restic password is the local
# repository's (restic-local): one secret to keep outside the cluster, not two.
#
# Run after `terraform apply` in bootstrap/terraform:
#   ./scripts/make-recovery-credentials.sh
# Then commit the two files it writes. Plaintext never reaches the repository:
# it is written to a private temporary directory, encrypted in place, checked
# for leftover plaintext, and only then installed.
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${REPO_ROOT}/bootstrap/terraform"
DEST_DIR="${REPO_ROOT}/cluster/base/infrastructure/37-backup-system"

die() { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }
need terraform
need sops
[ -f "${REPO_ROOT}/.sops.yaml" ] || die "no .sops.yaml at repo root; cannot determine the age recipient"
[ -d "$TF_DIR" ] || die "no terraform directory at ${TF_DIR}"
[ -d "$DEST_DIR" ] || die "no 37-backup-system directory at ${DEST_DIR}"

TMPDIR_SECURE="$(mktemp -d -t recovery-cred.XXXXXX)"
cleanup() {
  for f in "$TMPDIR_SECURE"/*; do
    [ -f "$f" ] || continue
    command -v shred >/dev/null 2>&1 && shred -u "$f" 2>/dev/null || rm -f "$f"
  done
  rm -rf "$TMPDIR_SECURE" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$TF_DIR"
read_output() {
  local name="$1" value
  value="$(terraform output -raw "$name" 2>/dev/null)" || die \
    "terraform output '${name}' is unavailable. Has 'terraform apply' been run for recovery-vault.tf?"
  [ -n "$value" ] || die "terraform output '${name}' is empty"
  printf '%s' "$value"
}

BUCKET="$(read_output recovery_vault_bucket)"
REGION="$(read_output recovery_vault_region)"
REPOSITORY="s3:s3.${REGION}.amazonaws.com/${BUCKET}/restic"

write_secret() {
  local name="$1" key_id="$2" secret="$3" file="$4"
  case "$key_id" in AKIA*) : ;; *) die "${name}: access key id does not look like an AWS access key" ;; esac
  [ "${#secret}" -ge 20 ] || die "${name}: secret access key is implausibly short"
  local tmp="${TMPDIR_SECURE}/${name}.yaml"
  cat > "$tmp" <<YAML
# ${name}: see scripts/make-recovery-credentials.sh and ADR-012. SOPS-encrypted;
# regenerate with that script after rotating the key in Terraform.
apiVersion: v1
kind: Secret
metadata:
  name: ${name}
  namespace: backup-system
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: "${key_id}"
  AWS_SECRET_ACCESS_KEY: "${secret}"
  AWS_BUCKET: "${BUCKET}"
  AWS_DEFAULT_REGION: "${REGION}"
  RESTIC_REPOSITORY_AWS: "${REPOSITORY}"
YAML
  sops --config "${REPO_ROOT}/.sops.yaml" --encrypt --in-place "$tmp" \
    || die "${name}: sops encryption failed; plaintext discarded, nothing written"
  grep -q 'ENC\[AES256_GCM' "$tmp" || die "${name}: no SOPS ciphertext in the output; refusing to write"
  if grep -qF -- "$secret" "$tmp"; then
    die "${name}: PLAINTEXT SECRET FOUND in the encrypted output; refusing to write"
  fi
  for k in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_BUCKET AWS_DEFAULT_REGION RESTIC_REPOSITORY_AWS; do
    grep -qE "^\s+${k}: ENC\[" "$tmp" || die "${name}: ${k} was not encrypted; refusing to write"
  done
  install -m 0644 "$tmp" "${DEST_DIR}/${file}"
  echo "wrote ${DEST_DIR#"${REPO_ROOT}/"}/${file}  (key ${key_id:0:4}************, secret not displayed)"
}

write_secret recovery-aws-promoter \
  "$(read_output recovery_promoter_access_key_id)" \
  "$(read_output recovery_promoter_secret_access_key)" \
  recovery-aws-promoter.yaml
write_secret recovery-aws-retention \
  "$(read_output recovery_retention_access_key_id)" \
  "$(read_output recovery_retention_secret_access_key)" \
  recovery-aws-retention.yaml

echo
echo "bucket:     ${BUCKET}"
echo "repository: ${REPOSITORY}"
echo "Next: add both files to ${DEST_DIR#"${REPO_ROOT}/"}/kustomization.yaml and commit them."
