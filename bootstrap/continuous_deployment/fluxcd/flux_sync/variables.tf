/**
 * Ref.: https://github.com/fluxcd/terraform-provider-flux/blob/main/examples/github/variables.tf
 * Due to security reasons the following variables should be set within the system and not directly in the Terraform files
 *   - TF_VAR_github_owner
 *   - TF_VAR_github_token
 *
**/

variable "github_host_url" {
  type        = string
  default     = "github.com"
  description = "github host url"
}

##Required for GitHub Enterprise
#variable "github_org" {
#  type        = string
#  default     = "MY_CORP"
#  description = "github organization url"
#}

variable "github_owner" {
  type        = string
  description = "github owner"
}

variable "github_token" {
  type        = string
  description = "github token"
}

variable "repository_name" {
  type        = string
  default     = "Edge_GitOps"
  description = "github repository name"
}

variable "repository_visibility" {
  type        = string
  default     = "public"
  description = "How visiable is the Git repo [private, internal, public]"
}

variable "branch" {
  type        = string
  default     = "main"
  description = "Git branch name"
}

variable "target_path" {
  type    = string
  default = "non-prod/namespaces"
  description = "flux sync target path"
}

/****/

variable "components_extra" {
  type        = list(string)
  default     =  ["image-reflector-controller","image-automation-controller"]
  description = "extra flux components"
}

##Required for custom Image Registry
variable "registry" {
  type    = string
  default = "ghcr.io/fluxcd"
  description = "registry.my-domain.net/org/repository"
}

variable "image_pull_secrets" {
  type    = string
  default = "ghcr.io-pull-secret"
  description = "secret used to pull image from container registry"
}

