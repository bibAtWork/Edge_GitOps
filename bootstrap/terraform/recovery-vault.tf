# ADR-012: the recovery system's independent AWS repository.
#
# A new bucket, not a prefix in the old vault: the recovery system replaces
# the old pipeline, and its repository had to be operationally independent of
# everything it replaced. The old vault (backup-vault*.tf) is decommissioning
# since the cutover (#617, 2026-09-13) and is removed, with its identities
# already gone (#620), once its bucket is empty (docs/backlog.md).
#
# What differs from the old vault, and why:
#
#   The old vault granted no in-cluster identity any delete, and pruned by tag
#   through Lifecycle, because the relay mirrored a Longhorn backupstore that
#   only Longhorn could prune. The recovery vault holds only restic
#   repositories -- barman's WAL archives stay local-only (runbook A1) and are
#   never promoted here -- which prune themselves and must be allowed to: that
#   is what makes a vault shallower than local possible (1 weekly / 3 monthly
#   against 7 / 3 / 3).
#
#   So `retention` may DeleteObject. On a versioned bucket under Object Lock that
#   writes a delete marker and nothing more: the locked version survives, an
#   admin with MFA can bring it back until the lock expires, and only Lifecycle
#   removes it for good, after vault_noncurrent_expiration_days. No in-cluster
#   identity can delete a version, shorten a lock, or change the bucket.
#
#   `promoter` cannot delete at all, except restic's own lock files, which every
#   restic write creates and removes.
#
# Exactly two in-cluster identities, as for the old vault.

