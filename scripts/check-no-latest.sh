#!/usr/bin/env bash
# Reject image references using the :latest tag (or bare "latest" as tag value).
# Matches:
#   image: registry/repo:latest
#   image: "registry/repo:latest"
#   tag: latest
#   tag: "latest"
# Does NOT match:
#   channel: https://github.com/.../releases/latest  (URL, not an image ref)
#   # comments containing the word latest
set -euo pipefail

file="$1"
found=0
lineno=0

while IFS= read -r line || [[ -n "$line" ]]; do
  lineno=$((lineno + 1))

  # Skip blank lines and comment lines
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

  # Match: image: <anything>:latest  (with optional quotes, optional trailing comment)
  if [[ "$line" =~ ^[[:space:]]*image:[[:space:]]+[\'\""]?[^[:space:]#\'\"]*:latest[\'\""]?[[:space:]]*(#.*)?$ ]]; then
    echo "$file:$lineno:0: error: image tag must be explicit, not ':latest' ($line)"
    found=1
    continue
  fi

  # Match: tag: latest  or  tag: "latest"  or  tag: 'latest'
  if [[ "$line" =~ ^[[:space:]]*tag:[[:space:]]+[\'\""]?latest[\'\""]?[[:space:]]*(#.*)?$ ]]; then
    echo "$file:$lineno:0: error: image tag must be explicit, not 'latest' ($line)"
    found=1
  fi
done < "$file"

exit $found
