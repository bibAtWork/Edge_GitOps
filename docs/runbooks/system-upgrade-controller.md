# Upgrade controller settings and kubectl compatibility

The controller in `cattle-system` reads `system-upgrade-controller-config` only
when its Pod starts. Flux can apply a ConfigMap change without disturbing an
upgrade Plan. Restart the Deployment separately, in a staffed maintenance
window. A restart on 2026-10-24 resumed a failed applying Plan and rebooted
the single node outside its scheduled window.

CI compares the proposed `rancher/kuberlr-kubectl` image in the controller
ConfigMap with both upgrade-gate images, inspects the **bundled executables** in
that image, and requires a client within one Kubernetes minor of both the base
and target API versions. Keep any image update in the same PR as a version bump.
The required check is **Kubectl image compatibility**. The Sunday 11:00 UTC
pre-upgrade gate then checks the real gate Pod image against the live controller
ConfigMap, runs its `kubectl get node` against the live API, checks selected
client skew against the live API and target, and requires the controller Pod to
have started after the last ConfigMap write. A failed check removes the
`talos.homelab/upgrade-ready` label and prevents scheduled Plans from acting.

After a controller ConfigMap change is merged and reconciled:

1. In a staffed window, inspect `kubectl -n cattle-system get plans -o yaml`.
   Review `status.applying`, `status.latestHash`, and every `Complete` condition.
   An applying or failed Plan may resume immediately on controller restart;
   resolve it or prepare for that node upgrade before continuing.
2. Confirm the ConfigMap has the intended image and the gate CronJob uses the
   same tag with `kubectl -n cattle-system get cm
   system-upgrade-controller-config -o yaml` and `kubectl -n longhorn-system
   get cronjob upgrade-backup-gate -o yaml`.
3. Run `kubectl -n cattle-system rollout restart
   deployment/system-upgrade-controller`, then `kubectl -n cattle-system
   rollout status deployment/system-upgrade-controller --timeout=10m`.
4. Check `kubectl -n cattle-system get cm system-upgrade-controller-config
   --show-managed-fields -o yaml` and `kubectl -n cattle-system get pods -l
   app.kubernetes.io/name=system-upgrade-controller -o wide`. The one ready
   controller Pod must have started after the newest ConfigMap managed-field
   timestamp. Inspect `kubectl -n cattle-system logs
   deployment/system-upgrade-controller --since=10m` for a resumed Plan.
5. Before the next upgrade window, inspect the 11:00 gate Job and its logs:
   `kubectl -n longhorn-system get jobs --sort-by=.metadata.creationTimestamp`
   and `kubectl -n longhorn-system logs job/<gate-job-name>`. Look for the
   `kubectl ... can reach a node` and `GATE_OK` messages, then confirm the
   `talos.homelab/upgrade-ready=true` label on the intended nodes. If the gate
   refuses, fix the cause and rerun it before the upgrade window.

An image that lacks a compatible kubectl fails CI even if Renovate grouped its
tag with the version bump. A changed ConfigMap cannot silently reach the next
upgrade: the gate stays closed until the staffed restart is complete.
