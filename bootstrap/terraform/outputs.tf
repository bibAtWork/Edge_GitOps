output "etcd_backup_bucket" {
  description = "S3 bucket name for etcd offsite backups"
  value       = aws_s3_bucket.backup["etcd"].id
}

output "velero_backup_bucket" {
  description = "S3 bucket name for Velero offsite backups"
  value       = aws_s3_bucket.backup["velero"].id
}

output "kms_key_arn" {
  description = "KMS key ARN used for S3 server-side encryption"
  value       = aws_kms_key.backup.arn
}

output "velero_access_key_id" {
  description = "AWS access key ID for Velero (store in SOPS secret)"
  value       = aws_iam_access_key.velero.id
  sensitive   = true
}

output "velero_secret_access_key" {
  description = "AWS secret access key for Velero (store in SOPS secret)"
  value       = aws_iam_access_key.velero.secret
  sensitive   = true
}

output "talos_backup_access_key_id" {
  description = "AWS access key ID for talos-backup (store in SOPS secret)"
  value       = aws_iam_access_key.talos_backup.id
  sensitive   = true
}

output "talos_backup_secret_access_key" {
  description = "AWS secret access key for talos-backup (store in SOPS secret)"
  value       = aws_iam_access_key.talos_backup.secret
  sensitive   = true
}

# ADR-005 backup vault. Access keys are sensitive; read them with
#   terraform output -raw backup_relay_secret_access_key
# The admin identity deliberately has no key here -- see backup-vault-iam.tf.
output "backup_vault_bucket" {
  description = "Name of the ADR-005 immutable backup vault"
  value       = aws_s3_bucket.vault.id
}

output "backup_relay_access_key_id" {
  description = "Access key ID for the write-only relay identity"
  value       = aws_iam_access_key.backup_relay.id
}

output "backup_relay_secret_access_key" {
  description = "Secret access key for the write-only relay identity"
  value       = aws_iam_access_key.backup_relay.secret
  sensitive   = true
}

output "backup_auditor_access_key_id" {
  description = "Access key ID for the read-and-tag auditor identity"
  value       = aws_iam_access_key.backup_auditor.id
}

output "backup_auditor_secret_access_key" {
  description = "Secret access key for the read-and-tag auditor identity"
  value       = aws_iam_access_key.backup_auditor.secret
  sensitive   = true
}

output "backup_admin_user_arn" {
  description = "ARN of the interactive MFA admin identity (no access key is created by Terraform)"
  value       = aws_iam_user.backup_admin.arn
}
