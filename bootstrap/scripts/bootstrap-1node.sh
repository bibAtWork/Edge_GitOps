#!/usr/bin/env bash
# Bootstrap script for the single-node profile.
# Prerequisites: talosctl, kubectl, flux, sops, age, terraform installed locally.
#
# Usage:
#   NODE_IP=192.168.1.10 \
#   GITHUB_OWNER=<your-github-user> GITHUB_REPO=homelab-cluster \
#   ./bootstrap/scripts/bootstrap-1node.sh
#
#   PRIMARY_DISK and BACKUP_DISK are prompted interactively in maintenance mode.
#   Set TALOS_VERSION when the node ISO version differs from talosctl
#   (e.g. TALOS_VERSION=v1.11.2). A post-bootstrap upgrade will be printed.
#
#   The script is idempotent: re-running resumes from where it left off.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/bootstrap/config.json"

# Read a dotted-path key from config.json (e.g. _cfg 'node.ip')
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

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "ERROR: bootstrap/config.json not found."
  echo "Copy bootstrap/config.json.template to bootstrap/config.json and fill in values."
  exit 1
fi

# Env vars override config.json values (backward-compatible)
NODE_IP="${NODE_IP:-$(_cfg 'node.ip')}"
GITHUB_OWNER="${GITHUB_OWNER:-$(_cfg 'github.owner')}"
GITHUB_REPO="${GITHUB_REPO:-$(_cfg 'github.repo')}"
GITHUB_TOKEN="${GITHUB_TOKEN:-$(_cfg 'github.token')}"
PRIMARY_DISK="${PRIMARY_DISK:-$(_cfg 'node.primary_disk')}"
BACKUP_DISK="${BACKUP_DISK:-$(_cfg 'node.backup_disk')}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$(_cfg 'aws.access_key_id')}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$(_cfg 'aws.secret_access_key')}"
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$(_cfg 'aws.region')}"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION

: "${NODE_IP:?node.ip is required in config.json}"
: "${GITHUB_OWNER:?github.owner is required in config.json}"
: "${GITHUB_REPO:?github.repo is required in config.json}"
: "${GITHUB_TOKEN:?github.token is required in config.json}"

_cfg_branch="$(_cfg 'github.branch')"
GITHUB_BRANCH="${GITHUB_BRANCH:-${_cfg_branch:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}}"
TALOS_VERSION="${TALOS_VERSION:-}"
KUBERNETES_VERSION="${KUBERNETES_VERSION:-}"

if [[ -n "${TALOS_VERSION}" && -z "${KUBERNETES_VERSION}" ]]; then
  KUBERNETES_VERSION=$(curl -fsSL \
    "https://raw.githubusercontent.com/siderolabs/talos/${TALOS_VERSION}/pkg/machinery/constants/constants.go" 2>/dev/null \
    | grep -oP 'DefaultKubernetesVersion\s*=\s*"v?\K[^"]+' | head -1)
  if [[ -z "${KUBERNETES_VERSION}" ]]; then
    echo "Warning: could not auto-detect Kubernetes version for Talos ${TALOS_VERSION}. Set KUBERNETES_VERSION manually."
  else
    echo "Using Kubernetes ${KUBERNETES_VERSION} (default for Talos ${TALOS_VERSION})"
  fi
fi

TALOS_DIR="${REPO_ROOT}/cluster/overlays/1-node/talos-machineconfigs"
TALOSCONFIG_PATH="${REPO_ROOT}/.talos/generated/talosconfig"

# ── Helpers ───────────────────────────────────────────────────────────────────
_wait_until() {
  local desc="$1" timeout="$2"; shift 2
  local deadline=$(( $(date +%s) + timeout ))
  until "$@" &>/dev/null; do
    (( $(date +%s) < deadline )) || { echo "ERROR: timed out waiting for ${desc}"; exit 1; }
    echo "  waiting for ${desc}..."
    sleep 5
  done
  echo "  ${desc} ready"
}

_in_maintenance_mode() {
  talosctl get disks --insecure --nodes "${NODE_IP}" &>/dev/null 2>&1
}

# ── Pre-load TALOSCONFIG for re-runs ─────────────────────────────────────────
if [[ -f "${TALOSCONFIG_PATH}" ]]; then
  export TALOSCONFIG="${TALOSCONFIG_PATH}"
  talosctl config endpoint "${NODE_IP}" 2>/dev/null || true
  talosctl config node "${NODE_IP}" 2>/dev/null || true
fi

