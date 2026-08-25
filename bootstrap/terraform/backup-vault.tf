# ADR-005 immutable backup vault (Tasks 5, 6, 8).
#
# Distinct from the buckets in s3-buckets.tf in three ways that matter:
#
#   1. SSE-S3, not SSE-KMS. A customer-managed key whose deletion can be
#      scheduled is a backdoor around Object Lock -- schedule the key for
#      deletion and the locked objects become permanently unreadable while every
#      retention setting still reports healthy. ADR-005 rejects SSE-KMS for this
#      reason. The older buckets predate that decision and are left alone.
#   2. Object Lock in Governance mode. Compliance mode is rejected because no
#      override exists, including for account root -- a fat-fingered retention
#      value would be unfixable for its full duration.
#   3. Nothing in the cluster may delete from it. Deletion is performed by S3
#      Lifecycle (which runs as the service, not as any principal) or by an
#      interactive MFA admin.
#
# Prefix layout: longhorn/ (relayed backupstore), seaweedfs/ (relayed staging),
# inventory/ (S3 Inventory reports).
resource "aws_s3_bucket" "vault" {
  bucket = "${var.cluster_name}-backup-vault"

  # Must be set at creation. AWS can enable Object Lock on an existing versioned
  # bucket since Nov 2023, but a fresh bucket keeps the prefix layout clean and
  # avoids inheriting the KMS default of the older buckets.
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "vault" {
  bucket = aws_s3_bucket.vault.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Governance retention. The relay holds no bypass permission, so a compromised
# relay cannot shorten or remove this. 21 days is chosen to sit comfortably
# inside the 45-day noncurrent expiration below, so Lifecycle never has to fight
# an active lock.
resource "aws_s3_bucket_object_lock_configuration" "vault" {
  bucket = aws_s3_bucket.vault.id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.vault_object_lock_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.vault]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "vault" {
  bucket = aws_s3_bucket.vault.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "vault" {
  bucket = aws_s3_bucket.vault.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
