
# Fux-CD for GitOps
GitOps Infrastructure-as-Code (IaC) for the deployment of FluxCD to a Kubernetes (k8s) cluster.

## Description
This repository contains files to deploy FluxCD to a Kubernetes cluster via Terraform and `kubectl`. Thereby Terraform installs FluxCD on the Kubernetes cluster and links it to a Git repository, so that you are getting a fully functioning GitOps architecture.

## Architecture

```mermaid
graph LR
A[Kubernetes Cluster] --> B((Terraform))
B --> C[FluxCD installed]
C --> D((Terraform))
D --> E[Flux linked to Git reporisotry]
```

## Prerequirements

### System Requirements
* Internet Access
* Access to the Kubernetes cluster

### Software Requirements
| Software | Latest tested Version |
|--|--|
| Terraform | 1.3.1 |
| kubectl| v1.25.2 |
| Kubernetes| v1.22.3+k3s1 |

## How To
1. Install [Terraform](https://www.terraform.io)
> Tip: On Windows you can use the command-line installer [Scoop]( https://scoop.sh ) to install all the required components.
> Tip: On Linux-based systems you can use the package manager [apt] ( https://learn.hashicorp.com/tutorials/terraform/install-cli ) 

2. Make sure your Kubernetes cluster is available via `kubectl` by executing `kubectl get nodes`. The output should look similar to this:
```
$ kubectl get nodes
NAME         STATUS   ROLES                  AGE   VERSION
onsite-up2   Ready    control-plane,master   16h   v1.22.3+k3s1
```

3. Set Terrform variables in file ```variables.tf```
  * github_host_url
  * repository_name
  * repository_visibility
  * branch
  * target_path
  * registry
  * image_pull_secrets
  
4. (Optional) if you are behind a corporate proxy you might have to set the following variables
  * HTTP_PROXY	: Proxy used for http requests (unencrypted)
  * HTTPS_PROXY	: Proxy used for https requests (encrypted)
  * NO_PROXY	: connections with specific IPs / websites which should not use a proxy 
  
  ```
     	set HTTPS_PROXY=my-internal-proxy.com:8080
	set NO_PROXY=53.136.226.63,my-internal-server.com
  ```

5. Create GitHub repository you defined in Terrform varialbe `repository_name`

5. Prepare Kubernetes cluster
	a) Create the Namespace you want to deploy FluxCD

	  ```
		 kubectl create namespace flux-system
	  ```	
	b) Create a [Kubernetes secret](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/#create-a-secret-by-providing-credentials-on-the-command-line) (as defined in `variables.tf`) for the container registry the FluxCD images are pulled from

	  ```
		 kubectl create secret docker-registry ghcr.io-pull-secret --docker-server=ghcr.io --docker-username=myGithubUser --docker-password=myGithubTokenToPull --docker-email=me@myself.com -n flux-system
	  
	  ```
  > WARNING: Do NOT use '' or escape special characters for --docker-server, --docker-username, or --docker-password values.
  > INFO: Verify if secret was created correctly via `kubectl get secret registry.app.corpintra.net-harbor-pull-secret --output=yaml -n flux-system`

6. Execute the `install.bat` on Windows or `install.sh` on Linux-based systems, which is available within this directory.

Set the required Git Parameters in your shell:
```
TF_VAR_github_owner=YourGitHubUser (or GitHub Organization e.g. MY_ORG)
TF_VAR_github_token=YourGitHubToken (which is assoziated to a technical user)
```
> The Github token must contain create, write, read, and delete rights
> Specail caracters can be escaped with `^`




7. To verify that the installation was successful execute `kubectl get pods -n flux-system`. If you should get an output similar like this:
```
$ kubectl get pods -n flux-system
NAME                                       READY   STATUS    RESTARTS   AGE
helm-controller-6765c95b47-b99sz           1/1     Running   0          2m
kustomize-controller-7f5455cd78-2w2ts      1/1     Running   0          2m
notification-controller-694856fd64-8gdtk   1/1     Running   0          2m
source-controller-5bdb7bdfc9-ctlpp         1/1     Running   0          2m
```

 

### Latest tested Components

| Component | Version  |
|--|--|
| Docker |  19.03.13|
| Kubernetes | 1.19.13 |
| kubectl | 1.21.2 |
| Terraform| 1.0.1 |
| Terraform Provider Flux| 0.0.13 |
| Windows | Windows 10 Build 19043.1110 |



## Known issues
| Topic | Description  | Workaround | Related Articles  |
|--|--|--|--|
| FluxCD ManifestFile path error on Windows | Currently the automated linking between FluxCD and the defined Git repository is not working on Windows| Was fixed |  [fluxcd#645](https://github.com/fluxcd/flux2/issues/645)
| Unable to delete flux-system namespace | When deleting resources manually or via Terraform the flux-system namespace is left in Termination state \| kubectl get namespace flux-system -o json  \| sed 's/\"kubernetes\"//'  \| kubectl replace --raw /api/v1/namespaces/flux-system/finalize -f - | [fluxcd-provider#67](https://github.com/fluxcd/terraform-provider-flux/issues/67)

## Further Readings
* [FluxCD]( https://fluxcd.io )
* [FluxCD Terraform Provider]( https://github.com/fluxcd/terraform-provider-flux )
* [kubectl](https://kubernetes.io/docs/reference/kubectl/overview/)
* [Terraform Registry]( https://registry.terraform.io/providers/fluxcd/flux/latest/docs  )


## Key Words
* Infrastructure-as-Code (IaC)
* GitOps
* FluxCD
* Kubernetes
* Terraform
* Docker

## ToDo
* Open a pull request at the [FluxCD Terraform Provider]( https://github.com/fluxcd/terraform-provider-flux ) to support the linking of already existing Git repositories to FluxCD

## Author
Andreas Biberacher < bibatdevelopment@gmail.com >

Feel free to open discussions and pull requests :nerd_face:
