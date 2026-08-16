# Security review — 2026-08-14

Live-cluster + GitOps review. Follows the 2026-07-26 Opus 5 review (that one's findings are
almost all closed; only M6 disk encryption — declined — and M8 PSS-privileged namespaces
remain outstanding, both unchanged). This review weights the ~18 days of work landed since:
Keycloak, OPA, KubeOpenCode, Envoy Gateway, and today's Gateway API CRD upgrade.

Every finding below was verified against the live cluster, not inferred from manifests.

---

## C1 (Critical) — KubeOpenCode server can escalate to cluster-admin

`kubeopencode-server`'s ClusterRole grants:

- `users, groups, serviceaccounts: [impersonate]` — **unrestricted**, no `resourceNames`
- `secrets: [get, list, watch]` — cluster-wide
- `pods/exec: [create]`, `pods/log: [get]`

Unrestricted group impersonation includes `system:masters`, which is bound to `cluster-admin`.
Verified directly:

```bash
kubectl auth can-i '*' '*' -A \
  --as=system:serviceaccount:kubeopencode-system:kubeopencode-server \
  --as-group=system:masters
# yes
```

So anything able to drive that pod gets full cluster-admin — read every Secret (Cloudflare API
token, Tailscale OAuth, Velero AWS creds, Keycloak admin, SeaweedFS S3), exec into any pod,
modify any resource.

**Exposure**: `kubeopencode-server` is the backend of the `kubeopencode` HTTPRoute
(`kubeopencode.homelab.data-harness.org`). Until PR #117 earlier today, that route had **no
authentication of any kind** — reachable by anyone on the LAN or tailnet. It is now gated by
OPA's `admin_only_apps`, which closes the immediate exposure but leaves the underlying
escalation intact.

**Origin**: this RBAC comes from the upstream `kubeopencode` Helm chart (v0.1.9 — an early
0.1.x release), not from anything written in this repo.

**Fix options**, roughly in order of preference:

1. Drop the impersonation rule outright if the product doesn't need it — patch the ClusterRole
   via a Kustomize patch on the HelmRelease output, or a post-render patch. Test that the UI
   still works; impersonation is often used only for "run as the logged-in user" features.
2. If impersonation is required, constrain it with `resourceNames` to a specific, non-privileged
   service account, so `system:masters` is unreachable.
3. Scope the `secrets` rule to the `kubeopencode-system` namespace (a Role, not a ClusterRole)
   unless cross-namespace secret reads are genuinely needed.
4. Consider whether this component belongs in the cluster at this maturity level (0.1.x chart,
   `:latest` images — see M1).

---

## H1 (High) — The "read-only" MCP server can read every Secret in the cluster

`kubernetes-mcp-server` runs as SA `mcp-viewer`, bound to `kubernetes-mcp-server-readonly`:

```yaml
apiGroups: ["", "apps", "batch", "networking.k8s.io"]
resources: ["*"]
verbs: ["get", "list", "watch"]
```

`resources: ["*"]` in the core API group includes `secrets`. Verified:

```bash
kubectl auth can-i list secrets -A \
  --as=system:serviceaccount:kubeopencode-system:mcp-viewer
# yes
```

"Read-only" is accurate but misleading here — read-only access to *every credential in the
cluster* is a full compromise of every downstream service those credentials reach (Cloudflare
DNS, AWS/S3, Tailscale, Keycloak). This is the tool surface exposed to an LLM agent, so
prompt-injection against content the agent reads is a realistic path to credential exfiltration.

**Fix**: replace `resources: ["*"]` with an explicit list excluding `secrets` (e.g. pods,
services, deployments, nodes, events, configmaps, ingresses, jobs). If the agent needs to know a
Secret *exists*, `get` on specific named secrets is far safer than blanket `list`.

---

## H2 (High) — CVE scanning has been silently broken; 0 vulnerability reports cluster-wide

```
configauditreports:   263
vulnerabilityreports:   0
```

Trivy's config-audit scanner works, but **image vulnerability scanning has never produced a
single report**. The scan Jobs are created, then fail to create pods:

```
Error creating: pods "scan-vulnerabilityreport-..." is forbidden:
violates PodSecurity "restricted:latest": runAsNonRoot != true ...,
seccompProfile ... must set securityContext.seccompProfile.type to "RuntimeDefault"
```

The `trivy-system` namespace enforces PSS `restricted`, and trivy-operator's scan pods don't
set `runAsNonRoot`/`seccompProfile`. Jobs sit `Running 0/1` forever and are eventually replaced.
(Kyverno's `restrict-image-registries` also flags these jobs, but it's `validationFailureAction:
Audit` so it only warns — PSS is what actually blocks.)

This is the most insidious class of finding: a security control that looks deployed and healthy
while providing zero coverage. There is currently **no CVE visibility for any image in the
cluster**.

