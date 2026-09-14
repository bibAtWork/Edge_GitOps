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
# not as a principal. That is what lets the decommissioning rules in
# backup-vault-lifecycle.tf empty the bucket under a blanket delete deny.
data "aws_iam_policy_document" "vault_deny_destructive" {
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
    # A round of code review found this list should also drop
    # data.aws_caller_identity.current.arn, exempted unconditionally by an
    # earlier version so Terraform (which needs PutLifecycleConfiguration etc.
    # to manage the resources below) never locked itself out. Correct problem,
    # wrong fix: it exempted whoever last ran `terraform apply` from this
    # bucket's own destructive-action deny with no MFA condition at all, so a
    # long-lived key with broad IAM permissions lost no capability here just by
    # being the one that happened to apply. Dropping the exemption outright
    # (the next round) broke the opposite way: backup_admin's own identity
    # policy is scoped to only this bucket and recovery_vault (backup-vault-
    # iam.tf), with no IAM, KMS or Budgets access and not even
    # s3:GetBucketVersioning -- not broad enough to run a plan across the rest
    # of this Terraform config at all, let alone apply one. Routine applies
    # need an identity with the account's usual broad permissions (this
    # repo's general admin identity, not defined here); they were never
    # denied by this policy, since none of its actions cover plain reads or
    # anything outside these two buckets.
    #
    # What ties the two together: BoolIfExists on aws:MultiFactorAuthPresent,
    # ANDed with the same principal exemption, in the same condition block.
    # AWS's own semantics for `...IfExists` (IAM user guide, "condition
    # operators"): if the key is present in the request, test it as
    # specified; if the key is ABSENT -- a long-lived access key used
    # directly, with no STS session at all -- evaluate that condition element
    # as true. So `BoolIfExists: {MultiFactorAuthPresent: false}` reads as
    # "true (matches) unless the caller is in an MFA session" -- combined
    # with StringNotLike by AND, the Deny now fires only when BOTH the
    # caller is outside {backup_admin, root} AND no MFA session is present.
    # backup_admin and root stay exempt unconditionally, exactly as before
    # (the StringNotLike arm alone already excludes them, so the AND is
    # false regardless of the second condition). Anyone else -- the general
    # admin identity included -- is exempt only while using an MFA session
    # (`aws sts get-session-token`, same dance backup_admin already
    # requires), and denied otherwise, same as originally intended. No
    # in-cluster identity can ever produce an MFA context, so none of them
    # gain anything here.
    #
    # One-time bootstrapping snag, flagged on code review: if the PREVIOUS
    # version of this policy (no admin exemption at all -- commit b577284)
    # is already live when this change is applied, the apply that INSTALLS
    # this MFA-gated version needs s3:PutBucketPolicy, which that live
    # policy denies to everyone but backup_admin and root. The general admin
    # identity cannot bootstrap itself into the exemption it is about to
    # gain -- this one apply needs to run as backup_admin's MFA session, or
    # root. Every apply after this one can use the new exemption normally.
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

resource "aws_s3_bucket_policy" "vault" {
  bucket = aws_s3_bucket.vault.id
  policy = data.aws_iam_policy_document.vault_deny_destructive.json

  # Public access block must exist first, or S3 can reject a policy it considers
  # potentially public.
  depends_on = [aws_s3_bucket_public_access_block.vault]
}
