output "backup_vault_bucket" {
  description = "Name of the ADR-005 backup vault (decommissioning)"
  value       = aws_s3_bucket.vault.id
}

output "backup_admin_user_arn" {
  description = "ARN of the interactive MFA admin identity (no access key is created by Terraform)"
  value       = aws_iam_user.backup_admin.arn
}
