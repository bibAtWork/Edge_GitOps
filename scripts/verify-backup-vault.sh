#!/usr/bin/env bash
# Reads the ADR-002 backup vault effective configuration back from AWS and
# asserts the properties the design depends on.
#
# This exists because the definition of done asks for a read-back rather than a
# look at the Terraform. Those differ: Terraform reports what it last applied,
# not what is in force. A console edit, a partially-failed apply, or a resource
# removed from state all leave Terraform claiming a configuration AWS is not
# enforcing -- and every failure checked here is silent, surfacing only when a
# restore is attempted.
#
# Run interactively with admin credentials:
#   ./scripts/verify-backup-vault.sh homelab-backup-vault
#
# Exit code is the number of failed assertions.
set -uo pipefail

BUCKET="${1:-homelab-backup-vault}"
FAIL=0

pass() { printf '  [ PASS ]  %s\n' "$1"; }
fail() { printf '  [ FAIL ]  %s\n' "$1"; FAIL=$((FAIL + 1)); }
info() { printf '\n== %s ==\n' "$1"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 127; }; }

need aws
need jq

info "Bucket: ${BUCKET}"

info "Versioning and Object Lock"
V=$(aws s3api get-bucket-versioning --bucket "$BUCKET" --output json 2>/dev/null | jq -r '.Status // "None"')
if [ "$V" = "Enabled" ]; then pass "versioning enabled"; else fail "versioning is '$V', expected Enabled"; fi

OL=$(aws s3api get-object-lock-configuration --bucket "$BUCKET" --output json 2>/dev/null)
OLM=$(echo "$OL" | jq -r '.ObjectLockConfiguration.Rule.DefaultRetention.Mode // "none"')
OLD=$(echo "$OL" | jq -r '.ObjectLockConfiguration.Rule.DefaultRetention.Days // 0')
if [ "$OLM" = "GOVERNANCE" ]; then pass "Object Lock mode GOVERNANCE"; else fail "Object Lock mode is '$OLM', expected GOVERNANCE"; fi
if [ "$OLD" -ge 1 ] 2>/dev/null; then pass "Object Lock default retention ${OLD}d"; else fail "Object Lock retention is '$OLD'"; fi

info "Encryption"
ALG=$(aws s3api get-bucket-encryption --bucket "$BUCKET" --output json 2>/dev/null \
  | jq -r '.ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm // "none"')
if [ "$ALG" = "AES256" ]; then
  pass "SSE-S3 (AES256)"
elif [ "$ALG" = "aws:kms" ]; then
  # Not a style preference: a CMK whose deletion can be scheduled is a backdoor
  # around Object Lock. Schedule the key for deletion and every locked object
  # becomes permanently unreadable while retention still reports healthy.
  fail "SSE-KMS in use -- ADR-002 rejects this; a schedulable CMK bypasses Object Lock"
else
  fail "encryption is '$ALG', expected AES256"
fi

info "Block Public Access"
BPA=$(aws s3api get-public-access-block --bucket "$BUCKET" --output json 2>/dev/null | jq -r '.PublicAccessBlockConfiguration')
for k in BlockPublicAcls BlockPublicPolicy IgnorePublicAcls RestrictPublicBuckets; do
  if [ "$(echo "$BPA" | jq -r ".$k")" = "true" ]; then pass "$k"; else fail "$k is not true"; fi
done

