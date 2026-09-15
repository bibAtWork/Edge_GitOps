#!/usr/bin/env bash
# Bootstrap script for the 3-node HA profile.
# Prerequisites: talosctl, kubectl, flux, sops, age, terraform installed locally.
#
# Usage:
#   NODE1_IP=192.168.1.10 NODE2_IP=192.168.1.11 NODE3_IP=192.168.1.12 \
#   VIP=192.168.1.100 \
#   GITHUB_OWNER=<your-github-user> GITHUB_REPO=homelab-cluster \
#   ./bootstrap/scripts/bootstrap-3node.sh
#
#   Env vars above are optional overrides -- config.json (see below) is the
#   normal way to supply these. Export TF_VAR_budget_alert_email (the address
#   for AWS budget alerts) before running; Phase 5 requires it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/bootstrap/config.json"

# Read a dotted-path key from config.json (e.g. _cfg 'node.vip')
_cfg() {
  python3 - "${CONFIG_FILE}" "$1" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
v = cfg
for k in sys.argv[2].split("."):
    v = v.get(k, "") if isinstance(v, dict) else ""
print(v if isinstance(v, str) else "")
PYEOF
}

# ── Config wizard (runs only when config.json is absent) ─────────────────────
_config_wizard() {
  local _wn _we _wd _wsub _wgw _wni1 _wni2 _wni3 _wvip _wsnet
  local _wgho _wghr _wghb _wght
  local _war _wak _was _wcf _wtsi _wtss _wgpw

  echo ""
  echo "── Cluster ───────────────────────────────────────────────────────────────"
  read -rp "  Cluster name [homelab]: " _wn;          _wn="${_wn:-homelab}"
  read -rp "  Let's Encrypt email: " _we
  read -rp "  Domain (e.g. example.com): " _wd
  read -rp "  Subdomain prefix (optional, e.g. lab): " _wsub
  read -rp "  Gateway LAN IP (optional, e.g. 192.168.1.200): " _wgw

  echo ""
  echo "── Nodes ─────────────────────────────────────────────────────────────────"
  read -rp "  Node 1 IP: " _wni1
  read -rp "  Node 2 IP: " _wni2
  read -rp "  Node 3 IP: " _wni3
  read -rp "  Virtual IP (VIP) for the control-plane endpoint: " _wvip
  read -rp "  Node LAN subnet [192.168.1.0/24]: " _wsnet; _wsnet="${_wsnet:-192.168.1.0/24}"

  echo ""
  echo "── GitHub ────────────────────────────────────────────────────────────────"
  read -rp "  GitHub owner (user or org): " _wgho
  read -rp "  GitHub repo name [Edge_GitOps]: " _wghr;    _wghr="${_wghr:-Edge_GitOps}"
  read -rp "  GitHub branch [main]: " _wghb;              _wghb="${_wghb:-main}"
  read -rsp "  GitHub personal access token: " _wght; echo ""

  echo ""
  echo "── AWS ───────────────────────────────────────────────────────────────────"
  read -rp "  AWS region [eu-central-1]: " _war;          _war="${_war:-eu-central-1}"
  read -rsp "  AWS access key ID: " _wak; echo ""
  read -rsp "  AWS secret access key: " _was; echo ""

  echo ""
  echo "── Cloudflare ────────────────────────────────────────────────────────────"
  read -rsp "  Cloudflare API token: " _wcf; echo ""

  echo ""
  echo "── Tailscale ─────────────────────────────────────────────────────────────"
  read -rp "  Tailscale OAuth client ID: " _wtsi
  read -rsp "  Tailscale OAuth client secret: " _wtss; echo ""

  echo ""
  echo "── Grafana ───────────────────────────────────────────────────────────────"
  read -rsp "  Grafana admin password: " _wgpw; echo ""

  echo ""
  echo "  SeaweedFS, Zot, and Grafana's Keycloak OAuth client secret will be auto-generated."
  echo ""

  # Write config.json via Python — values passed through env vars to avoid shell injection
  _WN="$_wn" _WE="$_we" _WD="$_wd" _WSUB="$_wsub" _WGW="$_wgw" \
  _WNI1="$_wni1" _WNI2="$_wni2" _WNI3="$_wni3" _WVIP="$_wvip" _WSNET="$_wsnet" \
  _WGHO="$_wgho" _WGHR="$_wghr" _WGHB="$_wghb" _WGHT="$_wght" \
  _WAR="$_war" _WAK="$_wak" _WAS="$_was" \
  _WCF="$_wcf" _WTSI="$_wtsi" _WTSS="$_wtss" \
  _WGPW="$_wgpw" \
  python3 - "${CONFIG_FILE}" <<'PYEOF'
import json, os, sys

e = os.environ.get
config_file = sys.argv[1]
cfg = {
  "cluster": {
    "name":              e("_WN", "homelab"),
    "letsencrypt_email": e("_WE", ""),
    "domain":            e("_WD", ""),
    "subdomain":         e("_WSUB", ""),
    "gateway_ip":        e("_WGW", ""),
  },
  "node": {
    "ip1":    e("_WNI1", ""),
    "ip2":    e("_WNI2", ""),
    "ip3":    e("_WNI3", ""),
    "vip":    e("_WVIP", ""),
    "subnet": e("_WSNET", "192.168.1.0/24"),
  },
  "github": {
    "owner":  e("_WGHO", ""),
    "repo":   e("_WGHR", ""),
    "branch": e("_WGHB", "main"),
    "token":  e("_WGHT", ""),
  },
  "aws": {
    "region":            e("_WAR", "eu-central-1"),
    "access_key_id":     e("_WAK", ""),
    "secret_access_key": e("_WAS", ""),
  },
  "cloudflare": {
    "api_token": e("_WCF", ""),
  },
  "tailscale": {
    "oauth_client_id":     e("_WTSI", ""),
    "oauth_client_secret": e("_WTSS", ""),
  },
  "seaweedfs": {
    "admin_access_key_id":     "",
    "admin_secret_access_key": "",
  },
  "grafana": {
    "admin_password": e("_WGPW", ""),
  },
  "zot": {
    "admin_password": "",
  },
  "keycloak": {
    "grafana_client_secret": "",
  },
}
with open(config_file, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.chmod(config_file, 0o600)
print(f"  Written to {config_file}")
PYEOF
}

echo ""
if [[ -f "${CONFIG_FILE}" ]]; then
  echo "  1) Use existing bootstrap/config.json"
  echo "  2) Re-enter parameters interactively  (overwrites config.json)"
  echo "  3) Load from an existing file         (overwrites config.json)"
  echo ""
  read -rp "Choice [1/2/3]: " _init_choice
  _cfg_key="${_init_choice}"
else
  echo "bootstrap/config.json not found."
  echo ""
  echo "  1) Enter all parameters interactively (creates config.json)"
  echo "  2) Load from an existing file"
  echo ""
  read -rp "Choice [1/2]: " _init_choice
  # remap so "1=wizard, 2=file" aligns with the has-file branch numbering
  case "${_init_choice}" in
    1) _cfg_key=2 ;;
    2) _cfg_key=3 ;;
    *) _cfg_key="${_init_choice}" ;;
  esac
fi

case "${_cfg_key}" in
  1)
    echo "  Using existing config.json"
    ;;
  2)
    _config_wizard
    ;;
  3)
    read -rp "  Path to config file: " _cfg_src
    if [[ ! -f "${_cfg_src}" ]]; then
      echo "ERROR: File not found: ${_cfg_src}"
      exit 1
    fi
    cp "${_cfg_src}" "${CONFIG_FILE}"
    chmod 600 "${CONFIG_FILE}"
    echo "  Loaded config from ${_cfg_src}"
    ;;
  *)
    echo "ERROR: Invalid choice. Run the script again."
    exit 1
    ;;
esac

# Env vars override config.json values (backward-compatible)
NODE1_IP="${NODE1_IP:-$(_cfg 'node.ip1')}"
NODE2_IP="${NODE2_IP:-$(_cfg 'node.ip2')}"
NODE3_IP="${NODE3_IP:-$(_cfg 'node.ip3')}"
VIP="${VIP:-$(_cfg 'node.vip')}"
GITHUB_OWNER="${GITHUB_OWNER:-$(_cfg 'github.owner')}"
GITHUB_REPO="${GITHUB_REPO:-$(_cfg 'github.repo')}"
GITHUB_TOKEN="${GITHUB_TOKEN:-$(_cfg 'github.token')}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$(_cfg 'aws.access_key_id')}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$(_cfg 'aws.secret_access_key')}"
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$(_cfg 'aws.region')}"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION

