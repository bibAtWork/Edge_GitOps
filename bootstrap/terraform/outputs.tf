# The old backup buckets are being emptied (ADR-012 cutover, 2026-09-13). Their
# outputs, and the KMS key's, go with their resources once they are empty
# (docs/backlog.md). The recovery vault's outputs are in recovery-vault.tf.
output "etcd_backup_bucket" {
  description = "S3 bucket name for etcd offsite backups (decommissioning)"
  value       = aws_s3_bucket.backup["etcd"].id
}

output "velero_backup_bucket" {
  description = "S3 bucket name for Velero offsite backups (decommissioning)"
  value       = aws_s3_bucket.backup["velero"].id
}

output "kms_key_arn" {
  description = "KMS key ARN used for the old buckets' server-side encryption (decommissioning)"
  value       = aws_kms_key.backup.arn
}

output "backup_vault_bucket" {
  description = "Name of the ADR-005 backup vault (decommissioning)"
  value       = aws_s3_bucket.vault.id
}

output "backup_admin_user_arn" {
  description = "ARN of the interactive MFA admin identity (no access key is created by Terraform)"
  value       = aws_iam_user.backup_admin.arn
}
