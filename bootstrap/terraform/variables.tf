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