: "${NODE1_IP:?node.ip1 is required in config.json}"
: "${NODE2_IP:?node.ip2 is required in config.json}"
: "${NODE3_IP:?node.ip3 is required in config.json}"
: "${VIP:?node.vip is required in config.json}"
: "${GITHUB_OWNER:?github.owner is required in config.json}"
: "${GITHUB_REPO:?github.repo is required in config.json}"
: "${GITHUB_TOKEN:?github.token is required in config.json}"

_cfg_branch="$(_cfg 'github.branch')"
GITHUB_BRANCH="${GITHUB_BRANCH:-${_cfg_branch:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}}"

TALOS_DIR="${REPO_ROOT}/cluster/overlays/3-node/talos-machineconfigs"
TALOSCONFIG_PATH="${REPO_ROOT}/.talos/generated/talosconfig"

# ── Phase 1: Key Generation ───────────────────────────────────────────────────
echo ""
echo "=== Phase 1: Key Generation ==="

if [[ ! -f "${REPO_ROOT}/.age.key" ]]; then
  age-keygen -o "${REPO_ROOT}/.age.key"
  echo "SOPS age key generated — add public key to .sops.yaml and store private key offline"
else
  echo "SOPS age key already exists — skipping"
fi

echo ""
echo "=== Applying config.json to cluster files ==="
python3 "${REPO_ROOT}/bootstrap/scripts/apply-config.py" \
  --no-encrypt

# Encrypt all secrets that have been filled in
"${REPO_ROOT}/bootstrap/scripts/encrypt-secrets.sh"

# ── Phase 2: Talos Config Generation ─────────────────────────────────────────
echo ""
echo "=== Phase 2: Talos Config Generation ==="

mkdir -p "${REPO_ROOT}/.talos"

if [[ ! -f "${REPO_ROOT}/.talos/secrets.yaml" ]]; then
  talosctl gen secrets -o "${REPO_ROOT}/.talos/secrets.yaml"
  echo "Talos secrets bundle generated: .talos/secrets.yaml"
  echo "  -> CRITICAL: store this in your password manager — needed to add nodes or recover"
else
  echo "Talos secrets already exist — skipping"
fi

talosctl gen config homelab "https://${VIP}:6443" \
  --with-secrets "${REPO_ROOT}/.talos/secrets.yaml" \
  --config-patch-control-plane "@${TALOS_DIR}/controlplane.yaml" \
  --output-dir "${REPO_ROOT}/.talos/generated" \
  --force

# Inject the generated talosconfig into the system-upgrade-controller secret,
# now that Phase 2 has produced it. Same secret bootstrap-1node.sh fills in,
# just done here instead of after cluster bootstrap since the file already
# exists and nothing downstream needs it sooner.
python3 "${REPO_ROOT}/bootstrap/scripts/apply-config.py" \
  --talosconfig "${TALOSCONFIG_PATH}" \
  --no-encrypt
"${REPO_ROOT}/bootstrap/scripts/encrypt-secrets.sh"

export TALOSCONFIG="${TALOSCONFIG_PATH}"

echo ""
echo "=== Phase 3: Apply Talos Config ==="

for node in "$NODE1_IP" "$NODE2_IP" "$NODE3_IP"; do
  echo "Applying config to ${node}..."
  talosctl apply-config --insecure \
    --nodes "${node}" \
    --file "${REPO_ROOT}/.talos/generated/controlplane.yaml"
