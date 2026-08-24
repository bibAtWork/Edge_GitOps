#!/usr/bin/env bash
#
# Talos OS upgrade (design §5, "Loop A"). Flux cannot manage node OS images, so
# this lives outside it.
#
#   TALOS_VERSION / SCHEMATIC_ID come from talos-image.env
#   NODE_IPS is a space-separated list; SINGLE_NODE=true skips the drain
#
# Usage:
#   NODE_IPS="10.0.0.1" SINGLE_NODE=true ./scripts/talos-upgrade.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/../cluster/overlays/1-node/talos-machineconfigs/talos-image.env}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-1800}"
SINGLE_NODE="${SINGLE_NODE:-false}"

[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE not found" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${TALOS_VERSION:?not set in $ENV_FILE}"
: "${SCHEMATIC_ID:?not set in $ENV_FILE}"
: "${NODE_IPS:?set NODE_IPS to a space-separated list of node addresses}"

IMAGE="factory.talos.dev/installer/${SCHEMATIC_ID}:${TALOS_VERSION}"
echo "target image: ${IMAGE}"

# ---------------------------------------------------------------------------
# Health gates
# ---------------------------------------------------------------------------

# Loop WHILE any volume reports a robustness other than "healthy". An empty
# string means detached/not yet reporting, which is also acceptable.
#
# Do not "simplify" this to `until ... | grep -qv healthy`. `until` exits when
# its command SUCCEEDS, and `grep -v` succeeds the moment it finds one
# non-matching line -- so that form waits while everything is healthy and
# proceeds the instant a replica goes unhealthy, i.e. exactly when it is unsafe.
wait_for_longhorn_healthy() {
  local deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  while kubectl -n longhorn-system get volumes.longhorn.io \
          -o jsonpath='{.items[?(@.status.state=="attached")].status.robustness}' 2>/dev/null \
        | tr ' ' '\n' | grep -qvE '^(healthy)?$'; do
    if (( SECONDS > deadline )); then
      echo "ERROR: Longhorn volumes not healthy within ${HEALTH_TIMEOUT}s" >&2
      kubectl -n longhorn-system get volumes.longhorn.io >&2
      exit 1
    fi
    sleep 15
  done
  echo "  longhorn: all volumes healthy"
}

wait_for_etcd_healthy() {
  talosctl --nodes "$1" etcd status >/dev/null 2>&1 \
    || { echo "ERROR: etcd unhealthy on $1" >&2; exit 1; }
  echo "  etcd: healthy on $1"
}

# The extensions are the whole reason for the custom image, so verify they are
# actually present rather than trusting the exit code.
#
# talosctl upgrade has returned 0 while silently NOT upgrading: it cordons and
# drains by default, and on a single node there is nowhere to drain to, so every
# eviction times out against the client rate limiter and the drain error aborts
# the upgrade before the new image is written. The node comes back Ready on the
# OLD image. --force does not skip the drain either; --stage does, because it
# applies at boot before Kubernetes starts.
#
# Do not verify via /proc/modules: iSCSI modules load on demand, so their absence
# proves nothing. An ext- service entry does.
verify_extensions() {
  local node="$1"
  if talosctl --nodes "$node" services 2>/dev/null | grep -q 'ext-iscsid'; then
    echo "  extensions: ext-iscsid present"
  else
    echo "ERROR: ext-iscsid missing on ${node} -- the upgrade did not apply" >&2
    echo "       (talosctl upgrade can exit 0 without upgrading; see comment above)" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Rolling upgrade, strictly one node at a time
# ---------------------------------------------------------------------------
for NODE in ${NODE_IPS}; do
  echo "=== ${NODE} ==="

  if [[ "${SINGLE_NODE}" != "true" ]]; then
    NODE_NAME=$(kubectl get nodes -o jsonpath="{.items[?(@.status.addresses[?(@.address=='${NODE}')])].metadata.name}")
    kubectl cordon "${NODE_NAME}"
    # Blocks until Longhorn has rebuilt replicas elsewhere, enforced by
    # nodeDrainPolicy: block-if-contains-last-replica.
    kubectl drain "${NODE_NAME}" --ignore-daemonsets --delete-emptydir-data --timeout=600s
  else
    echo "  single node: skipping drain (nowhere to drain to, and"
    echo "  block-if-contains-last-replica would block forever)"
  fi

  # --stage writes the upgrade to META and applies it at boot, before Kubernetes
  # starts, so no drain is attempted. Staging alone does nothing -- the reboot
  # below is what applies it.
  talosctl upgrade --nodes "${NODE}" --image "${IMAGE}" --stage
  talosctl --nodes "${NODE}" reboot

  echo "  waiting for ${NODE} to return..."
  for _ in $(seq 1 60); do
    talosctl --nodes "${NODE}" services >/dev/null 2>&1 && break
    sleep 20
  done

  talosctl health --nodes "${NODE}" --wait-timeout 10m

  [[ "${SINGLE_NODE}" != "true" ]] && kubectl uncordon "${NODE_NAME}"

  # Gate on all three before touching the next node.
  verify_extensions "${NODE}"
  wait_for_etcd_healthy "${NODE}"
  wait_for_longhorn_healthy
  echo "  ${NODE} upgraded and healthy"
done

echo "all nodes upgraded to ${TALOS_VERSION}"
