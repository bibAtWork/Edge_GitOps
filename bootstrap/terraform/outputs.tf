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
