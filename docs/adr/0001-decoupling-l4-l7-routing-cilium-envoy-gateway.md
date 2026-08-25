# ADR-001: Decoupling L4 and L7 Routing with Cilium and Envoy Gateway

**Date:** 2026-08-13 (proposed) · 2026-08-16 (accepted and implemented)
**Status:** Accepted — implemented and live

## Context

The current Talos OS-based Kubernetes environment requires an ingress and routing architecture capable of handling strict security, identity management, and comprehensive observability. Specifically, the architecture must support robust Layer 7 capabilities, including OIDC authentication via Keycloak, authorization via Open Policy Agent (OPA), and (eventually) advanced rate limiting.

While standardizing on a single unified controller for all network layers is operationally appealing, evaluating the L7 capabilities of Cilium's own embedded Gateway API implementation revealed a concrete limitation, not a hypothetical one: Cilium's Gateway had no way to attach `ext_authz` (or any arbitrary Envoy filter) to Gateway-routed traffic at all. This was worked around temporarily via the Gateway API's own `ExternalAuth` HTTPRoute filter (GEP-1494, Experimental channel, shipped in Cilium 1.20.0) — but that filter is not portable: as this ADR's own implementation found, Envoy Gateway does not support it either. The real motivation for decoupling was never "Cilium can't do L7 well"; it was that OPA-based authorization needed a stable home independent of whichever Gateway implementation was in use.

## Decision

Implemented a decoupled network architecture using the Kubernetes Gateway API:

- **Layer 4 (Data Plane & CNI):** Cilium provides eBPF-driven networking, kube-proxy replacement, and default-deny `CiliumNetworkPolicy`/`CiliumClusterwideNetworkPolicy` enforcement everywhere, including inside `envoy-gateway-system`.
- **Layer 7 (Ingress & API Gateway):** Envoy Gateway manages the `homelab-gateway` `Gateway` resource and its data-plane Envoy fleet for all L7 HTTP routing and authorization.

Two structural choices, made during implementation rather than at proposal time:

- **`SecurityPolicy.extAuth` attaches to the `Gateway`, not to each `HTTPRoute`.** The original plan (and the two later "AI Agent Implementation Specification" drafts circulated for this decision, `ADR-002`/`ADR-005`, both dated 2026-08-16 and not adopted — see "Alternatives Considered") assumed per-route `SecurityPolicy` objects. Attaching once at the Gateway means OPA covers every route the moment it attaches, with no per-app policy to remember to add.
- **No `hostNetwork`, no `securityContext.privileged: true`.** `envoy-gateway-system` remains at Pod Security Standards `restricted`. This was not the obvious outcome — see "Path to this decision" below.

## Path to this decision — what actually blocked and unblocked the cutover

The first cutover attempt (2026-08-14) reconciled cleanly on Envoy Gateway's side but left every app unreachable — a silent connection timeout, not an error. Two root causes were proposed and both were wrong:

