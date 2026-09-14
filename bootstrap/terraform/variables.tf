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
  description = "ADR-005 backup vault default Object Lock retention, in days (Governance mode). The vault is decommissioning; kept until it is deleted."
  type        = number
  default     = 21
}