done

echo "Waiting 60s for nodes to boot..."
sleep 60

echo ""
echo "=== Phase 4: Bootstrap etcd ==="

talosctl bootstrap --nodes "${NODE1_IP}" \
  --talosconfig "${TALOSCONFIG_PATH}"

echo "Waiting 120s for cluster to form..."
sleep 120

talosctl kubeconfig --nodes "${NODE1_IP}" \
  --talosconfig "${TALOSCONFIG_PATH}" \
  --force

echo ""
echo "=== Phase 5: Flux Bootstrap ==="

kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey="${REPO_ROOT}/.age.key" \
  --dry-run=client -o yaml | kubectl apply -f -

_remote="https://${GITHUB_TOKEN}@github.com/${GITHUB_OWNER}/${GITHUB_REPO}.git"
if ! git ls-remote --heads "${_remote}" 2>/dev/null | grep -q "refs/heads/${GITHUB_BRANCH}"; then
  echo "Branch '${GITHUB_BRANCH}' does not exist on remote."
  read -rp "Create and push it now? [Y/n] " _ans
  if [[ "${_ans}" =~ ^[Nn]$ ]]; then
    echo "Aborted. Push the branch manually and re-run."
    exit 1
  fi
  git push "${_remote}" "HEAD:refs/heads/${GITHUB_BRANCH}"
  echo "Branch '${GITHUB_BRANCH}' pushed to remote."
fi

flux bootstrap github \
  --owner="${GITHUB_OWNER}" \
  --repository="${GITHUB_REPO}" \
  --branch="${GITHUB_BRANCH}" \
  --path="cluster/overlays/3-node" \
  --personal \
  --components-extra=image-reflector-controller,image-automation-controller

echo ""
echo "=== Phase 6: AWS Setup ==="

# The recovery system's AWS repository (ADR-012). If it already exists, this is
# a rebuild: its data is restored by hand once Flux is up, following
# docs/runbooks/backup-recovery.md, Part A (A7). Terraform reconciles only the
# bucket's configuration, never its contents.
_recovery_vault="$(_cfg 'cluster.name')-recovery-vault"
if aws s3api head-bucket --bucket "${_recovery_vault}" 2>/dev/null; then
  echo ""
  echo "The recovery vault ${_recovery_vault} already exists and may hold recovery points."
  echo "If this is a rebuild, restore application data by hand once Flux is up:"
  echo "  docs/runbooks/backup-recovery.md, Part A (A7)"
  echo ""
fi

: "${TF_VAR_budget_alert_email:?export TF_VAR_budget_alert_email (the address for the AWS budget alerts) before Phase 6}"
cd "${REPO_ROOT}/bootstrap/terraform"
terraform init -input=false
terraform apply -auto-approve \
  -var="cluster_name=$(_cfg 'cluster.name')" \
  -var="aws_region=$(_cfg 'aws.region')"
cd "${REPO_ROOT}"

# The recovery system's two AWS credentials, written SOPS-encrypted from the
# Terraform outputs.
"${REPO_ROOT}/scripts/make-recovery-credentials.sh"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Bootstrap Complete ==="
echo ""
echo "Next steps:"
echo "  1. Run PROFILE=3-node ./bootstrap/scripts/post-deploy.sh to check the deployment"
echo "  2. Commit the recovery system's AWS credentials written by make-recovery-credentials.sh,"
echo "     and the other SOPS-encrypted secrets apply-config.py just wrote from config.json"
echo "  3. Fill in and commit the secrets apply-config.py does not generate"
echo "     (docs/backlog.md, \"most SOPS secrets have no bootstrap generator\")"
echo "  4. Watch Flux reconcile: flux get all --watch"
echo "  5. Check cluster: kubectl get nodes && kubectl get pods -A"
echo ""
echo "IMPORTANT: Delete local key files after storing offline:"
echo "  rm .age.key"
echo "  (keep .talos/secrets.yaml in password manager, then delete local copy too)"
