# Lifecycle for the etcd and Velero buckets: DECOMMISSIONING (s3-buckets.tf).
#
# Every version expires after a day, then the delete markers left behind are
# removed. Neither bucket has Object Lock, so this empties them within a few
# days.
resource "aws_s3_bucket_lifecycle_configuration" "etcd" {
  bucket = aws_s3_bucket.backup["etcd"].id

  rule {
    id     = "decommission-expire-current"
    status = "Enabled"

    filter {}

    expiration {
      days = 1
    }
  }

  rule {
    id     = "decommission-expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 1
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
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "velero" {
  bucket = aws_s3_bucket.backup["velero"].id

  rule {
    id     = "decommission-expire-current"
    status = "Enabled"

    filter {}

    expiration {
      days = 1
    }
  }

  rule {
    id     = "decommission-expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 1
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
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