# ── Disk selection (maintenance mode only) ────────────────────────────────────
if _in_maintenance_mode && { [[ -z "${PRIMARY_DISK:-}" ]] || [[ -z "${BACKUP_DISK:-}" ]]; }; then
  echo "=== Available disks on ${NODE_IP} ==="
  talosctl get disks --insecure --nodes "${NODE_IP}"
  echo ""
  echo "Enter the WWID of each disk (WWID column above). Avoid TRANSPORT=usb drives."
  echo ""
  if [[ -z "${PRIMARY_DISK:-}" ]]; then
    read -rp "Primary disk WWID: " _wwid
    PRIMARY_DISK="/dev/disk/by-id/${_wwid}"
  fi
  if [[ -z "${BACKUP_DISK:-}" ]]; then
    read -rp "Backup disk WWID:  " _wwid
    BACKUP_DISK="/dev/disk/by-id/${_wwid}"
  fi
fi
PRIMARY_DISK="${PRIMARY_DISK:-}"
BACKUP_DISK="${BACKUP_DISK:-}"

# ── Phase 1: Key Generation ───────────────────────────────────────────────────
echo ""
echo "=== Phase 1: Key Generation ==="

if [[ ! -f "${REPO_ROOT}/.age.key" ]]; then
  age-keygen -o "${REPO_ROOT}/.age.key"
  echo "SOPS age key generated — add public key to .sops.yaml and store private key offline"
else
  echo "SOPS age key already exists — skipping"
fi

if [[ ! -f "${REPO_ROOT}/.talos-backup-age.key" ]]; then
  age-keygen -o "${REPO_ROOT}/.talos-backup-age.key"
  echo "talos-backup age key generated — store OFFLINE (needed to decrypt etcd snapshots)"
else
  echo "talos-backup age key already exists — skipping"
fi

TALOS_BACKUP_PUBLIC_KEY=$(grep 'public key' "${REPO_ROOT}/.talos-backup-age.key" | awk '{print $4}')

echo ""
echo "=== Applying config.json to cluster files ==="
python3 "${REPO_ROOT}/bootstrap/scripts/apply-config.py" \
  --age-public-key "${TALOS_BACKUP_PUBLIC_KEY}" \
  --no-encrypt

# Encrypt all secrets that have been filled in
"${REPO_ROOT}/bootstrap/scripts/encrypt-secrets.sh"

# ── Phase 2: Talos Config Generation ─────────────────────────────────────────
echo ""
echo "=== Phase 2: Talos Config Generation ==="

mkdir -p "${REPO_ROOT}/.talos"

if [[ ! -f "${REPO_ROOT}/.talos/secrets.yaml" ]]; then
  talosctl gen secrets -o "${REPO_ROOT}/.talos/secrets.yaml"
  echo "Talos secrets bundle generated — store offline"
else
  echo "Talos secrets already exist — skipping"
fi

talos_version_flag=()
[[ -n "${TALOS_VERSION}" ]] && talos_version_flag=(--talos-version "${TALOS_VERSION}")
kubernetes_version_flag=()
[[ -n "${KUBERNETES_VERSION}" ]] && kubernetes_version_flag=(--kubernetes-version "${KUBERNETES_VERSION}")

_gen_dir="${REPO_ROOT}/.talos/generated"
_talosconfig_bak="${_gen_dir}/talosconfig.bak"

# Preserve existing talosconfig — regeneration creates a new client cert which
# can cause TLS errors against an already-running node.
[[ -f "${TALOSCONFIG_PATH}" ]] && cp "${TALOSCONFIG_PATH}" "${_talosconfig_bak}"

talosctl gen config homelab "https://${NODE_IP}:6443" \
  --with-secrets "${REPO_ROOT}/.talos/secrets.yaml" \
  --config-patch-control-plane "@${TALOS_DIR}/controlplane.yaml" \
  --output-dir "${_gen_dir}" \
  --force \
  "${talos_version_flag[@]}" \
  "${kubernetes_version_flag[@]}"

# Restore the existing talosconfig if the node is already running
if [[ -f "${_talosconfig_bak}" ]]; then
  mv "${_talosconfig_bak}" "${TALOSCONFIG_PATH}"
fi

export TALOSCONFIG="${TALOSCONFIG_PATH}"
talosctl config endpoint "${NODE_IP}"
talosctl config node "${NODE_IP}"

# ── State detection ───────────────────────────────────────────────────────────
# States (in order): maintenance → installed → bootstrapped → k8s-ready → flux-ready
_detect_state() {
  if _in_maintenance_mode; then
    echo "maintenance"
  elif kubectl get nodes &>/dev/null 2>&1; then
    # Cluster is reachable — check Flux
    if kubectl -n flux-system get deployment source-controller &>/dev/null 2>&1; then
      echo "flux-ready"
    else
      echo "k8s-ready"
    fi
  elif talosctl version --nodes "${NODE_IP}" &>/dev/null 2>&1; then
    # Talos API reachable but cluster not yet up — check etcd
    if talosctl service etcd --nodes "${NODE_IP}" 2>/dev/null | grep -q "RUNNING"; then
      echo "bootstrapped"   # etcd running, kubeconfig not yet fetched
    else
      echo "installed"      # Talos installed, etcd not yet bootstrapped
    fi
  else
    echo "unreachable"
  fi
}