**Fix**: set the scan-job security context via trivy-operator's Helm values
(`trivy.securityContext` / `operator.scanJobPodTemplateSecurityContext` — set `runAsNonRoot:
true`, `seccompProfile.type: RuntimeDefault`, drop ALL caps), rather than lowering the
namespace's PSS level. Then confirm `kubectl get vulnerabilityreports -A` is non-empty.
Also add the trivy scanner image's registry to the Kyverno allowlist to clear the warnings.

---

## H3 (High) — kube-proxy and flannel run alongside Cilium's kube-proxy replacement

Cilium reports `KubeProxyReplacement: True`, and its HelmRelease comment states this setup
"requires Talos to boot with no kube-proxy". But the Talos machineconfigs
(`cluster/overlays/{1,3}-node/talos-machineconfigs/`) never set `cluster.proxy.disabled: true`
or `cluster.network.cni.name: none`, so Talos's defaults are still in force:

```
kube-proxy     DaemonSet  1/1 Running  33d   registry.k8s.io/kube-proxy:v1.36.2
kube-flannel   DaemonSet  1/1 Running  33d   ghcr.io/siderolabs/flannel:v0.28.5
```

Both are live, not inert:

- kube-proxy is actively reconciling: `"Deleting stale nftables chains" numChains=6`, repeatedly
  through today.
- flannel is running its vxlan backend and writing iptables rules
  (`vxlan_network.go:68 watching for new subnet leases`, `iptables.go:358 bootstrap done`).

**Security impact**: two unnecessary host-network workloads — kube-proxy runs `privileged: true`,
flannel holds `NET_ADMIN`+`NET_RAW`. Both are pure attack surface for functionality Cilium
already provides.

**Suspected operational impact — tested 2026-08-16 and DISPROVEN**: this review proposed that
kube-proxy, not upstream cilium#44630/#44187, explained the LoadBalancer-VIP failure blocking the
Envoy Gateway cutover. The reasoning was that kube-proxy independently programs nftables DNAT for
the *same* LoadBalancer Services Cilium handles in eBPF — a well-known conflict producing exactly
the observed signature (SYN arrives, no SYN-ACK, no Cilium drop event and no policy verdict,
because nftables consumed the packet before Cilium saw it). The kube-proxy reconcile timestamps
also lined up with the test window (09:06–09:38, 11:05).

It was tested properly on 2026-08-16: the fix below was applied, both DaemonSets deleted, and the
node rebooted — after which a fresh test LoadBalancer VIP **still** timed out from an external
client. The hypothesis is dead.

The actual cause turned out to be a `CiliumNetworkPolicy` on the wrong ports:
`allow-world-ingress` permitted 80/443 on the Envoy Gateway pods, but Envoy Gateway serves
listener 80 on container port 10080 and 443 on 10443 (`useListenerPortAsContainerPort: false`,
so the proxy never needs `CAP_NET_BIND_SERVICE`). External traffic to a LoadBalancer VIP is
DNAT'd straight to the backend pod, so it arrived on 10080/10443 and was dropped. Correcting the
ports took the VIP from timeout to `200` in 0.007 s. Full write-up in
[`backlog.md`](backlog.md), section "Real root cause found (2026-08-16)".

**H3 itself remains valid and is worth fixing on its own merits** — it removes two privileged
host-network workloads. That is the only claim this finding should be credited with.

**Status: fixed 2026-08-16.** Applied to both overlays, verified live, all 8 apps unchanged
against a pre-change baseline measured from a real external LAN client.

**Fix**: add to the Talos machineconfig `cluster:` block —

```yaml
cluster:
  network:
    cni:
      name: none      # Cilium is the CNI
  proxy:
    disabled: true    # Cilium does kubeProxyReplacement
```

then `talosctl apply-config` and delete the leftover DaemonSets. Note this is a
disruptive networking change on a single control-plane node — do it with console access
available and a tested rollback, not casually.

---

## M1 (Medium) — `:latest` images in production, and the CI check doesn't catch them

```
ghcr.io/kubeopencode/kubeopencode:latest
ghcr.io/kubeopencode/kubeopencode-agent-opencode:latest
ghcr.io/kubeopencode/kubeopencode-agent-devbox:latest
quay.io/containers/kubernetes_mcp_server:latest
ghcr.io/bibatwork/schenkmatch:latest
```