info "Lifecycle rules"
LC=$(aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" --output json 2>/dev/null)

# THE critical assertion. A rule expiring current versions without a filter would
# delete live deduplicated blocks that older backups still reference -- silently,
# and discovered only at restore time.
#
# The `// {}` and `// []` defaults are load-bearing and must not be changed to
# `// empty`. On a rule with no Tag, `.Filter.Tag // empty` yields an empty
# stream, `empty | length` yields nothing, and `select()` then drops the rule
# from consideration entirely -- so an unfiltered expiry rule reports as clean.
# Verified against a synthetic blanket-90d config, which this catches and the
# `// empty` form did not.
UNFILTERED=$(echo "$LC" | jq '[.Rules[]
  | select(.Status == "Enabled")
  | select(.Expiration.Days != null)
  | select(((.Filter.Tag // {}) | length) == 0)
  | select(((.Filter.And.Tags // []) | length) == 0)
  | select(((.Filter.Prefix // .Filter.And.Prefix // "") | length) == 0)
  ] | length')
if [ "$UNFILTERED" = "0" ]; then
  pass "no unfiltered current-version expiration rule"
else
  fail "$UNFILTERED unfiltered current-version expiration rule(s) -- these delete live backup data"
  echo "$LC" | jq -r '.Rules[] | select(.Expiration.Days != null) | "          rule: " + .ID'
fi

TAGRULE=$(echo "$LC" | jq '[.Rules[] | select(.Filter.Tag.Key == "lifecycle" and .Filter.Tag.Value == "prunable")] | length')
if [ "$TAGRULE" -ge 1 ]; then pass "tag-gated prune rule present"; else fail "no lifecycle=prunable rule -- pruning cannot work"; fi

NC=$(echo "$LC" | jq -r '[.Rules[].NoncurrentVersionExpiration.NoncurrentDays // empty] | max // 0')
if [ "$NC" -gt "$OLD" ] 2>/dev/null; then
  pass "noncurrent expiration ${NC}d exceeds lock window ${OLD}d"
else
  fail "noncurrent expiration ${NC}d does not exceed lock window ${OLD}d -- Lifecycle will fight active locks"
fi

MPU=$(echo "$LC" | jq '[.Rules[] | select(.AbortIncompleteMultipartUpload != null)] | length')
if [ "$MPU" -ge 1 ]; then pass "incomplete multipart abort configured"; else fail "no multipart abort rule -- interrupted relays bill silently"; fi

info "Inventory"
INV=$(aws s3api list-bucket-inventory-configurations --bucket "$BUCKET" --output json 2>/dev/null | jq '[.InventoryConfigurationList[]?] | length')
if [ "${INV:-0}" -ge 1 ]; then pass "$INV inventory configuration(s)"; else fail "no inventory configured -- the reconciler has nothing to diff against"; fi

info "In-cluster credentials hold no delete permission"
# ADR-002 asks for this to be attempted, not inferred. A policy that reads
# correctly and evaluates differently is the entire reason for testing it.
if [ -n "${RELAY_ACCESS_KEY_ID:-}" ] && [ -n "${RELAY_SECRET_ACCESS_KEY:-}" ]; then
  KEY="probe/delete-denial-check"
  # A real body, not /dev/null. S3 rejects a zero-length PutObject against an
  # Object Lock bucket, so an empty probe reports the write as denied and the
  # check reads as a broken relay policy when the policy is correct.
  PROBE_BODY="$(mktemp)"
  trap 'rm -f "$PROBE_BODY"' EXIT
  if AWS_ACCESS_KEY_ID="$RELAY_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$RELAY_SECRET_ACCESS_KEY" \
     printf 'adr005-probe
' > "$PROBE_BODY" &&      aws s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$PROBE_BODY" >/dev/null 2>&1; then
    pass "relay can write (expected)"
  else
    fail "relay cannot write -- the relay will not work"
  fi

  if AWS_ACCESS_KEY_ID="$RELAY_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$RELAY_SECRET_ACCESS_KEY" \
     aws s3api delete-object --bucket "$BUCKET" --key "$KEY" >/dev/null 2>&1; then
    fail "relay CAN DELETE -- violates the central ADR-002 constraint"
  else
    pass "relay denied DeleteObject"
  fi
else
  echo "          skipped: set RELAY_ACCESS_KEY_ID / RELAY_SECRET_ACCESS_KEY to test"
fi

if [ -n "${AUDITOR_ACCESS_KEY_ID:-}" ] && [ -n "${AUDITOR_SECRET_ACCESS_KEY:-}" ]; then
  if AWS_ACCESS_KEY_ID="$AUDITOR_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$AUDITOR_SECRET_ACCESS_KEY" \
     aws s3api delete-object --bucket "$BUCKET" --key "probe/delete-denial-check" >/dev/null 2>&1; then
    fail "auditor CAN DELETE -- violates the central ADR-002 constraint"
  else
    pass "auditor denied DeleteObject"
  fi

  # The tagging grant must be limited to the one key the Lifecycle rule filters
  # on; otherwise it could satisfy any future tag-filtered rule.
  if AWS_ACCESS_KEY_ID="$AUDITOR_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$AUDITOR_SECRET_ACCESS_KEY" \
     aws s3api put-object-tagging --bucket "$BUCKET" --key "probe/delete-denial-check" \
     --tagging "TagSet=[{Key=unauthorized,Value=x}]" >/dev/null 2>&1; then
    fail "auditor wrote an arbitrary tag key -- the s3:RequestObjectTagKeys condition is not effective"
  else
    pass "auditor denied non-lifecycle tag keys"
  fi
else
  echo "          skipped: set AUDITOR_ACCESS_KEY_ID / AUDITOR_SECRET_ACCESS_KEY to test"
fi

info "${FAIL} failed assertion(s)"
exit "$FAIL"