1. An unresolved upstream Cilium bug ([cilium/cilium#44630](https://github.com/cilium/cilium/issues/44630)) affecting same-node LoadBalancer VIPs, believed unfixable without adding nodes.
2. A conflict between Cilium's `kubeProxyReplacement` and Talos's still-running default `kube-proxy`/`flannel` DaemonSets (since fixed regardless, as good practice, but disproven as the cause of this failure — see the 3rd bullet below).

Believing (1), the plan on record as of 2026-08-14 was `hostNetwork` + `NodePort`, which is the only path that sidesteps a LoadBalancer VIP entirely — at the cost of requiring `securityContext.privileged: true`, because Talos's SELinux `pod_t` confinement denies Envoy's bind on a privileged port without it. That would have meant dropping `envoy-gateway-system` out of PSS `restricted`.

Neither theory was correct. Investigation on 2026-08-16 found the actual cause: the `CiliumNetworkPolicy` gating ingress to the Envoy Gateway data-plane pods (`allow-world-ingress`) allowed ports **80/443** — the Gateway's *listener* ports. Envoy Gateway defaults to `useListenerPortAsContainerPort: false`, serving listener 80 on **container port 10080** and 443 on **10443**, specifically so the proxy never needs `CAP_NET_BIND_SERVICE`. A LoadBalancer VIP DNATs directly to the pod, so traffic arrived on 10080/10443 and was silently dropped by a policy that never matched. Confirmed live: correcting the policy's ports took a test VIP from timeout to a `200` response in 7ms, with no other change.

This meant the `LoadBalancer` path was viable all along, on a single node, with no privileged pod and no SELinux workaround needed. Full investigation trail — including the eliminated theories and the live verification steps — is preserved in [`docs/backlog.md`](../backlog.md).

## Consequences

**Positive, confirmed live (2026-08-16):**

- **Authorization decoupled from the Gateway implementation.** OPA's decision log confirms `ext_authz` runs for every request — allowed apps show `"allowed":true`, `admin_only_apps` show `"allowed":false` with OPA's own `{"error":"Forbidden"}` body reaching the client verbatim, not a generic network-level block.
- **Client IP visibility.** Envoy Gateway populates `x-forwarded-for` / `x-envoy-external-address` with the real client address; Cilium's embedded Gateway did not surface this.
- **Decoupled lifecycle, proven, not just claimed.** The cutover replaced the Gateway implementation under `homelab-gateway` with zero DNS change and zero downtime once executed, retaining the VIP (`192.168.178.200`) throughout. The mechanism that made this work — and the ordering constraint that made it safe rather than lucky — is operational detail, not a decision; see `Agent.md`, "Envoy Gateway cutover (2026-08-16) — two mechanisms that made it safe, not obvious from the manifests".
- **No security posture regression.** `envoy-gateway-system` stayed at PSS `restricted` throughout — the `privileged: true` trade-off considered during the blocked period was never actually needed.

**Enabled by the decision, and since built:**

The original proposal scoped four capabilities this split makes possible but which
did not exist at the time of the cutover. All four are now in place; they are
listed because they are what the decision was *for*, not as a status report.

- **OpenTelemetry trace injection at the edge**, via `EnvoyProxy.telemetry.tracing`
  pointed at the OTel collector already feeding VictoriaTraces.
- **Rate limiting**, via `BackendTrafficPolicy` on the Gateway — a first-class API
  object, where the pre-cutover design needed a hand-wired Envoy filter and a
  separate Valkey backend.
- **Observability of the Envoy fleet**, via scrape configs for both the control
  and data plane plus the upstream envoy-mixin dashboards. Hubble covers the L4
  data plane; this is the L7 view it cannot give.
- **Edge OIDC**, via `SecurityPolicy.oidc` against Keycloak for Hubble UI and
  KubeOpenCode — real per-user login, replacing OPA's coarse allow/deny.

Each of these is an object the Gateway API defines and Cilium's embedded Envoy did
not expose. That is the substance of the decision rather than a footnote to it.

**Negative:**

- **Increased resource utilization.** A dedicated Envoy Gateway deployment runs alongside Cilium's own embedded Envoy (used internally for L7 policy enforcement elsewhere in the cluster), rather than a single shared proxy.
- **Two more objects per app to reason about.** `SecurityPolicy` and (eventually) `BackendTrafficPolicy` are new CRDs a reader must know about, on top of `HTTPRoute` and `Gateway`.
- **The container-port remapping is a sharp edge.** Any future `CiliumNetworkPolicy` touching Envoy Gateway's data-plane pods must target 10080/10443, not the Gateway's listener ports — this is exactly what caused the two-day-long misdiagnosed outage above, and nothing in the Gateway API surfaces the remapping to make the mistake obvious.

## Alternatives Considered

- **Cilium for All (L4 + L7):** This was the status quo before 2026-08-16, and it worked — Cilium's embedded Gateway correctly served all 8 apps for 34 days. It was replaced specifically because Cilium's Gateway has no `ext_authz`/arbitrary-filter attachment point, forcing OPA integration through a brittle Experimental-channel HTTPRoute filter (`ExternalAuth`, GEP-1494) that later proved non-portable — Envoy Gateway itself rejects that same filter. Cilium's Gateway remains viable for L4/L7 in general; the specific gap that justified moving off it was the authorization attachment point, not raw routing correctness or performance.
- **Dark Cluster Architecture (Kube-VIP) / Dual-Path Hybrid Ingress (Cloudflare Tunnels)** — two fully-specified "AI Agent Implementation Specification" documents were circulated on 2026-08-16 proposing Kube-VIP for L4 IPAM (replacing Cilium's own `CiliumLoadBalancerIPPool`/`CiliumL2AnnouncementPolicy`), disabling `kubeProxyReplacement`, adding SPIRE mutual-TLS, and — in the second variant — a public Cloudflare Tunnel. Both were evaluated and rejected before implementation:
  - Kube-VIP would duplicate LB/ARP functionality this cluster's Cilium config already provides, and its `hostNetwork`+`NET_ADMIN`/`NET_RAW` requirements are the same class of privileged workload this ADR's actual implementation specifically avoided.
  - Disabling `kubeProxyReplacement` would have broken the Cilium `k8sServiceHost`/`k8sServicePort` (KubePrism) addressing and `egressGateway`, both of which depend on it being enabled.
  - SPIRE mTLS is explicitly not recommended for a single-user, non-public-facing homelab by the CNCF IAM whitepaper's own "Basic Pattern" guidance, and was already disabled in the live Cilium `HelmRelease` for an unrelated reason (StatefulSet startup races on a single-node cluster).
  - The Cloudflare Tunnel variant was rejected outright: it would expose Immich (personal photos) and Paperless (personal documents) to the public internet, terminating TLS at a third party, for capability the existing Tailscale subnet router already provides without any public exposure.
