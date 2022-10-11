## Source: https://github.com/fluxcd/terraform-provider-flux/blob/main/examples/github/main.tf

terraform {
  required_version = ">= 0.13"

  required_providers {
    github = {
      source = "integrations/github"
      version = ">= 4.5.2"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.0.2"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">= 1.10.0"
    }
    flux = {
      source  = "fluxcd/flux"
      version = ">= 0.0.13"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "3.1.0"
    }

  }
}

provider "flux" {}

provider "kubectl" {}

provider "kubernetes" {
  config_path = "~/.kube/config"
  config_context = "default"
  insecure = false
}

## Ref.: https://registry.terraform.io/providers/integrations/github/latest/docs
provider "github" {
  owner = var.github_owner
  token = var.github_token
  ## Only for GitHub Enterprise
  #organization = var.github_org #DEPRECATED
  #base_url = "https://${var.github_host_url}/"
}

# SSH
## Ref.: https://github.blog/changelog/2022-01-18-githubs-ssh-host-keys-are-now-published-in-the-api/
## ssh-keyscan github.com # WARNING: Will timeout if proxy is required
locals {
  known_hosts = "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg="
}

resource "tls_private_key" "main" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

# Flux
data "flux_install" "main" {
  target_path = var.target_path
  components_extra = var.components_extra
  registry = var.registry
  image_pull_secrets = var.image_pull_secrets
}

data "flux_sync" "main" {
  target_path = var.target_path
  url         = "ssh://git@${var.github_host_url}/${var.github_owner}/${var.repository_name}.git"
  branch      = var.branch
  git_implementation = "libgit2" #Required due to issue with Github Enterprise (https://github.com/fluxcd/source-controller/issues/652#issuecomment-1085680590)
}

# Kubernetes
## Decomment if resource does not already exist or should be managed by Terraform
## Ref.: https://github.com/hashicorp/terraform/issues/23178
#resource "kubernetes_namespace" "flux_system" {
#  metadata {
#    name = "flux-system"
#  }
#
#  lifecycle {
#    ignore_changes = [
#      metadata[0].labels,
#    ]
#  }
#}

data "kubernetes_namespace" "flux_system" {
  metadata {
    name = "flux-system"
  }
}

data "kubectl_file_documents" "install" {
  content = data.flux_install.main.content
}

data "kubectl_file_documents" "sync" {
  content = data.flux_sync.main.content
}

locals {
  patches = {
    psp  = file("./patch-podSecurityPolicy.yml")
  }

  install = [for v in data.kubectl_file_documents.install.documents : {
    data : yamldecode(v)
    content : v
    }
  ]
  sync = [for v in data.kubectl_file_documents.sync.documents : {
    data : yamldecode(v)
    content : v
    }
  ]
}

resource "kubectl_manifest" "install" {
  for_each   = { for v in local.install : lower(join("/", compact([v.data.apiVersion, v.data.kind, lookup(v.data.metadata, "namespace", ""), v.data.metadata.name]))) => v.content }
  depends_on = [data.kubernetes_namespace.flux_system]
  yaml_body  = each.value
}

resource "kubectl_manifest" "sync" {
  for_each   = { for v in local.sync : lower(join("/", compact([v.data.apiVersion, v.data.kind, lookup(v.data.metadata, "namespace", ""), v.data.metadata.name]))) => v.content }
  depends_on = [data.kubernetes_namespace.flux_system]
  yaml_body  = each.value
}

resource "kubernetes_secret" "main" {
  depends_on = [kubectl_manifest.install]

  metadata {
    name      = data.flux_sync.main.secret
    namespace = data.flux_sync.main.namespace
  }

  data = {
    identity       = tls_private_key.main.private_key_pem
    "identity.pub" = tls_private_key.main.public_key_pem
    known_hosts    = local.known_hosts
  }

}

# GitHub
##Ref.: https://registry.terraform.io/providers/integrations/github/latest/docs/data-sources/repository
data "github_repository" "main" {
  full_name = "${var.github_owner}/${var.repository_name}"
  #name       = var.repository_name
}

resource "github_branch_default" "main" {
  repository = data.github_repository.main.name
  branch     = var.branch
}

##Ref.: https://github.com/fluxcd/terraform-provider-flux/issues/181
resource "github_repository_deploy_key" "main" {
  title      = "staging-cluster"
  repository = data.github_repository.main.name
  key        = tls_private_key.main.public_key_openssh
  read_only  = false # WRITE access required to be able to automatically update images
}

resource "github_repository_file" "install" {
  repository = data.github_repository.main.name
  file       = data.flux_install.main.path
  content    = data.flux_install.main.content
  branch     = var.branch
  overwrite_on_create = true
}

resource "github_repository_file" "sync" {
  repository = data.github_repository.main.name
  file       = data.flux_sync.main.path
  content    = data.flux_sync.main.content
  branch     = var.branch
  overwrite_on_create = true
}

resource "github_repository_file" "kustomize" {
  repository = data.github_repository.main.name
  file       = data.flux_sync.main.kustomize_path
  content    = data.flux_sync.main.kustomize_content
  branch     = var.branch
  overwrite_on_create = true
}