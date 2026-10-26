# Access the homelab Kubernetes cluster

This guide is for a person who has been given access to the homelab. You need
an invitation to its Tailscale network, a Keycloak account, and the public
Kubernetes API CA file (`homelab-ca.crt`) from the cluster administrator.
Keep your login details private.

The examples below use the `mh0794` account and namespace. Replace `mh0794`
with your own account and namespace if they differ.

## Connect to the network

1. Accept the Tailscale invitation, install Tailscale, and sign in with the
   identity that was invited. Connect to the tailnet. On Linux, also run
   `sudo tailscale set --accept-routes`; Windows and macOS accept subnet
   routes automatically.
2. Open `https://keycloak.homelab.data-harness.org` in a browser to check
   that you can reach the login service. Change your temporary password when
   Keycloak asks you to do so.

The Kubernetes API is `https://192.168.178.100:6443`. Tailscale carries
traffic to that address through `homelab-router`; the router's Tailscale name
is not the API endpoint and should not replace this address in your
kubeconfig. Keycloak is reached through `192.168.178.200:443`.

## Install the command-line tools

Install [kubectl](https://kubernetes.io/docs/tasks/tools/) and the
[int128/kubelogin](https://github.com/int128/kubelogin#setup) OIDC plugin.
If you use Krew, install the plugin with:

```sh
kubectl krew install oidc-login
```

Check that `kubectl oidc-login --help` runs before continuing.

## Create your kubeconfig

If you were given a kubeconfig for your own account, use that file and skip
the creation commands below. Otherwise, save the **public** `homelab-ca.crt`
file you received in a working directory and create a separate kubeconfig.
Do not use another person's kubeconfig.

In PowerShell:

```powershell
New-Item -ItemType Directory -Force (Join-Path $HOME '.kube') | Out-Null
$env:KUBECONFIG = Join-Path $HOME '.kube/mh0794.yaml'
```

In a POSIX shell:

```sh
mkdir -p "$HOME/.kube"
export KUBECONFIG="$HOME/.kube/mh0794.yaml"
```

From the directory containing `homelab-ca.crt`, run:

```sh
kubectl config set-cluster homelab --server=https://192.168.178.100:6443 --certificate-authority=./homelab-ca.crt --embed-certs=true
kubectl config set-credentials mh0794 --exec-interactive-mode=Never --exec-api-version=client.authentication.k8s.io/v1 --exec-command=kubectl --exec-arg=oidc-login --exec-arg=get-token --exec-arg=--grant-type=authcode --exec-arg=--oidc-issuer-url=https://keycloak.homelab.data-harness.org/realms/homelab --exec-arg=--oidc-client-id=kubernetes --exec-arg=--oidc-pkce-method=S256
kubectl config set-context mh0794@homelab --cluster=homelab --user=mh0794 --namespace=mh0794
kubectl config use-context mh0794@homelab
```

Set `KUBECONFIG` again in each new terminal, or add the corresponding line
to your shell profile. The kubeconfig contains the public CA and OIDC plugin
settings; it does not contain your password.

## Sign in and check access

Run `kubectl get pods -n mh0794`. The OIDC plugin opens a browser for
Keycloak login. Your account can manage workloads in `mh0794` and use the
built-in Kubernetes `view` role in other namespaces. That role does not
allow reading Kubernetes Secrets. Check the permissions with:

```sh
kubectl auth can-i create deployments -n mh0794
kubectl auth can-i list pods -n monitoring
kubectl auth can-i get secrets -n monitoring
kubectl auth can-i create deployments -n monitoring
```

The expected answers are `yes`, `yes`, `no`, and `no`, respectively. The
OIDC plugin caches tokens locally; keep its cache private. See the plugin's
[token-cache documentation](https://github.com/int128/kubelogin/blob/master/docs/usage.md#token-cache)
for storage options.

If the API connection times out, confirm Tailscale is connected and subnet
routes are enabled, then ask the administrator to check the API route and
access rule for TCP `6443`. If Keycloak does not open, ask them to check its
route and TCP `443` rule. An `x509` trust error points to a wrong or stale
`homelab-ca.crt`; `Unauthorized` points to login or token configuration;
`Forbidden` means your account lacks the required Keycloak group or RBAC
permission. If `kubectl oidc-login` is not found, install the plugin or add
it to your `PATH`.
