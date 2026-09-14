# Lifecycle for the ADR-005 backup vault: DECOMMISSIONING.
#
# The vault was retired at the ADR-012 cutover (2026-09-13). The recovery
# system keeps its own AWS repository (recovery-vault.tf), and nothing writes
# here any more. These rules empty the bucket, so that it and the rest of the
# backup-vault*.tf files can then be deleted:
#
#   1. every current version expires after a day (S3 writes a delete marker);
#   2. every noncurrent version expires a day after it became noncurrent --
#      but Object Lock holds each version until its 21-day retention ends, so
#      S3 removes it only after that;
#   3. delete markers with no versions left behind them are removed;
#   4. incomplete multipart uploads are aborted.
#
# The relay wrote its last objects on 2026-09-13, so the final lock ends on
# 2026-10-04 and the bucket should be empty a few days after. Confirm that with
# `aws s3api list-object-versions --bucket <vault> --max-items 1` before
# removing the vault's resources (docs/backlog.md).
#
# S3 Lifecycle runs as the service, not as a principal, so the bucket policy's
# delete deny does not stop it, and no cluster credential is involved. The
# Inventory reports that fed the old reconciler are no longer produced.
resource "aws_s3_bucket_lifecycle_configuration" "vault" {
  bucket = aws_s3_bucket.vault.id

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
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.vault]
}
