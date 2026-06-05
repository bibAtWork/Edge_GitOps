locals {
  buckets = {
    etcd    = "${var.cluster_name}-etcd-backups-offsite"
    velero  = "${var.cluster_name}-velero-backups-offsite"
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

resource "aws_s3_bucket_intelligent_tiering_configuration" "velero" {
  bucket = aws_s3_bucket.backup["velero"].id
  name   = "deep-archive"

  tiering {
    access_tier = "DEEP_ARCHIVE_ACCESS"
    days        = 180
  }
}
