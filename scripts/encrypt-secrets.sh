#!/usr/bin/env bash
# Helper: encrypt all secret.yaml files with SOPS using the age key in .sops.yaml.
# Run after filling in placeholder values and before committing.
#
# Usage:
#   ./scripts/encrypt-secrets.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Encrypting secret files..."

find "${REPO_ROOT}/cluster" -name "secret.yaml" -o \
  -name "*-secret.yaml" -o \
  -name "admin-secret.yaml" -o \
  -name "aws-secret.yaml" -o \
  -name "cloudflare-secret.yaml" -o \
  -name "oauth-secret.yaml" | while read -r file; do
  if grep -q "REPLACE_WITH" "${file}"; then
    echo "  SKIPPING (still has placeholder): ${file}"
    continue
  fi
  if sops --decrypt "${file}" &>/dev/null; then
    echo "  Already encrypted: ${file}"
    continue
  fi
  echo "  Encrypting: ${file}"
  sops --encrypt --in-place "${file}"
done

echo ""
echo "Done. Verify with: git diff"
echo "Only stringData/data fields should be encrypted."
