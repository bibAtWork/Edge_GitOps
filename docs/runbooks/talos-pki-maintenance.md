# Talos certificate maintenance

Talos rotates its etcd, Kubernetes, and Talos API server certificates. It does
not renew administrator `talosconfig` or `kubeconfig` client certificates. The
kubelet must restart at least once a year to rotate its certificates; a Talos
upgrade or node reboot does that. The Talos and Kubernetes root CAs are separate
ten-year trust roots. See the [Talos certificate guide](https://docs.siderolabs.com/talos/v1.14/security/cert-management)
and [CA rotation guide](https://docs.siderolabs.com/talos/v1.14/security/ca-rotation).

| Item | Owner and schedule | Current evidence |
| --- | --- | --- |
| Upgrade controller's `os:admin` Talos client certificate | Cluster admin; renew before 30 days remain, plan at least 90 days before expiry | `cluster-health.py --group certs` observed expiry **2027-07-11** on 2026-10-02; plan by **2027-04-11** |
| Administrator `talosconfig` and `kubeconfig` files | Each holder; renew at least annually and before expiry | Stored outside Git; record their dates in the offline admin inventory |
| Kubelet certificates | Cluster admin; ensure each node restarts within 365 days | Grafana `TalosKubeletRestartDue` warns after 330 days without a node reboot |
| Talos and Kubernetes API root CAs | Cluster admin; review expiry annually and plan a coordinated rotation before the ten-year expiry or after compromise/revocation | Keep CA dates and the latest `secrets.yaml` in the offline admin inventory |

The six-hour [cluster health workflow](../../.github/workflows/cluster-health.yml)
checks the live `cattle-system/talos-credentials` certificate and warns below 30
days (critical below 14). A failed workflow is not proof that the certificate
was renewed. The Grafana restart alert reads `node_boot_time_seconds` for each
node from node-exporter; it is a reminder, not an automatic reboot.

## Renew the upgrade controller credential

Do this while a valid offline `os:admin` talosconfig still works. Use an admin
workstation with `talosctl`, `sops`, `kubectl`, Python 3, and the SOPS age key.
The new credential is a distinct client key pair for the controller. Keep it
outside Git except for its SOPS-encrypted Secret. Do not run the general
`apply-config.py --talosconfig` path for a routine renewal: that bootstrap helper
also processes other application configuration.

From the repository root, set paths and a control-plane IP for this cluster:

```bash
set -euo pipefail
export SOPS_AGE_KEY_FILE=/secure/offline/sops.age.key
ADMIN_CONFIG=/secure/offline/admin-talosconfig
CONTROL_PLANE='REPLACE_WITH_CONTROL_PLANE_IP'
SECRET=cluster/base/infrastructure/15-system-upgrade-controller/operator/talos-credentials-secret.yaml
WORKDIR="$(mktemp -d)"                 # outside the repository
chmod 700 "$WORKDIR"
NEW_CONFIG="$WORKDIR/upgrade-talosconfig"

talosctl --talosconfig "$ADMIN_CONFIG" --nodes "$CONTROL_PLANE" \
  config new "$NEW_CONFIG" --roles os:admin --crt-ttl 8760h
chmod 600 "$NEW_CONFIG"
talosctl --talosconfig "$NEW_CONFIG" --nodes "$CONTROL_PLANE" version
```

The last command must authenticate successfully before changing Git. `os:admin`
is required by the Talos upgrade API. If the current admin certificate already
expired, recover access from the **offline** Talos secrets bundle using Talos's
client-certificate procedure; do not create or rotate a new root CA merely to
renew this client certificate.

Update only the encrypted `stringData.talosconfig` field. JSON encoding on
standard input preserves the YAML content and avoids putting the private key in
the process command line or a plaintext Git file:

```bash
python3 -c 'import json,pathlib,sys; sys.stdout.write(json.dumps(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")))' "$NEW_CONFIG" \
  | sops set --value-stdin "$SECRET" '["stringData"]["talosconfig"]'
sops --decrypt "$SECRET" >/dev/null
git diff --check
git diff -- "$SECRET"                   # ciphertext only
```

Commit this single Secret change, open a PR to `ops/talos_linux`, and let Flux
reconcile it. Keep the old, still-valid credential until the new one is verified
in the cluster. Then compare the live Secret with the generated file without
printing either credential, and rerun the certificate check:

```bash
kubectl get secret talos-credentials -n cattle-system -o jsonpath='{.data.talosconfig}' \
  | base64 -d | cmp - "$NEW_CONFIG"
python3 scripts/cluster-health.py --group certs --json
```

Check that `talos/upgrade-credential` reports the new expiry, that the Secret
comparison succeeded, and that the earlier `talosctl version` call worked with
the new client key. These checks cover the API identity, the encrypted Git
value, and the value mounted by future upgrade Jobs without triggering a node
upgrade. Record the date and new expiry in the table above and the offline admin
inventory. Securely remove the temporary file when finished. If authentication
or Secret reconciliation fails while the old credential remains valid, revert
the Secret commit and verify the old value is restored before retrying.

## Keep kubelet restarts within a year

`TalosKubeletRestartDue` warns when `time() - node_boot_time_seconds` exceeds
330 days. A normal upgrade usually resets the timer. If no upgrade is due,
schedule a controlled `talosctl reboot` before day 365. Check recent validated
recovery points and AWS verification first; the one-node profile has a full
outage during reboot. For a multi-node cluster, restart one node at a time and
wait for `Ready` before the next. Afterward, confirm that the boot time changed,
the node is Ready, the alert returned to Normal, and Talos reports healthy
Kubernetes certificates with `talosctl get KubernetesDynamicCerts -o yaml` on a
control-plane node. A root CA rotation is not part of this restart.

## Plan root CA rotation separately

Root CA rotation is an admin maintenance event, not a scheduled CronJob. At the
annual PKI review, check the protected bundle's CA expiry dates and inventory
every client and consumer: offline administrator talosconfigs, the upgrade
controller Secret, kubeconfigs, the offline `secrets.yaml`, and workloads that
trust the Kubernetes API CA. Arrange a maintenance window and a tested recovery
point before making changes. Use Talos's [CA rotation guide](https://docs.siderolabs.com/talos/v1.14/security/ca-rotation)
for separate Talos and Kubernetes dry runs, then a staged rotation. The Talos
API CA rotation requires new talosconfigs and an updated offline `secrets.yaml`;
Kubernetes CA rotation requires new kubeconfigs and may restart components or
require workload restarts. Verify all consumers and update the offline bundle
before closing the maintenance record. Never assume automatic server-certificate
renewal has changed the root CA keys.
