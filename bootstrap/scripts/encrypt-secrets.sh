#!/usr/bin/env bash
# Helper: encrypt all Kubernetes Secret manifests with SOPS using the age key
# in .sops.yaml. Discovery is content-based because valid Secret filenames such
# as recovery-object-store.yaml and github-token.yaml do not contain "secret".
# Run after filling in placeholder values and before committing.
#
# Usage:
#   ./scripts/encrypt-secrets.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "Encrypting secret files..."

find "${REPO_ROOT}/cluster" \( -name "*.yaml" -o -name "*.yml" \) -type f -print0 \
  | while IFS= read -r -d '' file; do
  grep -q '^kind:[[:space:]]*Secret[[:space:]]*$' "${file}" || continue
  if grep -q "REPLACE_WITH" "${file}"; then
    echo "  SKIPPING (still has placeholder): ${file}"
    continue
  fi
  if grep -q '^sops:' "${file}"; then
    if sops --decrypt "${file}" &>/dev/null; then
      echo "  Already encrypted: ${file}"
    else
      # A published template contains ciphertext for its original recipient.
      # apply-config.py rewrites application Secrets; Terraform rewrites the
      # two AWS identities later in bootstrap. Do not attempt to encrypt SOPS
      # ciphertext as plaintext while those downstream-owned files wait.
      echo "  SKIPPING (encrypted for another age key; regenerate with its owner): ${file}"
    fi
    continue
  fi
  echo "  Encrypting: ${file}"
  sops --encrypt --in-place "${file}"
done

echo ""
echo "Done. Verify with: git diff"
echo "Only stringData/data fields should be encrypted."
