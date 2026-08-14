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

1. **Native `ExternalAuth` HTTPRoute filter (GEP-1494)** — the actual correct fix, identified 2026-08-12 (see dated section below). Required a Gateway API CRD upgrade first (standard v1.2.1 → experimental v1.4.1); **done 2026-08-14** (see dated section below). This is the recommended path now — it replaces the whole `25-gateway-authz/envoy-config.yaml` redirect mechanism, which cannot work and never could (see above). Redoing the filter addition itself is the next concrete step.
2. **oauth2-proxy in front of Hubble UI** — attempted 2026-08-12, blocked (see below). Standard pattern for an app with no native OIDC: a proxy pod does real browser-based OIDC login against Keycloak, then forwards to hubble-ui. Sidesteps the EDS/ClusterIP mismatch entirely since it's ordinary Gateway→backend routing to a pod that enforces its own auth. Probably superseded by option 1 — no need to retry this once the CRD upgrade is done.
3. **Network-level restriction** — lock `hubble.homelab.data-harness.org` down via Cilium L3/L4 CiliumNetworkPolicy (source IP/namespace) instead of OIDC. Simpler, less capable. Not attempted.

The Rego policy logic itself was already fixed and verified correct (2026-08-12) — don't re-debug it. The problem is purely that traffic never reaches OPA's decision.

### oauth2-proxy attempt (2026-08-12) — abandoned and cleaned up

