# ADR-002 Task 6: Lifecycle rules and Inventory for the backup vault.
#
# The pruning mechanism lives here. Objects deleted at the source are never
# overwritten in S3, so they stay current versions forever -- versioning and
# noncurrent expiration cannot reach them, and blanket age-based expiration of
# current versions would destroy live deduplicated blocks that older backups
# still reference. So pruning is explicit: the reconciler tags an object
# lifecycle=prunable, and rule 1 below expires it. AWS performs the delete
# internally, which is why no cluster credential needs any delete permission.
#
# INVARIANT: rule 1 and rule 6 are the only rules that expire CURRENT versions,
# and both are filtered -- rule 1 on the prune tag, rule 6 on inventory/. An
# unfiltered current-version expiration rule here would silently destroy live
# backup data. ADR-002's definition of done requires reading this configuration
# back from AWS to confirm that; see scripts/verify-backup-vault.sh.
resource "aws_s3_bucket_lifecycle_configuration" "vault" {
  bucket = aws_s3_bucket.vault.id

  # 1. THE PRUNING RULE. Fires only on objects the reconciler has tagged.
  # Because the bucket is versioned, this writes a delete marker rather than
  # destroying the locked version -- so a mistaken or malicious tagging run is
  # reversible by an admin for as long as Object Lock holds.
  rule {
    id     = "tag-gated-prune"
    status = "Enabled"

    filter {
      tag {
        key   = "lifecycle"
        value = "prunable"
      }
    }

    expiration {
      days = 1
    }
  }

  # 2. Reclaims the space. Runs after the lock window has passed, so it never
  # collides with an active retention.
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.vault_noncurrent_expiration_days
    }
  }

  # 3. Removes delete markers whose last noncurrent version is gone, so listings
  # do not accumulate tombstones indefinitely.
  rule {
    id     = "clean-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  # 4. A relay interrupted mid-upload leaves parts that are billed but invisible
  # to a normal listing.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # 5. Cost control on the two data prefixes only. Deliberately NOT Glacier
  # Instant Retrieval: its 90-day minimum outlives several of these retention
  # tiers, so objects would be billed for storage they no longer occupy, and it
  # has to be checked against the quarterly drill's restore-time budget first.
  rule {
    id     = "transition-longhorn-ia"
    status = "Enabled"

    filter {
      prefix = "longhorn/"
    }

    transition {
      days          = var.vault_ia_transition_days
      storage_class = "STANDARD_IA"
    }
  }

  rule {
    id     = "transition-seaweedfs-ia"
    status = "Enabled"

    filter {
      prefix = "seaweedfs/"
    }

    transition {
      days          = var.vault_ia_transition_days
      storage_class = "STANDARD_IA"
    }
  }

  # 6. Inventory reports are disposable -- a fresh one lands every day.
  rule {
    id     = "expire-inventory-reports"
    status = "Enabled"

    filter {
      prefix = "inventory/"
    }

    expiration {
      days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.vault]
}

# S3 Inventory feeds the reconciler's diff (Task 11). Two configurations rather
# than one because an Inventory filter accepts a single prefix, and ADR-002 asks
# for longhorn/ and seaweedfs/ while excluding inventory/ so the report does not
# describe itself and grow without bound.
#
# Inventory does NOT report object tags. Tagging activity is therefore invisible
# here and must be monitored from the reconciler's own metrics -- see Task 15.
locals {
  vault_inventory_prefixes = toset(["longhorn", "seaweedfs"])

  vault_inventory_fields = [
    "Size",
    "LastModifiedDate",
    "StorageClass",
    "IsMultipartUploaded",
    "ObjectLockRetainUntilDate",
    "ObjectLockMode",
    "ObjectLockLegalHoldStatus",
    "ETag",
  ]

  # ADR-002 Task 6 also lists IsLatest. AWS rejects it: IsLatest is an implicit
  # column of a versioned inventory, not a requestable optional field, and
  # `terraform validate` fails on it. It would carry no information here anyway,
  # because included_object_versions is Current, so every listed row is by
  # definition the latest. Current is the right choice for the reconciler, which
  # diffs live objects against the local backupstore and must not tag a
  # superseded version.
}

resource "aws_s3_bucket_inventory" "vault" {
  for_each = local.vault_inventory_prefixes

  bucket                   = aws_s3_bucket.vault.id
  name                     = "${each.value}-daily"
  included_object_versions = "Current"
  optional_fields          = local.vault_inventory_fields

  schedule {
    frequency = "Daily"
  }

  filter {
    prefix = "${each.value}/"
  }

  destination {
    bucket {
      format     = "Parquet"
      bucket_arn = aws_s3_bucket.vault.arn
      prefix     = "inventory"
    }
  }
}

# S3 Inventory writes as the service principal, which needs explicit permission
# even when the source and destination bucket are the same.
data "aws_iam_policy_document" "vault_inventory_delivery" {
  statement {
    sid    = "AllowInventoryDelivery"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.vault.arn}/inventory/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.vault.arn]
    }
  }
}
