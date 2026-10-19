# Talos A/B rollback rehearsal

**Status: planned, not executed.** This cluster currently has one control-plane
node (`talos-1ps-0l8`, `192.168.178.100`). A rollback reboots that node and
interrupts the Kubernetes API and every workload. It is a separate operation
from changing `TALOS_VERSION` in Git: [`talosctl rollback`](https://www.talos.dev/latest/reference/cli/#talosctl-rollback)
selects the previous installed Talos image and reboots. The previous slot's
version and compatibility with the currently running Kubernetes version must
be established before this rehearsal is scheduled. The CLI offers no dry-run.

Run this only in a staffed maintenance window outside the control-plane Plan's
Sunday 12:00–14:00 UTC upgrade window. Budget for two node reboots and leave
time to recover with the out-of-band console. Record actual start/end times,
versions, observations, and any deviation in the recovery runbook after the
exercise. Do not mark the [backlog item](../backlog.md#production-recovery-and-talos-rollback-rehearsal)
complete from this procedure alone; a production application cutover also needs
its own dated evidence.

## Preflight and abort conditions

1. Confirm the latest Keycloak, Immich, Paperless, and SeaweedFS recovery points
   are `VALIDATED` and their AWS copies `REMOTE_VERIFIED`. Read the records,
   not just Workflow phases, using the [recovery runbook](backup-recovery.md).
   Verify that the offline Talos secrets bundle and the local `talosconfig` can
   be read back. Capture a fresh etcd snapshot on the admin machine with
   `talosctl -n 192.168.178.100 etcd snapshot <offline-snapshot-path>`.
2. Record `talosctl -n 192.168.178.100 version --short`, `kubectl get node
   talos-1ps-0l8 -o wide`, `talosctl -n 192.168.178.100 etcd status`, and the
   current `talos-controlplane` Plan version. Identify the **previous installed
   slot's version** from reliable node/console evidence and verify it supports
   the running Kubernetes version and required system extensions. If that
   evidence is unavailable, stop; `talosctl rollback` is not a version picker.
3. Confirm there is no active system-upgrade-controller Job, and that the node
   does not match either an on-demand upgrade selector or the scheduled Plan's
   `talos.homelab/upgrade-ready=true` gate. Keep those gates closed until the
   original Talos version is restored; otherwise the controller can undo the
   rehearsal. Check that Flux will not reapply an override during the window.
4. Confirm out-of-band console access and the exact Image Factory installer
   image for the **current** version and schematic are available as a return
   route. Check that the node, etcd, Immich, Keycloak, and the public Gateway
   routes are healthy. Abort for missing escrow, unhealthy storage, an active
   upgrade, unknown previous slot, or no reliable return path.

## Rehearsal

In the maintenance window, announce the outage and stop application writers
for the selected cutover test. Record a baseline of pod readiness, application
API responses, and representative database/media counts. Then use the admin
`talosconfig` from a machine outside the cluster:

```sh
talosctl -n 192.168.178.100 rollback
```

Once the Talos API returns after reboot, run:

```sh
talosctl -n 192.168.178.100 version --short
talosctl -n 192.168.178.100 etcd status
talosctl -n 192.168.178.100 health
kubectl wait node/talos-1ps-0l8 --for=condition=Ready --timeout=15m
```

The version after reboot must equal the preflight's previous-slot version.
Check the same applications and data counts, the Gateway, persistent volumes,
and backup-controller health before starting writers. If the rollback fails to
boot or the API does not return, use the out-of-band console and the prepared
current-version installer/recovery material; do not retry arbitrary version
pins. If it boots but workload checks fail, keep writers stopped and restore
the known-good current version using the matching installer image, then repeat
all checks. The exact `talosctl upgrade --image` invocation depends on the
verified current schematic and is recorded at preflight rather than guessed
here.

After successful checks, return to the original Talos version, verify node
and application health again, reopen the upgrade gates, and resume writers.
Keep the post-rehearsal recovery point and the dated evidence with the
[recovery runbook](backup-recovery.md). The official Talos [CLI reference](https://www.talos.dev/latest/reference/cli/)
documents rollback as a return to the previous installation, not a downgrade
to an arbitrary release.
