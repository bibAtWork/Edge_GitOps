# Network architecture

Live topology of the homelab cluster's ingress, authorization, and East-West traffic
paths, as of the Envoy Gateway cutover (2026-08-16, [ADR-001](adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md),
[#126](https://github.com/bibAtWork/Edge_GitOps/pull/126)). Diagrams are generated from
the manifests in `cluster/base/infrastructure/` and cross-checked against the running
cluster — not hand-drawn intent.

This is a living document. If a diagram and the manifests disagree, the manifests are
correct — open a PR to fix the diagram.

## 1. Ingress topology — how a request reaches a backend

```mermaid
flowchart TB
    subgraph external["External clients"]
        lan["LAN client\n192.168.178.0/24"]
        tailnet["Tailscale client\n(any device on the tailnet)"]
    end

    subgraph node["Talos node — talos-1ps-0l8 (single control-plane)"]
        subgraph l2["Cilium L2 announcement"]
            vip["VIP 192.168.178.200\nARP owner: this node\nLease: cilium-l2announce-...homelab-gateway\ngateway-pool (CiliumLoadBalancerIPPool)\ngateway-announce (CiliumL2AnnouncementPolicy)"]
        end

        subgraph ts["tailscale namespace"]
            subnetrouter["tailscaled-subnet-router\nStatefulSet, hostNetwork\nkernel mode, advertises 192.168.178.200/32\nCilium TC hook on tailscale0"]
        end

        subgraph egw["envoy-gateway-system (PSS: restricted)"]
            svc["Service: LoadBalancer\nexternalTrafficPolicy: Cluster\n443→10443, 80→10080"]
            proxy["envoy-gateway-system-homelab-gateway pod\nreplicas: 1\nno hostNetwork, no privileged\nlistens on 10080/10443\n(useListenerPortAsContainerPort: false)"]
            secpol["SecurityPolicy: homelab-gateway-authz\nextAuth → security/opa:9191 (gRPC)\nattached to the Gateway, not per-route"]
        end

        subgraph gwsys["gateway-system"]
            gw["Gateway: homelab-gateway\ngatewayClassName: envoy-gateway\nlisteners: 80 (redirect), 443 (TLS terminate)\ncert: homelab-wildcard-tls (cert-manager, DNS-01)"]
        end

        subgraph opans["security namespace"]
            opa["opa Deployment\nClusterIP :9191\nRego: admin_only_apps gate"]
        end

        subgraph backends["Backend namespaces"]
            grafana["monitoring/grafana"]
            immich["immich/immich-server"]
            paperless["paperless/paperless-ngx"]
            schenkmatch["schenkmatch/schenkmatch"]
            zot["zot/zot"]
            keycloak["keycloak/keycloak"]
            hubble["kube-system/hubble-ui\n(admin_only_apps)"]
            koc["kubeopencode-system/kubeopencode-server\n(admin_only_apps)"]
        end
    end

    lan -->|"HTTPS, *.homelab.data-harness.org"| vip
    tailnet -->|"tailnet route to .200"| subnetrouter
    subnetrouter -->|"kernel-mode forward, TC INGRESS on tailscale0"| vip
    vip --> svc --> proxy
    proxy -->|"ext_authz gRPC, per request"| secpol
    secpol -.->|"Envoy → OPA"| opa
    opa -.->|"allow / deny + body"| secpol
    proxy -->|"HTTPRoute match on Host header"| grafana & immich & paperless & schenkmatch & zot & keycloak
    proxy -->|"OPA denies unless admin"| hubble
    proxy -->|"OPA denies unless admin"| koc
    gw -.->|"configures"| proxy

    style vip fill:#2d5016,color:#fff
    style opa fill:#5c1a1a,color:#fff
    style proxy fill:#1a3a5c,color:#fff
```

The VIP's survival across the Gateway-implementation swap, and the ordering constraint
that made the cutover safe rather than lucky, are both operational mechanics rather than
architecture — see `Agent.md`, "Envoy Gateway cutover (2026-08-16) — two mechanisms that
made it safe, not obvious from the manifests", for the full explanation, the live
experiment that proved it, and the checklist for not breaking it in a future change.

**Why the ingress CiliumNetworkPolicy uses 10080/10443, not 80/443.** Envoy Gateway
defaults to `useListenerPortAsContainerPort: false`: a listener below 1024 is served on
`port + 10000` inside the container, specifically so the proxy never needs
`CAP_NET_BIND_SERVICE`. A LoadBalancer VIP DNATs straight to the pod, so external traffic
arrives on the container port — a policy written for 80/443 silently drops every
connection with `policy-verdict:none INGRESS DENIED`, indistinguishable from a datapath
bug until you read the port in the Hubble verdict. This was the actual two-day blocker
behind the original (and wrong) `cilium#44630` / kube-proxy theories — see
`docs/backlog.md`.

## 2. Authorization decision path

```mermaid
sequenceDiagram
    participant C as Client
    participant E as Envoy Gateway proxy
    participant O as OPA (security ns)
    participant B as Backend

    C->>E: HTTPS request, Host: <app>.homelab.data-harness.org
    E->>O: ext_authz (gRPC), full request context
    alt admin_only_apps and no valid session
        O-->>E: allowed:false, http_status:403, body {"error":"Forbidden"}
        E-->>C: 403 (OPA's body, verbatim)
    else allowed
        O-->>E: allowed:true
        E->>B: forward (Host header preserved,\nx-forwarded-for = real client IP)
        B-->>E: response
        E-->>C: response
    end
```

OPA is consulted on **every** request through the Gateway, not just the two
`admin_only_apps` (Hubble UI, KubeOpenCode) — Grafana, Paperless, and Immich also pass
through `ext_authz` and are allowed by policy, relying on their own native OIDC clients
for the actual login. OPA's decision log is the ground truth for "is this actually
enforced" — confirmed live 2026-08-16 by reading `"result":{"allowed":...}` per app,
not by HTTP status code alone, since a generic Cilium network block and an OPA JSON
`403` are otherwise indistinguishable from `curl` output.

**Known gap, not yet closed:** neither Hubble UI nor KubeOpenCode has real per-user OIDC.
`admin_only_apps` is a coarse Rego allow/deny, not identity-aware. An `oauth2-proxy`
attempt in front of Hubble UI (2026-08-12) hit an unrelated Cilium policy-realization bug
and was abandoned; a `SecurityPolicy.oidc` block against Keycloak is the intended fix and
is not yet implemented.

## 3. East-West microsegmentation model

```mermaid
flowchart LR
    subgraph default["Cluster-wide default (CiliumClusterwideNetworkPolicy)"]
        direction TB
        deny_ing["default-deny-ingress\nallow only fromEntities: [host]\n(excludes reserved:ingress —\nthe Gateway has its own rule)"]
        deny_eg["default-deny-egress\nallow only toEntities: [host, kube-apiserver]"]
    end

    subgraph pattern["Per-namespace pattern, repeated ~25 times"]
        direction TB
        intra["allow-intra-namespace-egress\n(required in every namespace,\nor pods can't reach same-namespace peers)"]
        specific["Targeted allow rules\ne.g. allow-paperless-to-keycloak-egress,\nallow-opa-ingress, allow-gateway-ingress"]
    end

    subgraph example["Example: keycloak namespace"]
        direction TB
        kc["keycloak pod"]
        kc_rules["allow-gateway-ingress (from Envoy Gateway)\nallow-opa-ingress (OPA needs Keycloak for token introspection)\nallow-paperless-ingress (paperless OIDC callback)\nallow-intra-namespace-egress/ingress"]
    end

    deny_ing -.->|baseline| pattern
    deny_eg -.->|baseline| pattern
    pattern -.-> example

    style deny_ing fill:#5c1a1a,color:#fff
    style deny_eg fill:#5c1a1a,color:#fff
```

Default-deny is cluster-wide; every namespace opts back in explicitly. The
`default-deny-ingress` rule is scoped with `reserved.ingress: DoesNotExist` specifically
so it does not fight the Gateway's own ingress rule — an easy accidental overlap if
copied without that exclusion.

**A subtlety worth remembering when testing Gateway reachability:** an in-cluster test
pod making a cross-namespace request to the Gateway's VIP is evaluated by Cilium against
the **client pod's own egress** reaching the real resolved backend — not against the
Gateway itself ([cilium/cilium#47617](https://github.com/cilium/cilium/issues/47617),
maintainer-confirmed, not a bug). This cluster's `allow-intra-namespace-egress`-only
model means an in-cluster pod calling a different namespace's app through the Gateway
will get a false-negative 403 regardless of how the Gateway is configured. **Always test
Gateway reachability from a genuinely external client** (LAN or Tailscale) — verified
2026-08-14.

## 4. Certificate issuance (DNS-01, no inbound HTTP needed)

```mermaid
flowchart LR
    cm["cert-manager"] -->|"DNS-01 challenge"| cf["Cloudflare API\n(cloudflare-api-token secret)"]
    cf -->|"validates TXT record"| le["Let's Encrypt"]
    le -->|"issues cert"| cm
    cm -->|"writes Secret"| secret["homelab-wildcard-tls\n(gateway-system namespace)"]
    secret -.->|"referenced by"| gw2["Gateway listener :443"]
```

DNS-01 means the cluster never needs inbound port 80 reachable from the public internet
for cert issuance — consistent with the "zero public exposure" posture the two draft
ADRs (`ADR-002` "Dark Cluster", `ADR-005` "Dual-Path Hybrid") both assumed, without
either of them needing to be adopted. `external-dns` publishes the LAN-only
`192.168.178.200` under public DNS; those records only resolve for clients who can
actually route to that RFC1918 address (LAN or the Tailscale subnet route) — publicly
resolvable, not publicly reachable.

## 5. What this replaced

Until 2026-08-16, `homelab-gateway` ran on Cilium's own embedded `GatewayClass`
(`gatewayClassName: cilium`), and the OPA gate used the Gateway API `ExternalAuth`
HTTPRoute filter (GEP-1494, Experimental channel) per-route instead of the
`SecurityPolicy` shown above. Two findings forced the change in the same commit rather
than as separate steps:

- Envoy Gateway does not implement the `ExternalAuth` filter at all —
  `Accepted=False (UnsupportedValue): unsupported filter type ExternalAuth` — so a route
  still carrying it would stop resolving the instant the `GatewayClass` flipped.
- Cilium's own Gateway never needed an ingress `CiliumNetworkPolicy` for its VIP, because
  its backend is a TPROXY handoff to `127.0.0.1:13410` (the node-local embedded Envoy
  socket), not a pod IP — a fundamentally different datapath from Envoy Gateway's, where
  the VIP DNATs directly to a pod.

Full investigation trail, including two earlier (wrong) root-cause theories for the
LoadBalancer VIP failure that blocked this cutover for two days, is in
[`docs/backlog.md`](backlog.md).
