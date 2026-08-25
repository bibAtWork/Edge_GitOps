variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "eu-central-1"
}

variable "cluster_name" {
  description = "Cluster name used as prefix for all resource names"
  type        = string
  default     = "homelab"
}

variable "etcd_backup_retention_days" {
  description = "Days to retain etcd backups in S3"
  type        = number
  default     = 7
}

variable "velero_daily_retention_days" {
  description = "Days to retain daily Velero backups"
  type        = number
  default     = 90
}

variable "velero_monthly_retention_days" {
  description = "Days to retain monthly Velero backups"
  type        = number
  default     = 365
}

variable "tags" {
  description = "Common tags applied to all AWS resources"
  type        = map(string)
  default = {
    Project     = "homelab"
    ManagedBy   = "terraform"
    Environment = "homelab"
  }
}

variable "vault_object_lock_days" {
  description = "ADR-005 backup vault default Object Lock retention, in days (Governance mode)"
  type        = number
  default     = 21
}

variable "vault_noncurrent_expiration_days" {
  description = "Days before noncurrent versions in the backup vault expire. Must exceed vault_object_lock_days so Lifecycle never conflicts with an active lock."
  type        = number
  default     = 45
}

variable "vault_ia_transition_days" {
  description = "Days before current vault objects move to Standard-IA. Not lower than 30: Standard-IA bills a 30-day minimum plus a per-object transition charge, so an earlier move costs money and buys nothing."
  type        = number
  default     = 30
}
