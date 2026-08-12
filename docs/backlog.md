# Backlog

Known open issues that aren't yet fixed. Not a full project backlog — just things worth not forgetting.

---

## OPA `ext_authz` gate is inert for every app behind the Gateway

**Status:** Root cause confirmed, not fixed.

`25-gateway-authz/envoy-config.yaml`'s `CiliumClusterwideEnvoyConfig` gates traffic by having Cilium's eBPF datapath redirect connections **destined to a Service's ClusterIP** to a custom Envoy listener (`gateway-authz`) that runs `local_ratelimit → ext_authz(OPA) → router` before forwarding on.

Cilium's own Gateway API implementation does **not** route through Service ClusterIPs — it resolves backends via EDS directly to pod IPs. Confirmed via `cilium-dbg envoy admin clusters`: the Gateway's per-backend clusters use `eds_service_name` resolving straight to pod IPs, never the ClusterIP. Since the eBPF redirect only triggers on "destination IP is a known Service ClusterIP," it never fires for Gateway-routed traffic.

**Affects every app in `spec.services`**: grafana, immich, paperless, schenkmatch, zot, hubble-ui. Confirmed empirically (invalid Bearer token against Grafana got a normal 302, not the expected 401; an unconditional `allow := false` policy broke nothing).

**Practical impact**: apps with native login (Grafana, Paperless, Immich) are unaffected in practice — OPA was defense-in-depth for them. **Hubble UI has no native auth and is currently fully open** to anyone who can reach `hubble.homelab.data-harness.org`.

**Fix options** (not yet implemented):
1. **oauth2-proxy in front of Hubble UI** (recommended) — standard pattern for an app with no native OIDC. A proxy pod does real browser-based OIDC login against Keycloak, then forwards to hubble-ui. Sidesteps the EDS/ClusterIP mismatch entirely since it's ordinary Gateway→backend routing to a pod that enforces its own auth.
2. **Network-level restriction** — lock `hubble.homelab.data-harness.org` down via Cilium L3/L4 CiliumNetworkPolicy (source IP/namespace) instead of OIDC. Simpler, less capable.

The Rego policy logic itself was already fixed and verified correct (2026-08-12) — don't re-debug it. The problem is purely that traffic never reaches OPA's decision.
