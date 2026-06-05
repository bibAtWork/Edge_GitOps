resource "aws_s3_bucket_lifecycle_configuration" "etcd" {
  bucket = aws_s3_bucket.backup["etcd"].id

  rule {
    id     = "expire-old-snapshots"
    status = "Enabled"

    expiration {
      days = var.etcd_backup_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "velero" {
  bucket = aws_s3_bucket.backup["velero"].id

  rule {
    id     = "expire-daily-backups"
    status = "Enabled"

    filter {
      prefix = "daily/"
    }

    expiration {
      days = var.velero_daily_retention_days
    }
  }

  rule {
    id     = "expire-monthly-backups"
    status = "Enabled"

    filter {
      prefix = "monthly/"
    }

    expiration {
      days = var.velero_monthly_retention_days
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}
