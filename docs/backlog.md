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

1. **Native `ExternalAuth` HTTPRoute filter (GEP-1494)** — the actual correct fix, identified 2026-08-12 (see dated section below). Requires a Gateway API CRD upgrade first (standard v1.2.1 → experimental v1.4.1+); not yet done. This is the recommended path once that upgrade happens — it replaces the whole `25-gateway-authz/envoy-config.yaml` redirect mechanism, which cannot work and never could (see above).
2. **oauth2-proxy in front of Hubble UI** — attempted 2026-08-12, blocked (see below). Standard pattern for an app with no native OIDC: a proxy pod does real browser-based OIDC login against Keycloak, then forwards to hubble-ui. Sidesteps the EDS/ClusterIP mismatch entirely since it's ordinary Gateway→backend routing to a pod that enforces its own auth. Probably superseded by option 1 — no need to retry this once the CRD upgrade is done.
3. **Network-level restriction** — lock `hubble.homelab.data-harness.org` down via Cilium L3/L4 CiliumNetworkPolicy (source IP/namespace) instead of OIDC. Simpler, less capable. Not attempted.

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

### The real fix for the OPA gate found, blocked on a Gateway API CRD upgrade (2026-08-12)

Researched rather than guessed this time (see the general IAM concept section below for the full context that prompted this). Confirmed via web research: Cilium's Gateway API implementation had no way to attach `ext_authz`/arbitrary Envoy filters to Gateway-routed traffic at all — a known upstream gap ([cilium/cilium#45704](https://github.com/cilium/cilium/issues/45704), Gateway API's own `ExternalAuth` HTTPRoute filter, GEP-1494). This merged and shipped in **Cilium 1.20.0**, which is the exact version already running in this cluster.

Implemented it — added `filters: [{type: ExternalAuth, externalAuth: {protocol: GRPC, backendRef: {name: opa, namespace: security, port: 9191}}}]` to all 7 app HTTPRoutes, a `ReferenceGrant` for the cross-namespace `backendRef`, an ingress `CiliumNetworkPolicy` for OPA, and removed the dead `25-gateway-authz/envoy-config.yaml` CiliumClusterwideEnvoyConfig. **All 7 `kubectl apply`s were rejected**: `strict decoding error: unknown field "spec.rules[0].filters[0].externalAuth"`.

Root cause: `ExternalAuth` is an **Experimental**-channel Gateway API field, only present in the CRD schema from **Gateway API v1.4.0 onward**. This cluster's installed CRDs (`00-gateway-api/standard-install.yaml`) are **standard channel, v1.2.1** — two gaps at once (too old, and the standard channel never carries Experimental fields regardless of version). Confirmed Gateway API **v1.4.1 experimental channel** is compatible with the installed Cilium 1.20.

**This is a bigger, riskier change than the app-level fix itself** — it's the foundational CRDs every Gateway/HTTPRoute/ReferenceGrant in the cluster depends on, not something scoped to OPA or any one app. Asked before proceeding; decision was to stop here rather than do the CRD upgrade in the same pass. **All changes were fully reverted** (git working tree and live cluster both confirmed back to original state — the old broken `envoy-config.yaml` CCEC was restored, OPA's ConfigMap reverted, the `ReferenceGrant` and new CiliumNetworkPolicy deleted). Nothing is left half-applied.

**To pick this up**: upgrade `00-gateway-api/standard-install.yaml` from the standard v1.2.1 bundle to the experimental v1.4.1 bundle (`https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.1/experimental-install.yaml`), verify the existing `Gateway` and all `HTTPRoute`/`ReferenceGrant` objects remain `Accepted` afterward, then redo the `ExternalAuth` filter addition described above (the exact YAML is known-correct, only the CRD version was the blocker).

## General role and access-management concept for the cluster (2026-08-12)

Prompted by a narrower ask (switch KubeOpenCode specifically to Keycloak-backed RBAC) that would have added a third, inconsistent auth pattern on top of two already in the cluster (native per-app OIDC clients for Grafana/Paperless/Immich; the stalled OPA gate above). Researched two authoritative sources instead of freehand-designing a fix: **CNCF's Identity and Access Management Whitepaper** (published 2026-06-04) and **NIST SP 800-53** (AC-2/AC-3/AC-6, the origin of the RBAC/least-privilege model), plus Kubernetes' own `rbac-good-practices` docs.

CNCF's paper defines a small set of roles — **OIDC OP** (identity issuer), **PEP** (Policy Enforcement Point, where a decision is enforced), **PDP** (Policy Decision Point, where a decision is made — explicitly recommended as **one logical instance per cluster**, not reinvented per app), and **OIDC RP** (Relying Party — does the actual browser login; either the workload itself, or a proxy/BFF in front of one that can't). It also explicitly recommends the **Basic Pattern** (perimeter-based, single implicit trust zone) over the **Advanced Pattern** (zero-trust, mTLS+SPIFFE at every workload) for anything except sensitive/public-facing/uncontrolled-user-count systems — the Advanced Pattern is real over-engineering for a single-user homelab.

Mapped onto this cluster, the architecture was **already chosen correctly** — it's just not fully wired:

| CNCF role | This cluster's answer | Status |
|---|---|---|
| OIDC OP | Keycloak (`26-keycloak`) | Working |
| OIDC RP, apps with native support | Grafana / Paperless / Immich's own OIDC clients | Working |
| PDP | OPA (`24-opa`) | Deployed, not consulted (see above) |
| PEP, perimeter | Gateway `ExternalAuth`/OPA | Blocked on the CRD upgrade above |
| OIDC RP, apps without native support (Hubble UI, KubeOpenCode) | Nothing working | oauth2-proxy attempt blocked, probably superseded by the OPA fix once unblocked |
| Kubernetes-native RBAC (`kubectl`/`kubeoc`, the "Administrator" actor) | Cert-based only | Separate axis, not started (see below) |

**Proposed role tiers** (NIST-aligned, deliberately kept small — over-granular roles are their own maintenance/audit burden per both NIST and CNCF): reuse `admin` (already exists in the Keycloak realm, `/admin`, used by Grafana today) and add `viewer` once a second app actually needs the read-only distinction (KubeOpenCode's own `kubeopencode-viewer` ClusterRole maps directly to this).

**Kubernetes-native RBAC via Talos API server OIDC trust** (`kubectl`/`kubeoc` CLI access, not web login) is a separate, orthogonal axis from all of the above — matches the paper's own "Administrator" actor. Scoped and researched earlier the same day: requires `cluster.apiServer.extraArgs` (`oidc-issuer-url`/`oidc-client-id`/`oidc-groups-claim`) in `cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml` (currently has no OIDC config at all), applied via `talosctl apply-config` (brief kube-apiserver restart on this single control-plane node — cert-based admin access is untouched by this either way, the actual safety net). Not started.