_state=$(_detect_state)
echo ""
echo "Node state: ${_state}"

# ── Phase 3: Talos install + bootstrap ───────────────────────────────────────
echo ""
echo "=== Phase 3: Apply Config and Bootstrap ==="

case "${_state}" in
  unreachable)
    echo "ERROR: Node ${NODE_IP} is unreachable — check power and network"
    exit 1
    ;;
  maintenance)
    echo "Applying config (maintenance mode)..."
    talosctl apply-config --insecure \
      --nodes "${NODE_IP}" \
      --file "${REPO_ROOT}/.talos/generated/controlplane.yaml"
    echo "Node installing Talos — waiting for API..."
    _wait_until "Talos API" 600 talosctl version --nodes "${NODE_IP}"
    _state="installed"
    ;;&
  installed)
    echo "Bootstrapping etcd..."
    talosctl bootstrap --nodes "${NODE_IP}"
    _wait_until "etcd" 300 \
      bash -c "talosctl service etcd --nodes '${NODE_IP}' 2>/dev/null | grep -q RUNNING"
    _state="bootstrapped"
    ;;&
  bootstrapped)
    talosctl kubeconfig --nodes "${NODE_IP}" --force
    _wait_until "Kubernetes API" 300 kubectl get nodes
    _state="k8s-ready"

    # Inject the generated talosconfig into the system-upgrade-controller secret
    python3 "${REPO_ROOT}/bootstrap/scripts/apply-config.py" \
      --talosconfig "${TALOSCONFIG_PATH}" \
      --no-encrypt
    "${REPO_ROOT}/bootstrap/scripts/encrypt-secrets.sh"
    ;;&
  k8s-ready|flux-ready)
    echo "Talos and Kubernetes already up — skipping to Flux"
    ;;
esac

# ── Phase 4: Flux Bootstrap ───────────────────────────────────────────────────
echo ""
echo "=== Phase 4: Flux Bootstrap ==="

kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic sops-age \
  --namespace=flux-system \
  --from-file=age.agekey="${REPO_ROOT}/.age.key" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace talos-backup --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic talos-backup-age \
  --namespace=talos-backup \
  --from-literal="public-key=${TALOS_BACKUP_PUBLIC_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

if [[ "${_state}" == "flux-ready" ]]; then
  echo "Flux already bootstrapped — skipping"
else
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
    --path="cluster/overlays/1-node" \
    --personal \
    --components-extra=image-reflector-controller,image-automation-controller
fi

# ── Phase 5: AWS S3 Setup ─────────────────────────────────────────────────────
echo ""
echo "=== Phase 5: AWS S3 Setup ==="

cd "${REPO_ROOT}/bootstrap/terraform"
terraform init -input=false
terraform apply -auto-approve \
  -var="cluster_name=$(_cfg 'cluster.name')" \
  -var="aws_region=$(_cfg 'aws.region')"

# Capture IAM credentials from Terraform output and fill Velero secret
VELERO_KEY=$(terraform output -raw velero_access_key_id)
VELERO_SECRET=$(terraform output -raw velero_secret_access_key)
cd "${REPO_ROOT}"

python3 "${REPO_ROOT}/bootstrap/scripts/apply-config.py" \
  --velero-access-key "${VELERO_KEY}" \
  --velero-secret-key "${VELERO_SECRET}" \
  --no-encrypt
"${REPO_ROOT}/bootstrap/scripts/encrypt-secrets.sh"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Bootstrap Complete ==="
echo ""
echo "Next steps:"
echo "  1. Run ./bootstrap/scripts/post-deploy.sh --profile=1-node to create SeaweedFS buckets"
echo "  2. Update overlays/1-node/patches/seaweedfs-single.yaml with disk paths:"
echo "     PRIMARY_DISK=${PRIMARY_DISK}"
echo "     BACKUP_DISK=${BACKUP_DISK}"
echo "  3. Add SOPS-encrypted secrets for Cloudflare, Tailscale, Velero AWS creds"
echo ""
if [[ -n "${TALOS_VERSION}" ]]; then
  _client_ver=$(talosctl version --client 2>/dev/null | awk '/Tag:/{print $2}')
  echo "Node is running ${TALOS_VERSION} — upgrade to match talosctl:"
  echo "  talosctl upgrade --nodes ${NODE_IP} --image ghcr.io/siderolabs/talos:${_client_ver}"
  echo ""
fi
echo "IMPORTANT: Delete local key files after storing offline:"
echo "  rm .age.key .talos-backup-age.key"
