# The first offsite buckets: etcd snapshots and Velero backups, both encrypted
# with the KMS key in kms.tf.
#
# DECOMMISSIONING since the ADR-012 cutover (2026-09-13). Velero was removed,
# and nothing has uploaded etcd snapshots since talos-backup was removed on
# 2026-08-25. Their identities are gone; lifecycle.tf empties both buckets,
# which, with the KMS key, are then deleted along with the vault's files
# (docs/backlog.md).
locals {
  buckets = {
    etcd   = "${var.cluster_name}-etcd-backups-offsite"
    velero = "${var.cluster_name}-velero-backups-offsite"
  }
}

resource "aws_s3_bucket" "backup" {
  for_each = local.buckets
  bucket   = each.value
}

resource "aws_s3_bucket_versioning" "backup" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backup[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backup[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.backup.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backup[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