resource "aws_s3_bucket" "recovery_vault" {
  bucket              = "${var.cluster_name}-recovery-vault"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "recovery_vault" {
  bucket = aws_s3_bucket.recovery_vault.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "recovery_vault" {
  bucket = aws_s3_bucket.recovery_vault.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.recovery_vault_object_lock_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.recovery_vault]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recovery_vault" {
  bucket = aws_s3_bucket.recovery_vault.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "recovery_vault" {
  bucket                  = aws_s3_bucket.recovery_vault.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# INVARIANT: nothing here expires a CURRENT version. restic and barman decide
# what is garbage; Lifecycle only reclaims what they have already deleted, once
# the lock is long past.
resource "aws_s3_bucket_lifecycle_configuration" "recovery_vault" {
  bucket = aws_s3_bucket.recovery_vault.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.recovery_vault_noncurrent_expiration_days
    }
  }

  rule {
    id     = "clean-delete-markers"
    status = "Enabled"
    filter {}
    expiration {
      expired_object_delete_marker = true
    }
  }

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---- identities -------------------------------------------------------------

resource "aws_iam_user" "recovery_promoter" {
  name = "${var.cluster_name}-recovery-promoter"
}

resource "aws_iam_access_key" "recovery_promoter" {
  user = aws_iam_user.recovery_promoter.name
}

data "aws_iam_policy_document" "recovery_promoter" {
  statement {
    sid    = "WriteAndRead"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.recovery_vault.arn}/*"]
  }

  # restic takes a lock for every write and removes it afterwards. This is the
  # only delete the promoter has, and on this bucket it only writes a delete
  # marker.
  statement {
    sid       = "ReleaseResticLocks"
    effect    = "Allow"
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.recovery_vault.arn}/restic/locks/*"]
  }

  statement {
    sid    = "List"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.recovery_vault.arn]
  }
}

resource "aws_iam_user_policy" "recovery_promoter" {
  name   = "${var.cluster_name}-recovery-promoter"
  user   = aws_iam_user.recovery_promoter.name
  policy = data.aws_iam_policy_document.recovery_promoter.json
}

resource "aws_iam_user" "recovery_retention" {
  name = "${var.cluster_name}-recovery-retention"
}

resource "aws_iam_access_key" "recovery_retention" {
  user = aws_iam_user.recovery_retention.name
}

# restic forget --prune and barman's retention rewrite indexes and remove what
# no retained point references. DeleteObject, never DeleteObjectVersion: every
# delete here is a marker over a version the lock still protects.
data "aws_iam_policy_document" "recovery_retention" {
  statement {
    sid    = "Prune"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.recovery_vault.arn}/*"]
  }

  statement {
    sid    = "List"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.recovery_vault.arn]
  }
}

resource "aws_iam_user_policy" "recovery_retention" {
  name   = "${var.cluster_name}-recovery-retention"
  user   = aws_iam_user.recovery_retention.name
  policy = data.aws_iam_policy_document.recovery_retention.json
}

# The existing admin (interactive, MFA, no Terraform-held key) covers the new
# bucket through its own policy, so backup-vault-iam.tf stays untouched until
# cutover.
data "aws_iam_policy_document" "recovery_admin" {
  statement {
    sid    = "DestructiveObjectOperations"
    effect = "Allow"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:BypassGovernanceRetention",
      "s3:PutObjectRetention",
      "s3:PutObjectLegalHold",
      "s3:GetObjectLegalHold",
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${aws_s3_bucket.recovery_vault.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }

  statement {
    sid    = "BucketConfiguration"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:GetBucketLocation",
      "s3:PutBucketVersioning",
      "s3:PutLifecycleConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:PutBucketObjectLockConfiguration",
      "s3:GetBucketObjectLockConfiguration",
      "s3:PutBucketPolicy",
      "s3:GetBucketPolicy",
    ]
    resources = [aws_s3_bucket.recovery_vault.arn]
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

resource "aws_iam_user_policy" "recovery_admin" {
  name   = "${var.cluster_name}-recovery-admin"
  user   = aws_iam_user.backup_admin.name
  policy = data.aws_iam_policy_document.recovery_admin.json
}

# ---- bucket policy: the deny no Allow can override --------------------------
#
# Same shape as vault_deny_destructive, minus s3:DeleteObject: the retention
# identity needs it, and on this bucket it can only write a delete marker.
#
# Exemption is backup_admin and root, unconditionally, plus anyone else while
# using an MFA session (BoolIfExists on aws:MultiFactorAuthPresent, ANDed with
# the principal check). See backup-vault-policy.tf's vault_deny_destructive
# for the full reasoning -- same fix, same round of code review, same shape:
# an earlier version exempted data.aws_caller_identity.current.arn
# unconditionally (no MFA required), the version after that dropped the
# exemption entirely and broke routine Terraform applies (backup_admin's own
# identity policy has no IAM, KMS or Budgets access, and is scoped only to
# this bucket and the old vault -- nowhere near broad enough to run this
# config's plan, let alone apply one). MFA-gating the exemption instead of
# naming an identity means whichever identity normally runs Terraform here
# can still apply changes to this bucket's own configuration, exactly as
# before, but only while that identity is not just a long-lived key.
#
# Same one-time bootstrapping snag as backup-vault-policy.tf: if the
# no-admin-exemption version of this policy is already live, the apply that
# installs THIS version needs to run as backup_admin's MFA session or root
# -- the general admin identity cannot yet use the exemption it is about to
# gain.
data "aws_iam_policy_document" "recovery_vault_deny_destructive" {
  statement {
    sid    = "DenyDestructiveExceptAdminAndRoot"
    effect = "Deny"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions = [
      "s3:DeleteObjectVersion",
      "s3:BypassGovernanceRetention",
      "s3:PutObjectRetention",
      "s3:PutBucketVersioning",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketPolicy",
    ]
    resources = [
      aws_s3_bucket.recovery_vault.arn,
      "${aws_s3_bucket.recovery_vault.arn}/*",
    ]
    condition {
      test     = "StringNotLike"
      variable = "aws:PrincipalArn"
      values = [
        aws_iam_user.backup_admin.arn,
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
      ]
    }
    condition {
      test     = "BoolIfExists"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "recovery_vault" {
  bucket     = aws_s3_bucket.recovery_vault.id
  policy     = data.aws_iam_policy_document.recovery_vault_deny_destructive.json
  depends_on = [aws_s3_bucket_public_access_block.recovery_vault]
}

# ---- variables and outputs ----------------------------------------------------

variable "recovery_vault_object_lock_days" {
  description = "ADR-012 recovery vault default Object Lock retention, in days (Governance mode). A deleted object stays recoverable by the admin for at least this long."
  default     = 21
}

variable "recovery_vault_noncurrent_expiration_days" {
  description = "Days before noncurrent versions in the recovery vault expire. Must exceed recovery_vault_object_lock_days so Lifecycle never conflicts with an active lock."
  default     = 45
}

# ---- cost ceiling ------------------------------------------------------------
#
# An email when S3 spending approaches the ceiling (about EUR 10 a month). It
# alerts and never blocks: the hard limit is the recovery system's own
# repository cap (250 GiB, recovery-policy's `limits`), which promotion refuses
# to exceed -- and it acts within hours, where AWS billing arrives a day late.
#
# The address is a variable without a default, supplied at apply time
#   TF_VAR_budget_alert_email=you@example.org terraform apply
# and never written to the repository: *.tfvars is not gitignored here, and
# this repository is public. It ends up only in the local, gitignored state.
variable "budget_alert_email" {
  description = "Where AWS Budgets sends the backup-cost warnings. Supply via TF_VAR_budget_alert_email; never commit it."
  type        = string
  sensitive   = true
}

variable "backup_monthly_budget_usd" {
  description = "Monthly S3 spending that triggers the warning, in USD (AWS Budgets bills in USD). About EUR 10."
  default     = 11
}

resource "aws_budgets_budget" "backup_storage" {
  name         = "${var.cluster_name}-backup-storage"
  budget_type  = "COST"
  limit_amount = var.backup_monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Simple Storage Service"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

output "recovery_vault_bucket" {
  description = "ADR-012 recovery vault bucket name"
  value       = aws_s3_bucket.recovery_vault.bucket
}

output "recovery_vault_region" {
  description = "Region of the ADR-012 recovery vault, for the restic repository URL"
  value       = var.aws_region
}

output "recovery_promoter_access_key_id" {
  description = "Access key ID for the recovery system's promoter (write, no delete)"
  value       = aws_iam_access_key.recovery_promoter.id
}

output "recovery_promoter_secret_access_key" {
  description = "Secret access key for the recovery system's promoter"
  value       = aws_iam_access_key.recovery_promoter.secret
  sensitive   = true
}

output "recovery_retention_access_key_id" {
  description = "Access key ID for the recovery system's retention identity (delete markers only)"
  value       = aws_iam_access_key.recovery_retention.id
}

output "recovery_retention_secret_access_key" {
  description = "Secret access key for the recovery system's retention identity"
  value       = aws_iam_access_key.recovery_retention.secret
  sensitive   = true
}