The repo's "No :latest image tags" CI check passes, because it scans repo YAML — these tags come
from Helm chart defaults, which the check never sees. So the 2026-07-26 H5 fix ("all HelmReleases
pinned to exact versions") pinned the *charts* but not the *images they deploy*.

Highest concern on the KubeOpenCode images specifically, given C1: the workload holding a
cluster-admin escalation path can silently change content on any pod restart.

**Fix**: pin image tags/digests via Helm values for these charts. Extend the CI check to run
against rendered output (`helm template` / `flux build`) rather than raw manifests.

---

## M2 (Medium) — `kubeopencode-controller` holds broad cluster-wide write access

Separate from C1: cluster-wide `secrets` **and** `configmaps` with
`get,list,watch,create,update,patch,delete`, plus `pods`, `deployments`, `services`,
`pods/exec`. Verified it can `create pods/exec -A` and `delete deployments -A`.

Chart default again. Same remediation approach as C1 — scope to its own namespace where the
product allows.

---

## M3 (Medium) — Namespaces with no PSS enforcement

`default`, `flux-system`, `kube-system`, `cilium-secrets` carry no
`pod-security.kubernetes.io/enforce` label. `kube-system` is conventionally exempt, but
`default` and `flux-system` are worth labelling — `default` currently holds a leftover pod
(see L1), which is exactly the drift the label prevents.

**Fix**: label `default` as `restricted` (nothing legitimate should run there) and `flux-system`
as `baseline` or `restricted` after verifying the controllers comply.

Still outstanding from the previous review, unchanged: `monitoring`, `seaweedfs`, `tailscale`,
`system-upgrade`, `velero`, `falco`, `cattle-system`, `local-path-storage` at `privileged`
(prior review's M8 — needs architectural work to split trusted/untrusted workloads).

---

## M4 (Medium) — `cattle-system` has no CiliumNetworkPolicy

`cattle-system` runs `system-upgrade-controller`, which holds **cluster-admin** and can execute
arbitrary Plans against node OS images — one of the highest-value targets in the cluster. It has
no CiliumNetworkPolicy at all, so the cluster's default-deny model doesn't constrain it.
(`local-path-storage` is likewise uncovered, but is lower value.)

**Fix**: add an ingress/egress policy for `cattle-system` matching the pattern used elsewhere
(`allow-intra-namespace-egress` + kube-apiserver egress + whatever image registry it pulls from).

---

## L1 (Low) — Leftover debug/test pods

```
default/grpc-test              Completed   3d5h
monitoring/curl-test           Completed   17d
talos-backup/s3-check-12863    Error       5d22h
velero/fetch-log               Error       6d7h
velero/fetch-log-2             Error       5d23h
```

Cruft from past debugging sessions. Not exploitable as-is (all terminated), but they clutter
scanner output and `default/grpc-test` is the only workload in an unlabelled namespace.

**Fix**: delete them; they're not in git and won't come back.

---

## L2 (Low) — NodePorts bypass the Gateway's auth and TLS

`monitoring/grafana-nodeport` (30300) and `zot/zot-nodeport` (30500) expose services directly,
bypassing the Gateway — so no wildcard TLS and no OPA `ExternalAuth` gate. Both were
**unreachable** when tested from the LAN (`HTTP 000`), most likely blocked by Talos's host
firewall, so real-world risk today is low. Both backing services also have their own auth
(Grafana login, Zot htpasswd + accessControl).

**Fix**: confirm these are still wanted. If they're vestigial from before the Gateway worked,
delete them; if they're a deliberate break-glass path, document that and confirm what actually
gates access at the host firewall level.

---

## Verified healthy

Worth recording what held up, so future reviews don't re-litigate it:

- **Secrets management is solid.** Every Secret in git is SOPS-encrypted except two, both
  correctly so: an empty `tailscaled-subnet-router-state` (runtime-populated) and GitHub's
  *public* signing key (annotated with an explicit skip reason). The `.githooks/pre-push` SOPS
  guard and the `gitleaks` + `SOPS encryption` CI checks are all in place and passing.
- **Supply chain integrity**: Flux verifies commit signatures (`gotk-sync.yaml spec.verify`),
  images come from a small set of known registries, and Kyverno audits registry compliance.
- **OPA gate is now genuinely enforcing** (as of today's PRs #114/#117) — verified via OPA's own
  decision log, not just status codes.
- **cluster-admin bindings are minimal and all justified**: `system:masters`, Flux's
  kustomize/helm controllers, system-upgrade-controller, Velero.
- **etcd encryption at rest** is configured (`aescbc`), and an **API server audit policy** is in
  place (prior review's M15).
- **Default-deny network model** is applied cluster-wide with per-namespace exceptions.

---

## Suggested order of work

1. **C1** — constrain KubeOpenCode's impersonation. Biggest single risk; the auth gate added
   today mitigates exposure but not the underlying escalation.
2. **H2** — fix Trivy scan jobs. Cheap, and you currently have zero CVE visibility.
3. **H1** — scope the MCP server's read access away from Secrets.
4. **H3** — test the kube-proxy/flannel hypothesis. May resolve the Envoy Gateway blocker as a
   side effect; do the config change carefully.
5. **M1–M4**, then **L1–L2**.
