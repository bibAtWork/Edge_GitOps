# Backlog

Known open issues that aren't yet fixed. Not a full project backlog — just things worth not forgetting.

---

## OPA `ext_authz` gate is inert for every app behind the Gateway

**Status:** Root cause confirmed, not fixed.

`25-gateway-authz/envoy-config.yaml`'s `CiliumClusterwideEnvoyConfig` gates traffic by having Cilium's eBPF datapath redirect connections **destined to a Service's ClusterIP** to a custom Envoy listener (`gateway-authz`) that runs `local_ratelimit → ext_authz(OPA) → router` before forwarding on.

Cilium's own Gateway API implementation does **not** route through Service ClusterIPs — it resolves backends via EDS directly to pod IPs. Confirmed via `cilium-dbg envoy admin clusters`: the Gateway's per-backend clusters use `eds_service_name` resolving straight to pod IPs, never the ClusterIP. Since the eBPF redirect only triggers on "destination IP is a known Service ClusterIP," it never fires for Gateway-routed traffic.

**Affects every app in `spec.services`**: grafana, immich, paperless, schenkmatch, zot, hubble-ui. Confirmed empirically (invalid Bearer token against Grafana got a normal 302, not the expected 401; an unconditional `allow := false` policy broke nothing).

**Practical impact**: apps with native login (Grafana, Paperless, Immich) are unaffected in practice — OPA was defense-in-depth for them. **Hubble UI has no native auth and is currently fully open** to anyone who can reach `hubble.homelab.data-harness.org`.

**Fix options**:

1. **oauth2-proxy in front of Hubble UI** — attempted 2026-08-12, blocked (see below). Standard pattern for an app with no native OIDC: a proxy pod does real browser-based OIDC login against Keycloak, then forwards to hubble-ui. Sidesteps the EDS/ClusterIP mismatch entirely since it's ordinary Gateway→backend routing to a pod that enforces its own auth.
2. **Network-level restriction** — lock `hubble.homelab.data-harness.org` down via Cilium L3/L4 CiliumNetworkPolicy (source IP/namespace) instead of OIDC. Simpler, less capable. Not attempted.

The Rego policy logic itself was already fixed and verified correct (2026-08-12) — don't re-debug it. The problem is purely that traffic never reaches OPA's decision.

### oauth2-proxy attempt (2026-08-12) — blocked by a new variant of the same underlying Cilium issue

Built the full stack: a `hubble` Keycloak client (confidential, no PKCE — oauth2-proxy doesn't send `code_challenge`), an oauth2-proxy Deployment/Service/ConfigMap/Secret in `kube-system` (`23-hubble-auth`, not committed), CiliumNetworkPolicies for gateway ingress and Keycloak egress, and an HTTPRoute update pointing `hubble.homelab.data-harness.org` at the new `hubble-auth` service.

Login itself worked end to end — Keycloak authenticated the user, oauth2-proxy logged `[AuthSuccess]` with the correct `groups:[admin]` claim. But the **final hop, oauth2-proxy proxying to hubble-ui (same namespace, same as its own upstream)**, returns Cilium's generic `403 Access denied` (the same `server: envoy` / plain-text signature seen throughout the namespace-connectivity investigation above) — even with a `CiliumNetworkPolicy` `fromEndpoints` rule that label-matches exactly, and even after additionally adding the broader `allow-intra-namespace-ingress` fix that resolved the identical-looking problem for Keycloak earlier the same day. `cilium-dbg endpoint get` on hubble-ui's endpoint confirms the new rule is present in the realized policy (`rules-by-selector` shows it) but `allowed-ingress-identities` never grows beyond the fixed reserved set `[1,3,4,5,6,7,8,11]` — regular pod identities (hubble-auth's included) never appear there regardless of which policy is added. Root cause not found; ruled out policy revision propagation lag (checked `build` revision incremented) and label mismatches (labels confirmed correct via `kubectl get pods --show-labels` and cross-checked against the realized policy dump).

**Notably different from the earlier cross-namespace mystery**: this is same-namespace (`kube-system` → `kube-system`), tried both a narrow `fromEndpoints` rule and a broad empty-selector `allow-intra-namespace-ingress`, and neither worked — whereas the identical broad-selector fix *did* work for `keycloak` namespace's cross-namespace case (OPA → Keycloak) earlier that day. Something about `hubble-ui`'s specific endpoint (it's deployed via Cilium's own Helm chart, not a hand-written manifest like everything else this fix touched) may be involved, but this is speculation, not confirmed.

**Live cluster state as of 2026-08-12**: the oauth2-proxy pod, Keycloak client, and network policies are still deployed and running (nothing was torn down), but the HTTPRoute still points at `hubble-auth` — meaning `hubble.homelab.data-harness.org` currently returns the login flow correctly but then 500s/403s after auth, rather than either the old (fully open) or new (working) behavior. If picking this up cold, either finish debugging the connectivity (needs packet capture or a Cilium GitHub issue, not more kubectl/cilium-dbg guessing) or revert the HTTPRoute to point at `hubble-ui` directly (restores the old fully-open-but-at-least-functional state) while deciding on a real fix.

**Untried alternative worth considering next**: run oauth2-proxy as a sidecar container inside hubble-ui's own pod (localhost communication, same network namespace, completely bypasses Cilium's pod-to-pod policy enforcement). Bigger structural change since hubble-ui is deployed via Cilium's Helm chart, not something this repo directly controls the pod spec for — would need either patching Cilium's HelmRelease values (if the chart supports injecting extra containers, uncertain) or forking hubble-ui into a repo-owned Deployment.

### Gateway 403 confirmed cluster-wide, unrelated to any specific app (2026-08-12)

While deploying KubeOpenCode (separate PR), its HTTPRoute hit the same `403 Access denied` documented above. Ran the actual documented diagnostic this time (Envoy debug logging + NPDS dump — see `Agent.md`) instead of guessing at policies, and confirmed: this is **not** Tailscale-specific (reproduced with a pure in-cluster pod → Gateway-IP request), currently affects `monitoring`/grafana too (previously the one namespace that worked), and is **not caused by the kubeopencode work** — verified by reverting the (uncommitted) Gateway edit and confirming the 403 persists with the Gateway in its exact prior state. A promising lead (missing ipcache entry for the Gateway's L2-announced IP) was tested via a Cilium agent restart and ruled out as the sole cause. Full diagnostic detail and next steps in `Agent.md`'s "Follow-up (2026-08-12, still unresolved)" section — the concrete next lead is cross-referencing the `cil_from_netdev`/`bpf_metadata` direction-misclassification signature against upstream Cilium GitHub issues, since it looks like a known bug class rather than something specific to this cluster's config.

**Practical impact**: every app behind `homelab-gateway` is currently unreachable through it (403), regardless of source namespace. This blocks external access to grafana, paperless, immich, and now kubeopencode — all still fully functional over their internal cluster-only paths.
