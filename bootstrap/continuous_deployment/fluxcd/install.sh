#!/bin/bash
#FluxCD automated setup on Kubernetes via kubectl
set -eu

## Preperation steps
SCRIPT_DIR=$( dirname -- "$( readlink -f -- "$0"; )"; )


if ! command -v terraform &> /dev/null
then
    echo "'terraform' is not installed or not linked correctly."
    exit
fi


## Request GitHub user and password for Repository READ and WRITE
read -p 'Enter GitHub User or Organization [string]: ' TF_VAR_github_owner
read -sp 'Enter Token [string]: ' TF_VAR_github_token
read -p "Continue with '$TF_VAR_github_owner':'$TF_VAR_github_token' ? (Y/N): " confirm && [[ $confirm == [yY] || $confirm == [yY][eE][sS] ]] || exit 1


echo
echo "===================================="
echo

echo "INFO: Starting installation of Flux CD on the Kubernetes cluster ..."
cd "${SCRIPT_DIR}/flux_sync" \
	&& terraform init \
	&& terraform apply -auto-approve -var "github_owner=$TF_VAR_github_owner" -var "github_token=$TF_VAR_github_token" \
	&& echo "INFO: Setup of Flux CD in Kubernetes cluster was successful."
