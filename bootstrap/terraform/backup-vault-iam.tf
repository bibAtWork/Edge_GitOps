# ADR-002 Task 7: the three vault identities.
#
# The whole design reduces to what these policies do NOT contain. ADR-002's
# central constraint is that no in-cluster credential holds any S3 delete
# permission -- not scoped, not conditioned, not on a lock prefix. That is
# affordable only because Longhorn's backup target is the local SeaweedFS
# endpoint, so nothing here ever needs to delete to make retention work.
#
# Long-lived access keys rather than IRSA: the cluster runs outside AWS.

data "aws_caller_identity" "current" {}

# --- backup-relay: writes the mirror, and nothing else ------------------------
#
# No Delete* of any kind, no Put*Tagging (so a compromised relay cannot mark its
# own output prunable), no BypassGovernanceRetention, no object-lock or
# retention verbs, no bucket configuration. Deny is by omission.
resource "aws_iam_user" "backup_relay" {
  name = "${var.cluster_name}-backup-relay"
}

resource "aws_iam_access_key" "backup_relay" {
  user = aws_iam_user.backup_relay.name
}

data "aws_iam_policy_document" "backup_relay" {
  statement {
    sid    = "WriteObjects"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.vault.arn}/*"]
  }

  statement {
    sid    = "ListBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.vault.arn]
  }
}

resource "aws_iam_user_policy" "backup_relay" {
  name   = "${var.cluster_name}-backup-relay"
  user   = aws_iam_user.backup_relay.name
  policy = data.aws_iam_policy_document.backup_relay.json
}

# --- backup-auditor: reads Inventory, and tags for pruning --------------------
#
# A separate identity from the relay on purpose: if one credential could both
# write objects and tag them prunable, a single compromise could push data and
# then mark it for deletion. Splitting them means that takes two.
resource "aws_iam_user" "backup_auditor" {
  name = "${var.cluster_name}-backup-auditor"
}

resource "aws_iam_access_key" "backup_auditor" {
  user = aws_iam_user.backup_auditor.name
}

data "aws_iam_policy_document" "backup_auditor" {
  statement {
    sid       = "ReadInventoryReports"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.vault.arn}/inventory/*"]
  }

  statement {
    sid    = "ListBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.vault.arn]
  }

  # The tagging grant is the one privileged thing this identity has, so it is
  # constrained to the single tag key the Lifecycle rule filters on. Without the
  # condition, this credential could write arbitrary tags and match any future
  # tag-filtered rule.
  statement {
    sid    = "TagForPruning"
    effect = "Allow"
    actions = [
      "s3:GetObjectTagging",
      "s3:PutObjectTagging",
    ]
    resources = [
      "${aws_s3_bucket.vault.arn}/longhorn/*",
      "${aws_s3_bucket.vault.arn}/seaweedfs/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "s3:RequestObjectTagKeys"
      values   = ["lifecycle"]
    }
  }
}

resource "aws_iam_user_policy" "backup_auditor" {
  name   = "${var.cluster_name}-backup-auditor"
  user   = aws_iam_user.backup_auditor.name
  policy = data.aws_iam_policy_document.backup_auditor.json
}

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
      "s3:PutObjectLockConfiguration",
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
