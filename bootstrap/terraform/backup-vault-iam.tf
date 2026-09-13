# Identities for the ADR-005 backup vault.
#
# DECOMMISSIONING since the ADR-012 cutover (2026-09-13). The relay and auditor
# identities -- the vault's only in-cluster writer and tagger -- were removed
# with the pipeline that used them. What remains here is shared with the
# recovery vault (recovery-vault.tf): the caller identity, and the interactive
# MFA admin, whose policy below covers this bucket until the bucket is gone.

data "aws_caller_identity" "current" {}

# --- admin: interactive only, never in-cluster --------------------------------
#
# Deliberately has NO aws_iam_access_key resource. Terraform state would
# otherwise hold the one credential able to destroy locked backups, in plaintext,
# on the machine whose loss this vault exists to survive. The key and MFA device
# are created out of band and stored in the operator's password manager -- see
# docs/runbooks/backup-vault-admin.md.
#
# The MFA condition below evaluates FALSE for a long-lived access key used
# directly. Session credentials from `aws sts get-session-token --serial-number
# <mfa-arn> --token-code <code>` are required, or the first emergency deletion
# will look like a broken policy rather than a working one.
resource "aws_iam_user" "backup_admin" {
  name = "${var.cluster_name}-backup-admin"
}

data "aws_iam_policy_document" "backup_admin" {
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
      "s3:PutObjectTagging",
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${aws_s3_bucket.vault.arn}/*"]

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
      "s3:PutInventoryConfiguration",
      "s3:PutBucketPolicy",
      "s3:GetBucketPolicy",
    ]
    resources = [aws_s3_bucket.vault.arn]

    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

resource "aws_iam_user_policy" "backup_admin" {
  name   = "${var.cluster_name}-backup-admin"
  user   = aws_iam_user.backup_admin.name
  policy = data.aws_iam_policy_document.backup_admin.json
}
