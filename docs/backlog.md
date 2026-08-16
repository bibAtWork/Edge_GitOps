# Backlog

Known open issues that aren't yet fixed. Not a full project backlog — just things worth not forgetting.

---

## Security review findings (2026-08-14) — all open

Full write-up with verification commands and fix options: [`security-review-2026-08-14.md`](security-review-2026-08-14.md).
Everything below was verified against the live cluster. Suggested order is C1 → H2 → H1 → H3 → M/L.

| ID | Sev | Finding |
|---|---|---|
| C1 | Critical | `kubeopencode-server` has unrestricted `impersonate` on users/groups → can impersonate `system:masters` = cluster-admin. Also cluster-wide secrets read. From upstream chart v0.1.9, not repo code. Exposure was unauthenticated until PR #117. |
| H1 | High | `mcp-viewer` ("read-only" MCP server) has `resources: ["*"]` on the core API group → can list every Secret cluster-wide. LLM tool surface, so prompt-injection → credential exfil is a realistic path. |
| H2 | High | Trivy CVE scanning silently broken: 0 vulnerabilityreports vs 263 configauditreports. Scan Jobs blocked by `trivy-system`'s own PSS `restricted`. No CVE visibility for any image. |
| H3 | High | ~~kube-proxy + flannel DaemonSets still running despite Cilium `kubeProxyReplacement: True` — Talos machineconfig never disables them. Unnecessary privileged workloads.~~ **Fixed 2026-08-16**: both disabled in the machineconfig, DaemonSets deleted, node rebooted, all 8 apps verified unchanged. (Was also hypothesised as the LoadBalancer-VIP root cause — **tested and disproven**, see the ADR-001 section.) |
| M1 | Medium | `:latest` images (kubeopencode ×3, mcp-server, schenkmatch) from chart defaults; the "No :latest" CI check only scans repo YAML, not rendered charts. |
| M2 | Medium | `kubeopencode-controller`: cluster-wide secrets+configmaps write, `pods/exec`, deployments delete. |
| M3 | Medium | `default` and `flux-system` have no PSS enforce label. |
| M4 | Medium | `cattle-system` (runs system-upgrade-controller, which holds cluster-admin) has no CiliumNetworkPolicy. |
| L1 | Low | 5 leftover debug/test pods, up to 17d old. |
| L2 | Low | grafana/zot NodePorts bypass Gateway auth+TLS (currently unreachable from LAN, likely Talos host firewall). |

Prior review (2026-07-26) is essentially all fixed; still outstanding from it: **M6** (no Talos disk encryption — deliberately skipped) and **M8** (several namespaces at PSS `privileged` — needs architectural work).

---

## OPA `ext_authz` gate is inert for every app behind the Gateway

**Status:** Fixed 2026-08-14 — see dated section below. Sections above that date describe the original bug and are kept for history.

`25-gateway-authz/envoy-config.yaml`'s `CiliumClusterwideEnvoyConfig` gates traffic by having Cilium's eBPF datapath redirect connections **destined to a Service's ClusterIP** to a custom Envoy listener (`gateway-authz`) that runs `local_ratelimit → ext_authz(OPA) → router` before forwarding on.

Cilium's own Gateway API implementation does **not** route through Service ClusterIPs — it resolves backends via EDS directly to pod IPs. Confirmed via `cilium-dbg envoy admin clusters`: the Gateway's per-backend clusters use `eds_service_name` resolving straight to pod IPs, never the ClusterIP. Since the eBPF redirect only triggers on "destination IP is a known Service ClusterIP," it never fires for Gateway-routed traffic.

**Affects every app in `spec.services`**: grafana, immich, paperless, schenkmatch, zot, hubble-ui. Confirmed empirically (invalid Bearer token against Grafana got a normal 302, not the expected 401; an unconditional `allow := false` policy broke nothing).

**Practical impact**: apps with native login (Grafana, Paperless, Immich) are unaffected in practice — OPA was defense-in-depth for them. **Hubble UI has no native auth and is currently fully open** to anyone who can reach `hubble.homelab.data-harness.org`.

**Fix options**:

