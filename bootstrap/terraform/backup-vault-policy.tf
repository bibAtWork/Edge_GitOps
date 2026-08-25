# ADR-005 Task 8: bucket policy, defense in depth.
#
# Deliberately redundant with the IAM policies in backup-vault-iam.tf. Those
# grant nothing destructive, so in a correct configuration this policy denies
# nothing that was ever permitted. It exists to survive the failure mode where
# someone later attaches a broader policy to one of these users, or creates a
# fourth identity and forgets these rules -- an explicit Deny in the bucket
# policy cannot be overridden by any Allow anywhere.
#
# StringNotLike on aws:PrincipalArn rather than NotPrincipal: NotPrincipal
# evaluates surprisingly with roles and service principals, and is widely
# mis-specified. This form is easier to reason about and to read back.
#
# S3 Lifecycle is unaffected by bucket policy -- it executes as the S3 service,
# not as a principal. That is exactly what makes the tag-gated prune rule keep
# working under a blanket delete deny, which is the pivot the whole design turns
# on. Inventory delivery is likewise a service principal, and PutObject is not in
# the denied set below, so it is unaffected too.
data "aws_iam_policy_document" "vault_deny_destructive" {
  # Combined with the Inventory delivery grant: a bucket has exactly one policy,
  # so both statements must be rendered together.
  source_policy_documents = [data.aws_iam_policy_document.vault_inventory_delivery.json]

  statement {
    sid    = "DenyDestructiveExceptAdminAndRoot"
    effect = "Deny"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    # Action names here are validated by S3; IAM does not validate them at all.
    # This list originally carried "s3:PutObjectLockConfiguration", which does
    # not exist -- the bucket-level action is PutBucketObjectLockConfiguration.
    # The IAM policy in backup-vault-iam.tf accepted the invented name without
    # complaint and applied cleanly, silently granting the admin nothing, while
    # PutBucketPolicy rejected the identical string with MalformedPolicy. A
    # permission that IAM reports as granted and AWS never honours is the worse
    # of the two failures, and only this stricter validation surfaced it.
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:BypassGovernanceRetention",
      "s3:PutBucketVersioning",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketPolicy",
      "s3:PutInventoryConfiguration",
    ]

    resources = [
      aws_s3_bucket.vault.arn,
      "${aws_s3_bucket.vault.arn}/*",
    ]

    # Account root is included as break-glass, per ADR-005. Without it, deleting
    # or renaming the admin identity would permanently brick bucket
    # configuration -- the policy would deny every principal able to change the
    # policy, with no way back.
    #
    # The identity currently running Terraform is included for the same reason:
    # it manages the lifecycle, versioning, inventory and policy resources above,
    # and denying it PutLifecycleConfiguration would make this the last apply
    # that ever succeeds. If Terraform later runs as a different principal, that
    # apply fails and is recovered by running once as root.
    condition {
      test     = "StringNotLike"
      variable = "aws:PrincipalArn"
      values = distinct([
        aws_iam_user.backup_admin.arn,
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
        data.aws_caller_identity.current.arn,
      ])
    }
  }
}

resource "aws_s3_bucket_policy" "vault" {
  bucket = aws_s3_bucket.vault.id
  policy = data.aws_iam_policy_document.vault_deny_destructive.json

  # Public access block must exist first, or S3 can reject a policy it considers
  # potentially public.
  depends_on = [aws_s3_bucket_public_access_block.vault]
}