Built the full stack: a `hubble` Keycloak client (confidential, no PKCE — oauth2-proxy doesn't send `code_challenge`), an oauth2-proxy Deployment/Service/ConfigMap/Secret in `kube-system` (`23-hubble-auth`, never committed), CiliumNetworkPolicies for gateway ingress and Keycloak egress, and an HTTPRoute update pointing `hubble.homelab.data-harness.org` at the new `hubble-auth` service.

Login itself worked end to end — Keycloak authenticated the user, oauth2-proxy logged `[AuthSuccess]` with the correct `groups:[admin]` claim. But the **final hop, oauth2-proxy proxying to hubble-ui (same namespace, same as its own upstream)**, returned Cilium's generic `403 Access denied` — even with a `CiliumNetworkPolicy` `fromEndpoints` rule that label-matched exactly, and even after additionally adding the broader `allow-intra-namespace-ingress` fix that resolved the identical-looking problem for Keycloak earlier the same day. `allowed-ingress-identities` never grew beyond the fixed reserved set `[1,3,4,5,6,7,8,11]` — regular pod identities never appeared there regardless of which policy was added. **In hindsight, this was almost certainly an early manifestation of the general Cilium policy-realization bug documented in the ADR-001 section below** — the identical symptom (reserved-only `allowed-ingress-identities`, regardless of policy) was independently rediscovered and much more thoroughly isolated on 2026-08-14.

**Cleaned up**: the oauth2-proxy Deployment/Service/ConfigMap/Secret, its CiliumNetworkPolicies, and the Keycloak `hubble` client were all removed from the live cluster; the HTTPRoute was reverted to point at `hubble-ui` directly (restoring the old fully-open-but-functional state). Nothing from this attempt remains — Hubble UI is reachable but has no auth of its own, which is `OPA ext_authz gate is inert`'s practical impact above.

### Gateway 403 confirmed cluster-wide, unrelated to any specific app (2026-08-12)

While deploying KubeOpenCode (separate PR), its HTTPRoute hit the same `403 Access denied` documented above. Ran the actual documented diagnostic this time (Envoy debug logging + NPDS dump — see `Agent.md`) instead of guessing at policies, and confirmed: this is **not** Tailscale-specific (reproduced with a pure in-cluster pod → Gateway-IP request), currently affects `monitoring`/grafana too (previously the one namespace that worked), and is **not caused by the kubeopencode work** — verified by reverting the (uncommitted) Gateway edit and confirming the 403 persists with the Gateway in its exact prior state. A promising lead (missing ipcache entry for the Gateway's L2-announced IP) was tested via a Cilium agent restart and ruled out as the sole cause. Full diagnostic detail and next steps in `Agent.md`'s "Follow-up (2026-08-12, still unresolved)" section — the concrete next lead is cross-referencing the `cil_from_netdev`/`bpf_metadata` direction-misclassification signature against upstream Cilium GitHub issues, since it looks like a known bug class rather than something specific to this cluster's config.

**Practical impact**: every app behind `homelab-gateway` is currently unreachable through it (403), regardless of source namespace. This blocks external access to grafana, paperless, immich, and now kubeopencode — all still fully functional over their internal cluster-only paths.

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
| OIDC RP, apps without native support (Hubble UI, KubeOpenCode) | Nothing working | oauth2-proxy attempt abandoned and cleaned up; superseded by the OPA fix once unblocked |
| Kubernetes-native RBAC (`kubectl`/`kubeoc`, the "Administrator" actor) | Cert-based only | Separate axis, not started (see below) |

**Proposed role tiers** (NIST-aligned, deliberately kept small — over-granular roles are their own maintenance/audit burden per both NIST and CNCF): reuse `admin` (already exists in the Keycloak realm, `/admin`, used by Grafana today) and add `viewer` once a second app actually needs the read-only distinction (KubeOpenCode's own `kubeopencode-viewer` ClusterRole maps directly to this).

**Kubernetes-native RBAC via Talos API server OIDC trust** (`kubectl`/`kubeoc` CLI access, not web login) is a separate, orthogonal axis from all of the above — matches the paper's own "Administrator" actor. Scoped and researched earlier the same day: requires `cluster.apiServer.extraArgs` (`oidc-issuer-url`/`oidc-client-id`/`oidc-groups-claim`) in `cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml` (currently has no OIDC config at all), applied via `talosctl apply-config` (brief kube-apiserver restart on this single control-plane node — cert-based admin access is untouched by this either way, the actual safety net). Not started.

## ADR-001: Envoy Gateway migration — installed, cutover blocked on a deep Cilium bug (2026-08-14)

`docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md` decided to move L7 routing off Cilium's own Gateway API implementation onto a dedicated Envoy Gateway control plane. Envoy Gateway is installed (`28-envoy-gateway/`, chart `gateway-helm@1.8.3`, kept in the cluster) but **not wired to `homelab-gateway`** — the cutover was attempted and rolled back the same session.

**What happened**: cutting `homelab-gateway` over to Envoy Gateway's `GatewayClass` reconciled cleanly on Envoy Gateway's side (Accepted, Programmed, all 8 HTTPRoutes attached) but every app became completely unreachable — not a 403, a full connection timeout. Ten different hypotheses were tested and ruled out one by one (LB-IPAM label semantics, L2 announcement propagation, ARP, entity vs label policy rules, egress vs ingress, Cilium's embedded Envoy, even deleting the cluster's `default-deny-ingress` baseline entirely) — full detail in `Agent.md`'s "Envoy Gateway migration attempt" section.

**The actual finding is bigger than this migration**: the failure reproduces on **ordinary pod-to-pod traffic with zero Gateway API involvement** — a source pod's own workload identity never gets recognized by an ordinary `CiliumNetworkPolicy`, regardless of policy syntax or direction, and this reproduces identically even for a documented-working pod (Keycloak). This looks like a general Cilium policy-realization defect on this cluster, not something specific to Gateway API or to this migration — which also means it likely explains some of the earlier "mystery 403" investigations above (the same-namespace hubble-auth→hubble-ui case in particular).

**Current state**: fully rolled back and verified — `homelab-gateway` back on `gatewayClassName: cilium`, LB-IPAM/L2Announce back to Cilium's own labels, Cilium's embedded Envoy re-enabled. Confirmed working (matches pre-migration behavior). Envoy Gateway stays installed and idle so the cutover doesn't need to be redone from scratch.

**To pick this up**: `Agent.md` has concrete next diagnostic steps (cleanest-possible minimal reproduction with zero cluster-wide policy, testing whether `kubeProxyReplacement`/socket-LB is a variable, searching Cilium's GitHub issues for the exact drop signature, version bisection). This is now a prerequisite for the OPA `ExternalAuth` fix too, if that path is chosen — Envoy Gateway's own native `SecurityPolicy` (OIDC + ext_authz) would also solve the OPA gate problem directly, without needing the Gateway API CRD upgrade described above, once the underlying Cilium issue is resolved.