1. **Native `ExternalAuth` HTTPRoute filter (GEP-1494)** — the actual correct fix, identified 2026-08-12, **implemented and verified working 2026-08-14** (see dated sections below). Replaced the whole `25-gateway-authz/envoy-config.yaml` redirect mechanism, which could never work (see above).
2. **oauth2-proxy in front of Hubble UI** — attempted 2026-08-12, blocked (see below). Standard pattern for an app with no native OIDC: a proxy pod does real browser-based OIDC login against Keycloak, then forwards to hubble-ui. Sidesteps the EDS/ClusterIP mismatch entirely since it's ordinary Gateway→backend routing to a pod that enforces its own auth. Probably superseded by option 1 — no need to retry this once the CRD upgrade is done.
3. **Network-level restriction** — lock `hubble.homelab.data-harness.org` down via Cilium L3/L4 CiliumNetworkPolicy (source IP/namespace) instead of OIDC. Simpler, less capable. Not attempted.

The Rego policy logic itself was already fixed and verified correct (2026-08-12) — don't re-debug it. The problem is purely that traffic never reaches OPA's decision.

### oauth2-proxy attempt (2026-08-12) — abandoned and cleaned up

Built the full stack: a `hubble` Keycloak client (confidential, no PKCE — oauth2-proxy doesn't send `code_challenge`), an oauth2-proxy Deployment/Service/ConfigMap/Secret in `kube-system` (`23-hubble-auth`, never committed), CiliumNetworkPolicies for gateway ingress and Keycloak egress, and an HTTPRoute update pointing `hubble.homelab.data-harness.org` at the new `hubble-auth` service.

Login itself worked end to end — Keycloak authenticated the user, oauth2-proxy logged `[AuthSuccess]` with the correct `groups:[admin]` claim. But the **final hop, oauth2-proxy proxying to hubble-ui (same namespace, same as its own upstream)**, returned Cilium's generic `403 Access denied` — even with a `CiliumNetworkPolicy` `fromEndpoints` rule that label-matched exactly, and even after additionally adding the broader `allow-intra-namespace-ingress` fix that resolved the identical-looking problem for Keycloak earlier the same day. `allowed-ingress-identities` never grew beyond the fixed reserved set `[1,3,4,5,6,7,8,11]` — regular pod identities never appeared there regardless of which policy was added. **In hindsight, this was almost certainly an early manifestation of the general Cilium policy-realization bug documented in the ADR-001 section below** — the identical symptom (reserved-only `allowed-ingress-identities`, regardless of policy) was independently rediscovered and much more thoroughly isolated on 2026-08-14.

**Cleaned up**: the oauth2-proxy Deployment/Service/ConfigMap/Secret, its CiliumNetworkPolicies, and the Keycloak `hubble` client were all removed from the live cluster; the HTTPRoute was reverted to point at `hubble-ui` directly (restoring the old fully-open-but-functional state). Nothing from this attempt remains — Hubble UI is reachable but has no auth of its own, which is `OPA ext_authz gate is inert`'s practical impact above.

### Gateway 403 confirmed cluster-wide, unrelated to any specific app (2026-08-12) — RESOLVED 2026-08-14, not a bug

While deploying KubeOpenCode (separate PR), its HTTPRoute hit the same `403 Access denied` documented above. Ran the actual documented diagnostic this time (Envoy debug logging + NPDS dump — see `Agent.md`) instead of guessing at policies, and confirmed: this is **not** Tailscale-specific (reproduced with a pure in-cluster pod → Gateway-IP request), currently affects `monitoring`/grafana too (previously the one namespace that worked), and is **not caused by the kubeopencode work** — verified by reverting the (uncommitted) Gateway edit and confirming the 403 persists with the Gateway in its exact prior state. A promising lead (missing ipcache entry for the Gateway's L2-announced IP) was tested via a Cilium agent restart and ruled out as the sole cause. Full diagnostic detail and next steps in `Agent.md`'s "Follow-up (2026-08-12, still unresolved)" section — the concrete next lead is cross-referencing the `cil_from_netdev`/`bpf_metadata` direction-misclassification signature against upstream Cilium GitHub issues, since it looks like a known bug class rather than something specific to this cluster's config.

**Resolution (2026-08-14)**: not a bug, not a production issue — a test-methodology artifact. Every reproduction above used an **in-cluster test pod** as the client. Per [cilium/cilium#47617](https://github.com/cilium/cilium/issues/47617) (a maintainer-confirmed, documented behavior, not a bug), Cilium enforces policy for pod-origin Gateway traffic against the *client pod's own egress* reaching the real resolved backend — not against the Gateway itself. This cluster's per-namespace `allow-intra-namespace-egress`-only model means in-cluster pods were never going to satisfy that for cross-namespace apps, regardless of how the Gateway's own policy was configured. **Genuine external/LAN/Tailscale clients were never affected.** Verified live from a real external LAN client: all 8 apps behind `homelab-gateway` respond correctly (grafana/paperless/keycloak → `302` login redirects with real backend response times; immich/schenkmatch/zot/kubeopencode → `200`; hubble-ui → `403` with OPA's own JSON body, the *correct* result of the fix above, not this bug). Full writeup and the live proof table in `Agent.md`'s "Gateway 403 cluster-wide — resolved" section. **When testing Gateway reachability, use a genuinely external client — an in-cluster test pod produces false-negative 403s indistinguishable from a real bug.**

### The real fix for the OPA gate found, blocked on a Gateway API CRD upgrade (2026-08-12)

Researched rather than guessed this time (see the general IAM concept section below for the full context that prompted this). Confirmed via web research: Cilium's Gateway API implementation had no way to attach `ext_authz`/arbitrary Envoy filters to Gateway-routed traffic at all — a known upstream gap ([cilium/cilium#45704](https://github.com/cilium/cilium/issues/45704), Gateway API's own `ExternalAuth` HTTPRoute filter, GEP-1494). This merged and shipped in **Cilium 1.20.0**, which is the exact version already running in this cluster.

Implemented it — added `filters: [{type: ExternalAuth, externalAuth: {protocol: GRPC, backendRef: {name: opa, namespace: security, port: 9191}}}]` to all 7 app HTTPRoutes, a `ReferenceGrant` for the cross-namespace `backendRef`, an ingress `CiliumNetworkPolicy` for OPA, and removed the dead `25-gateway-authz/envoy-config.yaml` CiliumClusterwideEnvoyConfig. **All 7 `kubectl apply`s were rejected**: `strict decoding error: unknown field "spec.rules[0].filters[0].externalAuth"`.

Root cause: `ExternalAuth` is an **Experimental**-channel Gateway API field, only present in the CRD schema from **Gateway API v1.4.0 onward**. This cluster's installed CRDs (`00-gateway-api/standard-install.yaml`) are **standard channel, v1.2.1** — two gaps at once (too old, and the standard channel never carries Experimental fields regardless of version). Confirmed Gateway API **v1.4.1 experimental channel** is compatible with the installed Cilium 1.20.

**This is a bigger, riskier change than the app-level fix itself** — it's the foundational CRDs every Gateway/HTTPRoute/ReferenceGrant in the cluster depends on, not something scoped to OPA or any one app. Asked before proceeding; decision was to stop here rather than do the CRD upgrade in the same pass. **All changes were fully reverted** (git working tree and live cluster both confirmed back to original state — the old broken `envoy-config.yaml` CCEC was restored, OPA's ConfigMap reverted, the `ReferenceGrant` and new CiliumNetworkPolicy deleted). Nothing is left half-applied.

**To pick this up**: upgrade `00-gateway-api/standard-install.yaml` from the standard v1.2.1 bundle to the experimental v1.4.1 bundle (`https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.1/experimental-install.yaml`), verify the existing `Gateway` and all `HTTPRoute`/`ReferenceGrant` objects remain `Accepted` afterward, then redo the `ExternalAuth` filter addition described above (the exact YAML is known-correct, only the CRD version was the blocker).

### Gateway API CRD upgrade landed (2026-08-14)

Done — `00-gateway-api/standard-install.yaml` replaced with the full v1.4.1 experimental bundle (`experimental-install.yaml`). Along the way:

- `cilium-gateway-api-compat.yaml` (a full CRD resource adding `v1` to TLSRoute and BackendTLSPolicy, needed because the standard bundle didn't ship either) is now redundant for BackendTLSPolicy — the v1.4.1 experimental bundle ships `v1` for it natively. TLSRoute still only ships `v1alpha2`/`v1alpha3` upstream, so that half was kept, converted to a strategic-merge patch (`tlsroute-v1-patch.yaml`, same pattern as the existing `referencegrant-v1-patch.yaml`) instead of a competing full-resource definition, since the bundle now defines `tlsroutes.gateway.networking.k8s.io` itself.
- **Unexpected blocker, found and fixed**: applying the new bundle via `kubectl apply` failed outright with `metadata.annotations: Too long: may not be more than 262144 bytes` — these CRD schemas are big enough that the `last-applied-configuration` annotation client-side apply writes blows the 256KB annotation limit. Switched to `--server-side` apply (which Flux's `kustomize-controller` already uses by default, so this only affected manual verification, not the real GitOps path).
- **Second unexpected blocker, found and fixed**: server-side apply then failed with 3-way field conflicts against `helm-controller`. Root cause: the `envoy-gateway` HelmRelease's chart (`gateway-helm`) bundles a `crds` subchart that installs its own copy of the *same* upstream Gateway API CRDs (`crds.enabled: true` by default) alongside Envoy Gateway's own CRDs (EnvoyProxy, SecurityPolicy, etc., in the same subchart) — a standing ownership conflict with `00-gateway-api`, not a one-off. Fixed by setting `crds.enabled: false` on that HelmRelease (the chart's own docs recommend this exact flag for exactly this scenario); Envoy Gateway's own CRDs are unaffected since Flux/Helm never deletes CRDs a chart stops rendering.
- Verified live: all 12 CRDs apply cleanly, existing `Gateway` (`Programmed: True`, `192.168.178.200`) and all 9 `HTTPRoute`s unaffected, Cilium operator reconciles with no Gateway API errors, prod traffic still returns `403` (unchanged baseline).

**Next**: redo the `ExternalAuth` filter addition (YAML already known-correct from the 2026-08-12 attempt above) — should now apply cleanly.

### `ExternalAuth` filter added, OPA gate confirmed working (2026-08-14)

Added `filters: [{type: ExternalAuth, externalAuth: {backendRef: {name: opa, namespace: security, port: 9191}, protocol: GRPC, grpc: {}}}]` to all 6 app HTTPRoutes (grafana, immich, paperless-ngx, schenkmatch, zot, hubble-ui — the same set `envoy-config.yaml`'s `spec.services` covered), a `ReferenceGrant` in `security` for the 6 source namespaces, and deleted the dead `CiliumClusterwideEnvoyConfig/gateway-authz`. One correction versus the 2026-08-12 attempt's YAML: the CRD requires a `grpc: {}` stanza whenever `protocol: GRPC` (CEL validation rejects its absence), which wasn't caught before since that attempt never got past the CRD-version rejection.

**Verified live, precisely, via OPA's own decision log** (`kubectl logs -n security deploy/opa`), not just HTTP status codes:

- Unauthenticated request to `grafana.homelab.data-harness.org` → OPA decision log shows a real request with full headers, `"result":{"allowed":true}` (correct — grafana isn't in `admin_only_apps` and has no Bearer token to reject).
- Unauthenticated request to `hubble.homelab.data-harness.org` → OPA decision log shows `"result":{"allowed":false,...,"http_status":403}`, and the **client directly received OPA's own JSON body** (`{"error":"Forbidden"}`, `content-type: application/json`) — proof the response came from OPA itself, not a generic network-level block. Hubble UI's previously-documented "fully open to anyone" gap is closed.

**One caveat, not caused by this fix**: the `grafana` request that OPA allowed still doesn't reach the app — it hits the separate, pre-existing "Gateway 403 confirmed cluster-wide" issue from 2026-08-12 (still unresolved, tracked below), visible as a distinct plain-text `server: envoy` 403 with no JSON body, downstream of OPA in the same filter chain (`ext_authz → cilium.l7policy → router`). Confirmed via response headers that this is a genuinely different response than OPA's own — this bug predates today's fix and affects all Gateway traffic regardless of the OPA gate, so it isn't this fix's regression to solve, but it does mean **the two Gateway bugs together still block real end-to-end access to grafana/immich/paperless/schenkmatch/zot even though OPA is doing its job correctly now**. Only `hubble-ui`'s admin_only_apps path is *conclusively* protected end-to-end today, since OPA's own deny short-circuits before the other bug would matter.

Also out of scope for this fix, left untouched: `25-gateway-authz/valkey.yaml`/`ratelimit-config.yaml`/`ratelimit-service.yaml` — these were never wired into the deleted `envoy-config.yaml` (which used Envoy's in-process `local_ratelimit`, not this external service) and appear to be orphaned leftovers from an earlier, different rate-limiting design. Worth a separate cleanup pass, not bundled here.

## General role and access-management concept for the cluster (2026-08-12)

Prompted by a narrower ask (switch KubeOpenCode specifically to Keycloak-backed RBAC) that would have added a third, inconsistent auth pattern on top of two already in the cluster (native per-app OIDC clients for Grafana/Paperless/Immich; the stalled OPA gate above). Researched two authoritative sources instead of freehand-designing a fix: **CNCF's Identity and Access Management Whitepaper** (published 2026-06-04) and **NIST SP 800-53** (AC-2/AC-3/AC-6, the origin of the RBAC/least-privilege model), plus Kubernetes' own `rbac-good-practices` docs.

CNCF's paper defines a small set of roles — **OIDC OP** (identity issuer), **PEP** (Policy Enforcement Point, where a decision is enforced), **PDP** (Policy Decision Point, where a decision is made — explicitly recommended as **one logical instance per cluster**, not reinvented per app), and **OIDC RP** (Relying Party — does the actual browser login; either the workload itself, or a proxy/BFF in front of one that can't). It also explicitly recommends the **Basic Pattern** (perimeter-based, single implicit trust zone) over the **Advanced Pattern** (zero-trust, mTLS+SPIFFE at every workload) for anything except sensitive/public-facing/uncontrolled-user-count systems — the Advanced Pattern is real over-engineering for a single-user homelab.

Mapped onto this cluster, the architecture was **already chosen correctly** — it's just not fully wired:

| CNCF role | This cluster's answer | Status |
|---|---|---|
| OIDC OP | Keycloak (`26-keycloak`) | Working |
| OIDC RP, apps with native support | Grafana / Paperless / Immich's own OIDC clients | Working |
| PDP | OPA (`24-opa`) | Deployed, not consulted (see above) |
| PEP, perimeter | Gateway `ExternalAuth`/OPA | Working (2026-08-14) — gates all 6 apps in `spec.services`; see OPA section above for the one remaining caveat (unrelated pre-existing Gateway 403 bug) |
| OIDC RP, apps without native support (Hubble UI, KubeOpenCode) | Hubble UI: OPA's `admin_only_apps` rule (Rego-level, not real per-user OIDC) | oauth2-proxy attempt abandoned and cleaned up. KubeOpenCode still has nothing — not in OPA's `spec.services`/`admin_only_apps` list |
| Kubernetes-native RBAC (`kubectl`/`kubeoc`, the "Administrator" actor) | Cert-based only | Separate axis, not started (see below) |

**Proposed role tiers** (NIST-aligned, deliberately kept small — over-granular roles are their own maintenance/audit burden per both NIST and CNCF): reuse `admin` (already exists in the Keycloak realm, `/admin`, used by Grafana today) and add `viewer` once a second app actually needs the read-only distinction (KubeOpenCode's own `kubeopencode-viewer` ClusterRole maps directly to this).

**Kubernetes-native RBAC via Talos API server OIDC trust** (`kubectl`/`kubeoc` CLI access, not web login) is a separate, orthogonal axis from all of the above — matches the paper's own "Administrator" actor. Scoped and researched earlier the same day: requires `cluster.apiServer.extraArgs` (`oidc-issuer-url`/`oidc-client-id`/`oidc-groups-claim`) in `cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml` (currently has no OIDC config at all), applied via `talosctl apply-config` (brief kube-apiserver restart on this single control-plane node — cert-based admin access is untouched by this either way, the actual safety net). Not started.

## ADR-001: Envoy Gateway migration — cutover unblocked, root cause was a network policy on the wrong ports (2026-08-16)

> **Read this first.** Everything dated 2026-08-14 below is kept for history but its
> root-cause conclusions are **wrong**. The LoadBalancer-VIP failure was neither the
> upstream Cilium bug nor the kube-proxy conflict. See
> "Real root cause found (2026-08-16)" at the end of this section.

## ADR-001: Envoy Gateway migration — installed, cutover blocked on an unresolved upstream Cilium bug (2026-08-14)

`docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md` decided to move L7 routing off Cilium's own Gateway API implementation onto a dedicated Envoy Gateway control plane. Envoy Gateway is installed (`28-envoy-gateway/`, chart `gateway-helm@1.8.3`, kept in the cluster) but **not wired to `homelab-gateway`** — the cutover was attempted and rolled back the same session.

**What happened**: cutting `homelab-gateway` over to Envoy Gateway's `GatewayClass` reconciled cleanly on Envoy Gateway's side (Accepted, Programmed, all 8 HTTPRoutes attached) but every app became completely unreachable — not a 403, a full connection timeout. Ten hypotheses were tested and ruled out one by one before a clean 3-way comparison narrowed the actual scope: direct pod-IP traffic and the Service's `ClusterIP` both worked identically to any normal Kubernetes cluster; only the Service's **L2-announced LoadBalancer VIP** failed, as a plain timeout with zero drop or policy-verdict events. Full detail in `Agent.md`'s "Envoy Gateway migration attempt" section — earlier framing in this entry ("general Cilium policy-realization defect") was superseded by this narrower finding; that broader theory turned out to be wrong.

**Root cause found (2026-08-14, later the same day)**: matches a known, unresolved upstream Cilium bug class — [cilium/cilium#44630](https://github.com/cilium/cilium/issues/44630) and [#44187](https://github.com/cilium/cilium/issues/44187), both about LoadBalancer Services silently dropping SYN packets in the eBPF datapath (no drop events, no policy verdicts) when the L2-announce lease and the Service's backend pod land on the **same node**. Both closed without a real fix (one stale-bot-closed, one closed as a duplicate/config issue with a community workaround, not a resolution). This cluster is single-node, so any *new* LoadBalancer Service is unavoidably exposed — the lease has nowhere else to land. Cilium's own `homelab-gateway` doesn't hit it because its L7LB traffic never takes that eBPF same-node-DNAT path (Envoy owns the socket directly). **Not fixable from this repo** short of adding more nodes (the documented community workaround needs a dedicated L2-announce node pool, disjoint from backend nodes) or an eventual upstream fix.

**⚠️ Both of the above root causes are wrong — superseded 2026-08-16.** They are retained only so the reasoning trail is auditable. The upstream-bug framing (`cilium#44630`) and the kube-proxy/nftables hypothesis (security review H3) were each tested directly and each failed to explain the behaviour. Skip to "Real root cause found (2026-08-16)" below.

**The H3 hypothesis, stated and then tested (2026-08-14 → 2026-08-16)**: the security review found **kube-proxy and flannel still running** despite Cilium's `kubeProxyReplacement: True` — Talos's defaults were never disabled in the machineconfig. kube-proxy independently programs nftables DNAT for the *same* LoadBalancer Services Cilium handles in eBPF, a well-known conflict that would produce exactly the observed signature. It was a concrete, local, testable explanation, and its reconcile timestamps lined up with the test window. **It was tested on 2026-08-16 and disproven**: both DaemonSets were removed via the machineconfig and the node rebooted, after which a fresh test LoadBalancer VIP *still* timed out from an external client. H3 remains worth fixing on its own merits (two privileged host-network workloads removed), but it is not this bug.

**What does work**: `hostNetwork` + `NodePort` (the pattern the [k8rn](https://github.com/michaelbeaumont/k8rn) reference project uses) sidesteps this entirely — NodePort binds directly on the node and never takes the LoadBalancer/L2Announce path. Tried and confirmed working on an unprivileged port.

**The privileged-port blocker, root-caused (2026-08-14, later the same day)**: Talos runs pods under SELinux in the confined `pod_t` domain by default. Reproduced from scratch with plain upstream `envoyproxy/envoy` (not Cilium's fork) and a trivial static config — same `cannot bind '0.0.0.0:443': Permission denied`, confirming it's generic Envoy on this node, not anything Cilium- or chart-specific. Confirmed `CAP_NET_BIND_SERVICE` genuinely present in the container's capability set and no user-namespace remapping — ruling out capability/UID misconfiguration for good. The one thing that fixes it: `securityContext.privileged: true` (confirmed via `kubectl debug node --profile=sysadmin`, which sets exactly that). Requesting `seLinuxOptions.type: spc_t` directly (without full `privileged: true`) is silently ignored by Talos's container runtime — no surgical fix is available through the standard Kubernetes security context API here. Also confirmed this isn't a blanket restriction: plain `nc -l -p 443` succeeds under the *exact same* non-privileged context that fails for Envoy, and adding `NET_ADMIN`/`NET_RAW` (testing an `IP_TRANSPARENT` theory) made no difference — something specific to Envoy's own bind/socket-setup sequence trips a `pod_t` SELinux denial that a plain `bind()` doesn't, but the precise policy rule remains unidentified (would need an actual `strace` of Envoy's syscalls, not obtainable here — Cilium's Envoy image is distroless with no shell, and there's no easy path to attach `strace` to a container process from a Talos node). **Practical conclusion**: this path requires `privileged: true` on the whole Envoy Gateway data-plane pod — full capabilities, no seccomp, no SELinux confinement. A real security trade-off, not a minor tweak.

**Current state**: fully rolled back and verified — `homelab-gateway` back on `gatewayClassName: cilium`, LB-IPAM/L2Announce back to Cilium's own labels, Cilium's embedded Envoy re-enabled, confirmed matching pre-migration behavior. Envoy Gateway stays installed and idle so the cutover doesn't need to be redone from scratch.

**To pick this up**: the OPA `ExternalAuth` gate fix above **did not end up depending on this** — it landed via a Gateway API CRD upgrade on Cilium's own native Gateway instead, so Envoy Gateway is no longer a prerequisite for anything currently blocked. The "everything 403" issue that partly motivated ADR-001's urgency also turned out not to be a real problem (see above) — Cilium's own Gateway is confirmed working correctly for real traffic. Given that, and the `privileged: true` trade-off above, there's currently no pressing reason to pursue this cutover.

**Superseded — see below.** The step list that stood here declared the plain `LoadBalancer` path "a dead end on this cluster" and made accepting `privileged: true` the gating decision. Both premises were wrong; the corrected plan is in the next section.

### Real root cause found (2026-08-16) — a network policy on the wrong ports, not a Cilium bug

**The finding**: `allow-world-ingress` (in `28-envoy-gateway/operator/cilium-policy.yaml`) permitted ports **80/443** on the Envoy Gateway data-plane pods. Those are the Gateway *listener* ports, not the ports the pods actually listen on. Envoy Gateway defaults to `useListenerPortAsContainerPort: false`, remapping any listener port below 1024 to **port+10000** so the proxy never needs `CAP_NET_BIND_SERVICE` — listener 80 is served on **10080**, 443 on **10443**. External traffic hitting the LoadBalancer VIP is DNAT'd straight to the pod, so it arrives on 10080/10443 and the policy never matched it. Every SYN was dropped with `policy-verdict:none INGRESS DENIED`.

**Why this was so hard to see**: the policy existed, its `endpointSelector` matched the pods correctly, and it read as obviously right. The failure mode of a policy that matches the endpoint but not the port is a *silent connection timeout*, which is indistinguishable from a datapath bug unless you read the actual Hubble verdict and notice the port number in it.

The general principle behind it still holds and is worth remembering separately: external traffic to a LoadBalancer VIP is DNAT'd **directly to the backend pod**, so the backend needs `fromEntities: [world]` ingress **on its container port**. Cilium's own Gateway is exempt only because its VIP backend is `127.0.0.1:13410`, not a pod IP.

**How it was proven**, in order, against the live cluster:

1. H3 was fixed first (kube-proxy + flannel removed via machineconfig, node rebooted). A test LoadBalancer VIP still timed out — disproving the H3 hypothesis.
2. `cilium-dbg service list` showed the frontend correctly programmed with an active backend, so the eBPF LB entry was never the problem.
3. The same VIP was reachable **from inside the cluster** — proving the DNAT itself works.
4. `hubble observe` during an *external* request showed the real verdict: `192.168.178.29 (world) <> zot/zot-0:5000 policy-verdict:none INGRESS DENIED (TCP Flags: SYN)`.
5. Adding a `CiliumNetworkPolicy` with `fromEntities: [world]` on the backend changed the result from timeout to **`200` in 0.004 s** from an external LAN client — confirming the mechanism on a plain Service.
6. The mechanism was then confirmed on Envoy Gateway itself, using a throwaway `Gateway` on the `envoy-gateway` class with its own VIP so production was never touched. Its Service showed `port 80 → targetPort 10080`, and Hubble showed `(world) <> envoy-gateway-system/…envoy-test…:10080 policy-verdict:none INGRESS DENIED`. Correcting `allow-world-ingress` to 10080/10443 turned that into **`200` in 0.007 s** from an external LAN client.

**Why it looked like an unfixable datapath bug for two days**: Cilium's own `homelab-gateway` never hits this, because its VIP backend is `127.0.0.1:13410` — a TPROXY handoff into the node-local Envoy socket, not a pod IP. That is a genuinely different datapath which bypasses pod ingress policy entirely, which is why the existing Gateway works while any *new* LoadBalancer Service does not. On top of that, the Envoy policy that *did* exist matched the right pods with the wrong ports, so nothing in the manifests looked wrong on inspection.

**Testing gotcha that cost real time — read before repeating this test**: verify the test VIP is genuinely unused. `192.168.178.201` and `.202` are already held by a LAN device with MAC `b4:fc:7d:71:4f:f9`; ARP resolved to *that host*, packets never reached the node, and the symptom was indistinguishable from a datapath bug. The node's `enp2s0` MAC is `6c:3c:8c:04:34:b0` — confirm ARP resolves to it before concluding anything. Free at time of writing: `.203`–`.206`, `.210`–`.212`, `.215`, `.220`–`.222`.

**What this changes**:

- The `LoadBalancer` path is **viable**. `hostNetwork` + `NodePort` is not needed.
- Therefore `securityContext.privileged: true` is **not needed**, and `envoy-gateway-system` stays at PSS `restricted`.
- The Talos SELinux privileged-port finding above is still accurate, but only ever applied to the `hostNetwork` path, which is no longer on the table.
- Adding nodes is not required.

**Corrected steps for the cutover**:

1. ~~Add a `CiliumNetworkPolicy`…~~ **Done 2026-08-16**: `allow-world-ingress` corrected from ports 80/443 to the real container ports 10080/10443. **This one line is what caused the original failure.** Verified live.
2. `EnvoyProxy` (`28-envoy-gateway/config/envoyproxy.yaml`): keep `envoyService.type: LoadBalancer` and `externalTrafficPolicy: Cluster`. Single replica — the 2-replica + `podAntiAffinity` shape from the draft ADRs cannot schedule on one node.
3. Label the Envoy Gateway Service so the existing `gateway-pool` `CiliumLoadBalancerIPPool` and `CiliumL2AnnouncementPolicy` select it, so it inherits `192.168.178.200` and `external-dns` keeps publishing unchanged records.
4. Cut `Gateway.spec.gatewayClassName` from `cilium` to `envoy-gateway`. All 9 `HTTPRoute`s are portable and need no changes.
5. Migrate the OPA gate from the HTTPRoute `ExternalAuth` filter to Envoy Gateway's native `SecurityPolicy.extAuth`, and add `SecurityPolicy.oidc` for Keycloak — edge OIDC is the actual reason to run Envoy Gateway, since it closes the Hubble UI / KubeOpenCode gap that Cilium's Gateway cannot.
6. Keep the rollback ready before starting: revert `gatewayClassName`, and the LB-IPAM/L2Announce labels back to Cilium's own.

Note on `Gateway.spec.addresses`: the earlier step list pinned `192.168.178.100`, which is correct only for the abandoned NodePort path — `.100` is the **node's own LAN IP**. On the LoadBalancer path the VIP stays `192.168.178.200`, which is also what the Tailscale subnet router advertises (`TS_ROUTES=192.168.178.200/32`). Changing it would require updating the router too.
