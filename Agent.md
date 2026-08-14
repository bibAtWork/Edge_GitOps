# Agent Knowledge Base

Architectural lessons, debugging patterns, and hard-won invariants for this cluster.
Read this before touching Cilium network policies, the Gateway API stack, or Tailscale routing.
See `CLAUDE.md` for git and workflow rules.

---

## Cilium Gateway API — the two-policy rule

Every Cilium Gateway API setup requires **two** CiliumClusterwideNetworkPolicies on
`reserved:ingress`. Missing either one silently breaks traffic for a subset of clients.

### 1. INGRESS — who may USE the gateway

```yaml
# allow-gateway-world-ingress.yaml
endpointSelector:
  matchLabels:
    reserved:ingress: ""
ingress:
  - fromEntities: [world, host, cluster]
```

Controls which clients can establish connections to the Cilium L7LB listener.
Without this, `cilium.l7policy` fails closed for all external clients.

### 2. EGRESS — where the gateway may FORWARD

```yaml
# allow-gateway-egress-to-cluster.yaml
endpointSelector:
  matchLabels:
    reserved:ingress: ""
egress:
  - toEntities: [cluster, host]
```

Controls which backends the ingress entity may reach. Without this, L3 TUN traffic
(Tailscale) hits an EGRESS policy check that blocks forwarding to any backend.

**Why both are needed — the `enforce_policy_on_l7lb` switch:**
When `reserved:ingress` has zero ingress policies, Cilium sets `enforce_policy_on_l7lb=false`
and skips all identity checks. The moment you add ANY ingress policy (e.g., to fix a
LAN 403), enforcement flips to `true` globally — for ingress AND egress, for LAN AND
Tailscale. Adding the ingress policy without the egress policy will break L3 TUN clients.

---

## L3 TUN devices (tailscale0) behave differently from L2 Ethernet (enp2s0)

`cil_from_netdev` has two compilation paths:

| Device type | Example | Ethernet header | Proxy map write |
|-------------|---------|-----------------|-----------------|
| L2 Ethernet | enp2s0  | yes             | INGRESS direction, correct source identity |
| L3 TUN      | tailscale0 | no           | **EGRESS direction, wrong identity (ingress entity = 8)** |

For Tailscale kernel-mode routing, packets enter via `tailscale0`. The L3 TUN variant of
`cil_from_netdev` sets wrong direction bits in the TPROXY socket mark. `bpf_metadata`
in `cilium-envoy` then:

1. Runs in **EGRESS** mode instead of INGRESS
2. Returns `source_identity=8` (the ingress entity itself) instead of the client's identity
3. Falls back to the conntrack map — which is **not mounted** in `cilium-envoy`
   (`/sys/fs/bpf/tc/globals/` is only in the cilium-agent pod)
4. Defaults to the ingress endpoint identity (8)

`cilium.l7policy` then checks the EGRESS NPDS for the ingress entity rather than the
INGRESS NPDS. The EGRESS NPDS has no rule for cluster backends → 403.

**This is a Cilium upstream bug** specific to L3 TUN + native routing + Gateway API.
The `allow-gateway-egress-to-cluster` CCNP is the workaround.

---

## Diagnosing a Cilium Gateway 403

Run these steps in order. Each one narrows down which layer is failing.

### 1. Enable Envoy debug logging

```bash
kubectl exec -n kube-system <cilium-agent-pod> -- \
  cilium-dbg envoy admin logging set global debug
```

Make a test request, then:

```bash
kubectl logs -n kube-system <cilium-envoy-pod> --since=30s \
  | grep -E "CiliumPolicyFilterState|EGRESS POD IP|access_denied|DROP|conntrack"
```

Reset when done:
```bash
kubectl exec -n kube-system <cilium-agent-pod> -- \
  cilium-dbg envoy admin logging set global info
```

### 2. Read the key log fields

```
EGRESS POD IP: <src-ip>, destination IP: <dst-ip>
cilium.bpf_metadata: Using conntrack map global
cilium.bpf_metadata: IPv4 conntrack map lookup failed: No such file or directory
CiliumPolicyFilterState(): source_identity: <N>, ingress: <bool>, port: <P>
Egress network policy <endpoint-ip> DROP for <label> and destination identity: <M>
```

| Field | Healthy (LAN) | Broken (Tailscale L3 TUN) |
|-------|---------------|---------------------------|
| `ingress:` | `true` | `false` |
| `source_identity:` | 2 (world) | 8 (ingress entity) |
| conntrack lookup | not attempted | fails "No such file" |
| DROP reason | none | EGRESS policy miss |

### 3. Check the NPDS

```bash
kubectl exec -n kube-system <cilium-agent-pod> -- \
  sh -c "cilium-dbg envoy admin config networkpolicies 2>/dev/null" \
  | python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
for item in data.get('configs', []):
    if '10.244.0.186' in item.get('endpoint_ips', []):
        print(json.dumps(item, indent=2))
"
```

The ingress entity (10.244.0.186) must have:
- `ingress_per_port_policies`: identities 1, 2, and all cluster identities
- `egress_per_port_policies`: at least identity for cluster/host on `port: any`

---

## Tailscale subnet routing — required configuration

The subnet router StatefulSet in `14-tailscale-operator/config/subnet-router-hostnetwork.yaml`
must use **kernel mode** (`TS_USERSPACE=false`). Userspace mode proxies via Go's `net.Dial()`
through Cilium's cgroup socket LB. `hostNetwork: true` pods are not registered in Cilium's
cgroup-to-identity map, so all connections from them get identity 0 — which cannot appear in
any NPDS allowlist.

Kernel mode forwards packets via the `tailscale0` TUN interface. Cilium's `cil_from_netdev`
TCX hook fires, which (despite the L3 TUN direction bug) at least triggers the TPROXY path.
Combined with `allow-gateway-egress-to-cluster`, the EGRESS policy check passes.

The Cilium HelmRelease must also include `tailscale0` in `devices`:
```yaml
devices: "enp2s0,tailscale0"
```

Without this, `cil_from_netdev` never attaches to `tailscale0` and TPROXY is never triggered.

---

## BPF map visibility: agent vs envoy

| Pod | BPF path | Contents |
|-----|----------|----------|
| `cilium-agent` | `/sys/fs/bpf/tc/globals/` | `cilium_ct4_global`, `cilium_proxy4`, all shared maps |
| `cilium-envoy` | `/sys/fs/bpf/cilium/` | `devices/`, `endpoints/`, `socketlb/` only |

`bpf_metadata` in `cilium-envoy` cannot read conntrack or proxy maps from
`/sys/fs/bpf/tc/globals/`. If the socket-mark-based primary lookup fails, there is no
working fallback. Fix the primary lookup (ensure `cil_from_netdev` writes correct marks),
do not rely on the conntrack fallback.

---

## Grafana 10.5+ native alerting — notification delivery

Grafana 10.5.15 ships with two feature flags **enabled by default** that change the entire
notification dispatch path. Ignoring them causes alerts to fire in VictoriaMetrics but
silently disappear before reaching the contact point.

### `alertingNotificationsStepMode=true` (default in 10.5+)

Alerts are dispatched through a new "step-mode" execution path rather than the traditional
Prometheus Alertmanager dispatch loop. The old dispatcher goroutine still starts, but it no
longer produces the usual `ngalert.notifier component=alertmanager` dispatch logs, so its
silence looks like a bug rather than an intentional change.

**Required**: every alert rule that should send a notification must declare the contact point
directly on the rule:

```yaml
notification_settings:
  receiver: Telegram   # must match the contact point name exactly
```

Without this, step-mode does not know where to deliver, and the traditional alertmanager
routing tree is never consulted.

### `alertingUseNewSimplifiedRoutingHashAlgorithm=true` (default in 10.5+)

Changes how Grafana hashes alert groups for the notification log (nflog). A stale nflog
entry (e.g., from a previous failed delivery with a 4-hour repeat_interval) will produce
a fingerprint collision under the new algorithm. The alertmanager dispatcher sees the group
as "already notified, next at T+4h" and silently skips every subsequent dispatch until the
window expires — even across pod restarts, because nflog is persisted in Grafana's SQLite KV
store (`alertmanagernotifications` key) on the PVC.

Symptoms: alert fires, `ALERTS` metric shows `state="firing"`, but no message arrives and
no dispatcher logs appear. Changing repeat_interval or restarting the pod has no effect.

**Fix**: use `notification_settings: receiver: <name>` on the alert rule (see above). This
bypasses the alertmanager routing tree and nflog entirely, delivering directly via step-mode.

### `chatid` must be a YAML-quoted string in Helm values

go-yaml v3 (used by Helm's `toYaml`) auto-quotes integer-looking strings during marshaling.
A Telegram chat ID like `-1004444712571` must be written as a YAML single-quoted string so
the VALUE itself is a plain string (no embedded double quotes):

```yaml
# CORRECT — value is the string -1004444712571
chatid: '-1004444712571'   # trunk-ignore(yamllint/quoted-strings)

# WRONG — value is the string "-1004444712571" (with literal double quotes)
# This makes Grafana send chat_id="-1004444712571" to Telegram → 400 Bad Request
chatid: '"-1004444712571"'
```

go-yaml v3 will re-quote the plain string during `toYaml`, producing `"-1004444712571"` in
the ConfigMap, which Grafana parses as the clean string `-1004444712571`. The embedded-quote
variant adds a second layer of quoting and sends the extra characters to the Telegram API.

### Provisioning checklist for new contact points

- [ ] `chatid` (or any numeric-looking string) is single-quoted WITHOUT embedded double quotes
- [ ] `bottoken: $TELEGRAM_BOT_TOKEN` — env-var substitution in provisioning files requires
      the var to be in `envValueFrom` (not `env`) so it is available at provisioning time
- [ ] Every alert rule that should notify includes `notification_settings: receiver: <name>`
- [ ] After first deployment, wait for the configmap to render and the pod to restart before
      expecting any messages — `reloader.stakater.com/auto: "true"` handles subsequent rotations

---

## A second, distinct Cilium 403 pattern (2026-08-12) — RESOLVED 2026-08-14: not a bug, a test-methodology artifact

**Resolution, found 2026-08-14**: this was never a real bug or a real production issue. See
"Gateway 403 cluster-wide — resolved" further down (after the 2026-08-14 Envoy Gateway
section) for the full explanation and live proof. Short version: every test that produced
this 403 across both 2026-08-12 and 2026-08-14 was run from an **in-cluster pod**
(`kubectl run curl-...`), and Cilium enforces Gateway-routed policy for pod-origin traffic
against the *client pod's own egress* reaching the real resolved backend identity+port — not
against reaching the Gateway broadly. That's documented, intentional Cilium behavior, not a
bug (confirmed by Cilium maintainers on a near-identical upstream report). Genuine external/
LAN/Tailscale clients were never subject to this restriction and were reachable the whole
time. The section below is kept as the original diagnostic record.

Spent a very long time this session on a **different** Cilium `403 Access denied`
pattern than the one documented above. Confirmed the existing two-policy rule
(`allow-gateway-world-ingress` + `allow-gateway-egress-to-cluster` on
`reserved:ingress`) is present and correctly configured — this is **not** a
recurrence of that bug. Documenting so the next session doesn't re-walk the
same dead ends, and starts with the diagnostic technique above (Envoy debug
logging + NPDS dump) instead of guessing at network policies blindly, which is
what ate most of the time this round.

**Symptoms observed**:

- Some source namespaces (`paperless`, `security`, `kube-system`) get `403
  Access denied` on **every** request through the Gateway to **any**
  backend — not tied to a specific destination. Other namespaces
  (`monitoring`) succeed identically. TCP/TLS layer is byte-for-byte
  identical (full handshake, request sent); the 403 has no
  `x-envoy-upstream-service-time` header, meaning it never reached upstream.
- Separately, same-namespace pod-to-pod traffic within `kube-system`
  (`hubble-auth` → `hubble-ui`, both freshly created same-day) hit the
  identical-looking `403 Access denied`, even with a `CiliumNetworkPolicy`
  `fromEndpoints` rule that label-matched exactly, confirmed correct via
  `cilium-dbg endpoint get` showing the rule present in
  `rules-by-selector` — but `allowed-ingress-identities` never grew beyond
  the fixed reserved set `[1,3,4,5,6,7,8,11]`; no regular pod identity ever
  appeared there, for any policy added.

**Hypotheses tried and ruled out this session** (don't re-try these):
rate limiting, Envoy connection pooling/reuse, Pod Security Standard level
(temporarily matched `paperless`'s PSS to `monitoring`'s `privileged` —
no change), the `allow-intra-namespace-ingress` fix that resolved an
apparently-identical problem for `keycloak` earlier the same session (added
the same fix for `kube-system` — did not help), DNS resolution differences
(identical resolved IP for working and broken namespaces).

**Not yet tried**: the actual diagnostic procedure two sections up (Envoy
debug logging, reading `source_identity`/`ingress:`/DROP-reason fields,
dumping NPDS via `cilium-dbg envoy admin config networkpolicies` filtered by
endpoint IP). This session used `cilium-dbg endpoint get <id>` (realized
policy dump) instead, which shows the *intended* policy but apparently not
whether it's actually being matched at connection time — the NPDS dump is
probably the right next step, matching the existing playbook above instead
of a new one.

**Where this is parked**: `docs/backlog.md` (OPA gate / Hubble UI oauth2-proxy
attempt). The oauth2-proxy work itself was reverted/cleaned up; only the
Keycloak-realm and network-policy learnings from this investigation are worth
keeping, which is what this section is.

---

## Follow-up (2026-08-12) — RESOLVED 2026-08-14, see below: not a bug, a test-methodology artifact

Came back to this the same day while deploying KubeOpenCode — its HTTPRoute hit
the exact same 403 as above. This time ran the **actual documented diagnostic**
(Envoy debug logging + NPDS dump) instead of guessing at policies. Two findings:

**Finding 1 — this is not Tailscale/L3-TUN-specific after all.** Reproduced the
403 with a request that never touches Tailscale at all: a pod inside the
cluster (`kubeopencode-system`) curling the Gateway's own L2-announced IP
(`192.168.178.200`) directly. Same signature as the original "healthy vs
broken" table:
```
EGRESS POD IP: 10.244.0.123, destination IP: 192.168.178.200 sni: "grafana.homelab.data-harness.org"
CiliumPolicyFilterState(): source_identity: 8, ingress: false, port: 443, ...
```
`source_identity: 8` matches the Gateway's own endpoint identity
(`reserved:ingress`, confirmed via `cilium-dbg endpoint list`). `ingress: false`
is the same misclassification as the L3-TUN case. So whatever's misclassifying
direction affects **any** hairpin path back to the Gateway's own announced IP,
not just tailscale0 — the original theory was too narrow.

Also newly confirmed: this affects `monitoring` (grafana) too now, which the
section above recorded as the one namespace that *worked*. Either it's
non-deterministic/order-dependent, or something regressed since that was
written. Confirmed via a clean experiment that this is **not** caused by any
kubeopencode change: reverted the (uncommitted) Gateway edit that added
`kubeopencode-system` to `allowedRoutes`, retested grafana with the Gateway back
to its exact pre-session state — still 403. Pre-existing, independent of this
session's work.

**Finding 2 — plausible-but-ruled-out lead: missing ipcache entry.**
`cilium-dbg ip list` has **no entry at all** for `192.168.178.200`. Theory: the
misclassified-as-egress check (identity 8 → destination) can't resolve the
L2-announced LB IP to `host`, falls through to `world`, which isn't in
`allow-gateway-egress-to-cluster`'s allow-list (`toEntities: [cluster, host]`
only, no `world`). Tested by restarting the Cilium agent DaemonSet (thinking a
stale/unpopulated cache) — ipcache entry still absent after restart, and the
403 was **unchanged**. So either this ipcache gap is normal/expected for
L2Announce IPs (not the bug), or it's a real symptom but the agent restart
doesn't repopulate it. Not confirmed as root cause either way — don't spend
time re-testing the "just restart cilium" angle again.

**Still not tried**: cross-referencing this exact signature
(`bpf_metadata`/`cil_from_netdev` direction misclassification for L2Announce
hairpin traffic, independent of Tailscale) against upstream Cilium GitHub
issues — this smells like a known upstream bug class rather than something
specific to this cluster's config, given it reproduces identically for
in-cluster-pod-to-announced-IP traffic with zero Tailscale involvement.
Worth searching Cilium's issue tracker for `cil_from_netdev` + L2 announcement
+ Gateway API direction/identity bugs before the next from-scratch debugging
attempt.

**Current status**: every app behind `homelab-gateway` is currently affected
(confirmed for `monitoring`/grafana; presumed for the others per the original
section). This is a live, cluster-wide, pre-existing issue — not something
introduced by any work in this session. kubeopencode's HTTPRoute is wired
correctly (matches every other app's pattern exactly) but is blocked by this
same bug pending a real fix.

---

## Envoy Gateway migration attempt (2026-08-14): narrowed to L2Announce'd LoadBalancer VIPs specifically

ADR-001 (`docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md`) decided to move
L7 routing off Cilium's own Gateway API implementation onto a dedicated Envoy Gateway
control plane — partly to sidestep the entire `reserved:ingress`/L7LB-entity bug class
documented above. Envoy Gateway was installed (`28-envoy-gateway/`, kept in the cluster,
namespace `envoy-gateway-system`, chart `gateway-helm@1.8.3`) and `homelab-gateway` was cut
over to it. **The cutover was rolled back the same session** — every app became completely
unreachable (not 403, full TCP timeout). A same-day follow-up (see "Follow-up: narrowed to
the LoadBalancer VIP specifically" below) found the actual scope, which is much narrower
than first thought: **this is not a general policy bug — it's specific to traffic destined
to an L2-announced LoadBalancer VIP reaching a backend that isn't Cilium's own
`reserved:ingress` L7LB entity.** The section immediately below is the original same-day
writeup (kept for the full diagnostic trail); the narrower, corrected finding follows it.

### What was ruled out, cleanly, in order

1. **LB-IPAM/L2Announce selector labels** — Envoy Gateway's generated Service uses
   `gateway.envoyproxy.io/owning-gateway-name`/`-namespace` (confirmed via `cilium-dbg
   endpoint list`), not Cilium's own `io.cilium.gateway/owning-gateway`. Updated
   `lb-ipam.yaml` accordingly. **Gotcha**: `kubectl apply` on `CiliumLoadBalancerIPPool`/
   `CiliumL2AnnouncementPolicy` **merges** `matchLabels` instead of replacing them — editing
   the selector to different keys leaves the *old* key behind too (AND semantics), silently
   making the selector impossible to satisfy. Use `kubectl replace`, not `apply`, when
   changing these selectors' keys (not just values).
2. **L2 announcement not picking up the new Service** — the `l2-announce` statedb table was
   empty after cutover despite correct LBIPAM assignment; a Cilium agent DaemonSet restart
   populated it. Real, reproducible gap (control-plane state existed but didn't reach the
   L2-announcer job until restart) — not the final blocker, but worth knowing.
3. **ARP-level reachability** — initially suspected, then ruled out: a genuinely external
   client's ARP table correctly resolved the announced IP to the node's real MAC. The
   failure is downstream of ARP, at the Cilium datapath/policy layer.
4. **`allowed-ingress-identities` missing `world` (2)** — initially looked like the bug
   (`[1,3,4,5,6,7,8,11]`, no `2`, despite a policy explicitly granting `fromEntities:
   [world]`). **Ruled out as the explanation**: the *working* `allow-internet-egress-https-only`
   path shows the identical list on `allowed-egress-identities` for pods where egress to
   world is confirmed functional elsewhere. This field apparently just doesn't reflect
   world/CIDR-based identities at all — a red herring, not a bug.
5. **Test client routing through Tailscale unexpectedly** — the first "clean" external test
   was actually going through `tailscale0` (Windows preferred the `/32` Tailscale route over
   the LAN `/24`, confirmed via `route print` and the drop log's Tailscale-CGNAT source IP).
   That's the *already-documented* L3-TUN bug, not a new one — retested from a path
   guaranteed not to touch Tailscale (in-cluster pod → announced IP) to get a clean signal.
6. **Entity-based vs label-based ingress rules** — tested both `fromEntities: [cluster]` and
   a `fromEndpoints` rule matching the exact source pod's labels, scoped to the Envoy pod's
   ingress. Identical failure both ways — not about rule syntax.
7. **Egress-side vs ingress-side** — granted the source pod an explicit `toEndpoints` egress
   rule matching the destination pod's labels (removing any egress-side ambiguity entirely).
   **Identical drop, unchanged**: `identity <source>-><dest>: ... Policy denied`.
8. **Comparison against a documented-working pod** (Keycloak, reachable from Paperless in
   production today) — shows the *exact same* restricted `allowed-ingress-identities` list.
   Whatever makes Keycloak reachable in practice, it isn't reflected in this field either —
   reinforcing point 4, and meaning this field is not a reliable diagnostic signal on this
   cluster/version at all.
9. **Cluster-wide `default-deny-ingress` CiliumClusterwideNetworkPolicy itself** — deleted
   entirely (backed up first, restored immediately after, with explicit user sign-off since
   this is a real security-boundary removal) as the most direct possible test. **Identical
   timeout, unchanged.** Caveat: `allow-cluster-internal` (a separate, pre-existing
   CiliumClusterwideNetworkPolicy, ingress-only, `fromEntities: [cluster]`) was still present
   and still selects every non-`reserved:ingress` endpoint — so this wasn't a true
   zero-policy test, just a test with the *deny* baseline removed. Worth redoing with
   *every* CCNP temporarily gone if this is picked up again, to get a genuinely clean read.
10. **Cilium's own embedded Envoy proxy** (`envoy.enabled`, on by default, undocumented in
    this repo's HelmRelease) — disabled it, matching the pattern in
    [github.com/michaelbeaumont/k8rn](https://github.com/michaelbeaumont/k8rn), a real
    Cilium+Envoy-Gateway integration that explicitly turns this off for exactly this class of
    conflict. `cilium-envoy` pods confirmed fully gone. **Identical timeout, unchanged.**
    Reverted afterward — `cilium-envoy` came back healthy with no lasting issues from the
    toggle.

### What's still true and unexplained

- The failure is a hard drop at the Cilium datapath (`bpf_lxc.c:1663`/`bpf_lxc.c:2410`,
  `drop (Policy denied)`), not a timeout from routing, ARP, or L2 announcement — those were
  all independently confirmed working.
- It reproduces identically regardless of: which policy grants access, whether the rule is
  entity- or label-based, which side (source egress vs destination ingress) the rule is
  attached to, whether Cilium's embedded Envoy runs at all, and even with the cluster's main
  deny baseline removed.
- It affects a *brand-new* ordinary pod (Envoy Gateway's data-plane pod) talking to a
  long-lived, demonstrably-reachable-in-other-contexts pod (kubeopencode's own agent pod) —
  ruling out "newly created identity hasn't propagated yet" as an explanation, since the
  failure persisted across many minutes and several agent restarts.
- `kubeProxyReplacement: true` is enabled cluster-wide (`05-cilium/helmrelease.yaml`) — not
  investigated as a variable this session, but changes how Service traffic is processed
  end-to-end and is a reasonable next thing to isolate (e.g., does the same drop reproduce
  for direct pod-IP-to-pod-IP traffic that never goes through a Service/DNAT at all?).

### Recommended next steps, in priority order

1. Reproduce with the **cleanest possible minimal case**: two freshly-created pods in a
   fresh namespace, zero custom policy, one single `CiliumNetworkPolicy` granting exactly
   one direction — and remove `allow-cluster-internal` too (point 9's caveat) to get a truly
   policy-free baseline reading.
2. Test direct pod-IP-to-pod-IP traffic (bypassing any Service/ClusterIP/DNAT) to isolate
   whether `kubeProxyReplacement`/socket-LB is a variable.
3. Search Cilium's GitHub issues for the exact drop signature
   (`bpf_lxc.c:1663`/`bpf_lxc.c:2410`, `Policy denied`, regular workload identity never
   reaching `allowed-ingress-identities`) — this smells like a known upstream bug class
   given how it reproduces identically across every variable tested.
4. Consider a Cilium version bisection (this cluster runs 1.20.0) if the above doesn't
   surface a match — check whether the same reproduction succeeds on an older/newer minor.

### Current state

Rolled back cleanly: `homelab-gateway` is back on `gatewayClassName: cilium`, LB-IPAM/L2
Announce selectors reverted to Cilium's own labels, Cilium's embedded Envoy re-enabled.
Confirmed working (same 403-on-this-test-path as before, not a timeout — matches
pre-migration behavior exactly). Envoy Gateway itself (`28-envoy-gateway/operator/`,
`config/`) stays installed and idle — not wired to any traffic — so this can resume without
redoing the install once the Cilium bug above is understood.

### Follow-up (2026-08-14, same day): narrowed to the LoadBalancer VIP specifically

Picked up "recommended next step 2" above (isolate whether `kubeProxyReplacement`/socket-LB
is a variable) without needing to touch cluster-wide policy or take production down again —
this test only needed the already-idle Envoy Gateway data-plane pod (still running from the
cutover attempt) plus a pair of narrow, throwaway `CiliumNetworkPolicy` rules.

**Three-way comparison, identical policy rules throughout** (a `fromEndpoints`/`toEndpoints`
pair matching the exact source/destination pod labels — the same shape that failed
repeatedly via the LoadBalancer VIP the first time):

| Destination | Result |
|---|---|
| Envoy pod's own IP directly (`10.244.0.126:10443`, no Service involved at all) | **Works** (`404`, real HTTP response) |
| The Service's `ClusterIP` (`10.108.204.185:443`) | **Works** (`404`, real HTTP response) |
| The Service's L2-announced LoadBalancer external IP (`192.168.178.200:443`) | **Fails** (timeout — this is what every test earlier in the day used) |

Same pod, same backend, same policy — only the destination address category changes. This
conclusively rules out "CiliumNetworkPolicy identity realization is broken" as the
explanation (points 4, 6, 7, 8 in the section above all used addresses that, per this table,
were never going to work regardless of policy correctness). **The real scope: L2Announce'd
LoadBalancer VIP traffic reaching an ordinary (non-`reserved:ingress`) backend pod fails,
full stop — direct pod IP and ClusterIP both work identically to how they'd work on any
normal Kubernetes cluster.**

**Working theory**: Cilium's L2Announce + LoadBalancer-Service datapath appears to route
*all* traffic to such a VIP through the same internal TPROXY/L7LB pipeline that
`reserved:ingress` uses — regardless of which controller (Cilium's own Gateway vs an
external one like Envoy Gateway) actually owns the Service object, and regardless of whether
the destination pod is meant to participate in that pipeline at all. Cilium's own Gateway
works specifically *because* the two-policy-rule at the top of this document was built to
satisfy that pipeline's entity-based policy model — not because `reserved:ingress` is exempt
from some bug, but because the pipeline expects exactly that model and nothing else
satisfies it. An ordinary backend pod (Envoy Gateway's data-plane pod, or any other Service)
was never designed to be checked against that pipeline, so its ordinary
`fromEndpoints`/`toEndpoints` policy never matches, and the connection drops.

**This retroactively explains why the reference project
([github.com/michaelbeaumont/k8rn](https://github.com/michaelbeaumont/k8rn)) doesn't use
Cilium's LoadBalancer/L2Announce mechanism for Envoy Gateway at all** — it uses
`hostNetwork: true` + a `NodePort` Service instead, with `external-dns` pointing DNS records
directly at node IPs. That's not an arbitrary style choice; it's very likely a deliberate
workaround for this exact entanglement.

**Both concrete fixes below were actually tried the same day** (not just proposed) — neither
is fully working yet, but each narrowed the problem further. Full detail follows; the repo's
committed `28-envoy-gateway/config/envoyproxy.yaml` was left at its original LoadBalancer
form (this section is the record of what was tried, not a description of current config).

#### Attempt 1: `hostNetwork` + `NodePort` (the k8rn pattern) — partially worked, new blocker found

Set `EnvoyProxy.spec.provider.kubernetes.envoyDeployment.patch` to `hostNetwork: true` +
`dnsPolicy: ClusterFirstWithHostNet`, `envoyService.type: NodePort`,
`useListenerPortAsContainerPort: true` (so the container binds the Gateway's actual listener
ports directly, since hostNetwork means container port == host port) — matching
[github.com/michaelbeaumont/k8rn](https://github.com/michaelbeaumont/k8rn)'s
`services-gateway.yaml` exactly (fetched and compared directly, not from memory).

**Two real sub-issues found and fixed along the way**:
- Cilium's own embedded Envoy (`cilium-envoy`) also runs `hostNetwork` on this node with the
  default `--base-id 0` — a second Envoy instance with the same base-id fails outright
  (`errno=98`, `EADDRINUSE`, Unix domain socket collision). Fixed with
  `EnvoyProxy.spec.extraArgs: [--base-id, "1"]`.
- `envoy-gateway-system` needs `pod-security.kubernetes.io/enforce: privileged` for
  `hostNetwork` to be admitted at all (confirmed via k8rn's own namespace manifest, which
  uses the same label).

**Once those were fixed, tested on an unprivileged port (8080) first, deliberately, before
touching 443/80 — and it worked immediately**: both the node's Tailscale-registered
`InternalIP` (`100.95.245.36`) and its real LAN IP (`192.168.178.100`) returned real HTTP
responses with zero timeout, using the exact same `CiliumNetworkPolicy` rules that failed
every time via the L2Announce'd VIP. This is strong independent confirmation of the finding
above — hostNetwork+NodePort genuinely sidesteps the L2Announce/TPROXY coupling.

**The blocker**: switching to the real ports (443/80, `useListenerPortAsContainerPort: true`)
fails at Envoy's own listener startup — `cannot bind '0.0.0.0:443': Permission denied` —
**even running the container as root** (`runAsUser: 0`, `runAsNonRoot: false`), which should
never produce `EACCES` on a privileged-port bind under normal Linux semantics. Tried, in
order, all unsuccessful: `NET_BIND_SERVICE` capability alone; `NET_BIND_SERVICE` +
`allowPrivilegeEscalation: true` (in case `no_new_privs` was blocking ambient capability
propagation to a non-root process); full root; root + `NET_BIND_SERVICE` + `NET_ADMIN` (in
case Envoy's hostNetwork listener requests `IP_TRANSPARENT`, which needs `NET_ADMIN`
specifically); the same plus `seccompProfile: Unconfined` (in case `RuntimeDefault` blocks a
specific `setsockopt`/`bind` syscall pattern Envoy uses that a minimal test process doesn't).
**None worked, including root** — ruling out capability/UID/seccomp misconfiguration as the
explanation. **Also ruled out as a blanket Talos restriction**: a plain `busybox nc -l -p 443`
pod, `hostNetwork: true`, `runAsUser: 0`, on the same node, bound port 443 immediately with
no issue. So the failure is specific to something in *Envoy's own* listener bind path under
hostNetwork on this node/Talos version — not yet root-caused. Worth checking Envoy's actual
`setsockopt` calls for hostNetwork-mode listeners (strace, or Envoy's own verbose startup
logging) rather than continuing to guess at security-context permutations.

### Root cause identified (2026-08-14, later same day): matches known, unresolved upstream Cilium bugs — not fixable from this repo alone

Searched Cilium's GitHub issues for the exact signature above (L2Announce'd LoadBalancer VIP,
plain timeout, zero drop/policy-verdict telemetry) instead of continuing to guess. Found a
closely-matching, still-open/stale bug class, not something specific to this cluster's config:

- **[cilium/cilium#44630](https://github.com/cilium/cilium/issues/44630)** ("External traffic
  to LoadBalancer VIP silently dropped when backend pod is on the same node") — signature
  matches exactly: SYN packets arrive, no SYN-ACK, **no drop events, no policy verdicts**,
  conntrack entry created but `Packets=0`. Root cause per the issue: the eBPF same-node LB
  DNAT path silently fails to forward to the local backend pod's veth, specifically when the
  L2-announce lease and the Service's backend pod land on the *same node*. Reported against
  Cilium 1.19.1 and 1.19.3 (two independent clusters, different CNI setups); closed by a
  staleness bot in 2026-07-13 with **no fix landed**, not because it was resolved.
- **[cilium/cilium#44187](https://github.com/cilium/cilium/issues/44187)** ("Cilium Gateway
  API with nodeIPAM not reachable from external clients") — same architectural pattern
  (LoadBalancer Service + same-node backend), specifically for Gateway API LoadBalancer
  Services. A commenter's workaround: "deployed Envoy Gateway via helm... and everything works
  perfectly" — independent confirmation that swapping to a real DaemonSet-style ingress
  controller (not a single-replica LoadBalancer-fronted Deployment) sidesteps this class of
  bug, which lines up with why the k8rn reference project uses `hostNetwork`+`NodePort`
  instead of a LoadBalancer Service for Envoy Gateway.

**Why this cluster is permanently exposed to it**: it's single-node. Any *new* LoadBalancer
Service's L2Announce lease is unavoidably colocated with its own backend pod — there's no
other node for the lease to land on. The documented community workaround (a dedicated
L2-announce node pool, disjoint from backend nodes) requires multiple nodes and doesn't apply
here. **Why Cilium's own `homelab-gateway` doesn't hit it**: its L7LB traffic never takes the
eBPF same-node-DNAT-to-a-different-pod path at all — Envoy owns the socket directly via
TPROXY, matching the two-policy-rule model at the top of this document, not a Service backend
DNAT hop. Any *other* LoadBalancer Service (Envoy Gateway's own, or any future one) will hit
this bug on this cluster specifically because it's single-node.

**What this means for future attempts**: the "Recommended next steps" list two sections up
(minimal zero-policy reproduction, GitHub issue search, version bisection) is now superseded
by this finding — the issue search *was* the right next step, and it surfaced a real,
unresolved upstream bug, not a local misconfiguration to keep chasing. Don't re-attempt a
LoadBalancer-Service-based fix (plain `type: LoadBalancer`, or `externalIPs` layered on one)
on this cluster while it stays single-node — it will hit this same class of bug regardless of
CiliumNetworkPolicy correctness. `hostNetwork`+`NodePort` (Attempt 1 above) is the *architecturally
correct* workaround for exactly this reason — NodePort binds directly on the node and never
takes the LoadBalancer/L2Announce DNAT path at all, which is exactly why it worked immediately
on an unprivileged port. The **only** remaining blocker for that path is the unrelated
"Permission denied binding 443/80" issue documented in Attempt 1 — not this bug. If Envoy
Gateway is revisited, start from hostNetwork+NodePort with that specific privileged-port
problem as the sole open question, not from scratch.

#### Attempt 2: `externalIPs` instead of hostNetwork or LoadBalancer — did not work either

Theory: `externalIPs` (a Service listing a manually-specified, already-owned node address —
`192.168.178.100`, this node's real `enp2s0` IP, no ARP/L2Announce involved at all) might use
the same ordinary DNAT path already confirmed working for `ClusterIP` traffic in the
three-way comparison above, sidestepping both the L2Announce/TPROXY coupling *and* the
hostNetwork privileged-port fight. Set via `EnvoyProxy`'s `envoyService.patch` (`spec.type`
left as `LoadBalancer`, `spec.externalIPs: [192.168.178.100]` added).

**Result: identical timeout to the original L2Announce VIP failure** — not the "Permission
denied" from attempt 1, and not even a `cilium-dbg monitor --type drop` entry correlating to
the test flow at all (only unrelated, pre-existing drops from a completely different flow
were visible). The absence of any Cilium-level drop log for this specific flow suggests the
packet isn't reaching Cilium's policy engine at all — plausibly a service-map/DNAT resolution
failure rather than a policy denial, but not confirmed. **This means `externalIPs` on a
`type: LoadBalancer` Service doesn't actually avoid the underlying issue** — worth retesting
with `type: ClusterIP` + `externalIPs` specifically (not `LoadBalancer` + `externalIPs`,
which is what was actually tested) in case the Service *type* itself — not just how the
external address got assigned — is what triggers Cilium's special-case datapath handling.

**Where this leaves things**: hostNetwork+NodePort on an *unprivileged* port is proven
working. The only remaining problem is getting Envoy's own listener to bind 443/80 under
hostNetwork on this node — a narrower, more specific problem than where this investigation
started. `externalIPs` was a plausible-sounding shortcut that turned out not to work,
demonstrated rather than assumed.

---

## Gateway 403 cluster-wide — resolved 2026-08-14: not a bug, a test-methodology artifact

The "Gateway 403 confirmed cluster-wide" issue (first found 2026-08-12, re-confirmed
2026-08-14 during the Envoy Gateway investigation, believed for two sessions to be a live,
unresolved production issue affecting every app behind `homelab-gateway`) turned out not to
be a bug at all, and never to have affected real traffic.

**The explanation, found via [cilium/cilium#47617](https://github.com/cilium/cilium/issues/47617)**
(closed as user error, but the maintainer explanation applies exactly here): for **pod-origin
traffic** — a request originating from *inside* the cluster, whether hitting the Gateway's
ClusterIP or its L2-announced LoadBalancer VIP — Cilium enforces network policy on the
*client pod's own egress*, checked against the **real, DNAT-resolved backend identity and
pod port** the HTTPRoute selected, not against the Gateway's own address or the
`reserved:ingress` entity. Quoting a Cilium maintainer directly on the closed issue: "you
_have_ to treat Envoy as a hop in the data path, even when the traffic originates from inside
the cluster... The traffic picks up the `reserved:ingress` identity once it arrives at Envoy,
and is treated accordingly." This is documented, intentional behavior — see
[Cilium's Gateway API reference docs](https://docs.cilium.io/en/stable/network/servicemesh/gateway-api/gateway-api/#reference).

**Every test that produced the 403 across both sessions used an in-cluster test pod**
(`kubectl run curl-...`) as the client — which is exactly the traffic pattern subject to this
restriction. This cluster's network policy model gives most namespaces only
`allow-intra-namespace-egress` (same-namespace only), so an in-cluster pod in `paperless` or
`kube-system` curling the Gateway to reach `grafana` (a different namespace's backend pod)
was *never* going to be permitted — regardless of how correct the Gateway's own ingress/egress
policy was. This also explains the original "`monitoring` works, others don't" observation:
the test pod used for that check was itself inside `monitoring`, the same namespace as
grafana's backend, so `allow-intra-namespace-egress` happened to already cover it.

**Genuine external clients (LAN, Tailscale, or any real browser) were never subject to this
restriction** — they don't have a Cilium pod identity or egress policy to enforce in the first
place; they're handled entirely by the `reserved:ingress` ingress-allow (the two-policy-rule
at the top of this document), which has been correctly configured the whole time.

**Proven live, 2026-08-14**, testing from a genuinely external LAN client (not an in-cluster
pod) against every app behind `homelab-gateway`:

| App | Result |
|---|---|
| grafana | `302` → `/login`, `x-envoy-upstream-service-time` present (real backend response) |
| paperless-ngx | `302` → login, real backend response |
| keycloak | `302` → login, real backend response |
| immich | `200` |
| schenkmatch | `200` |
| zot | `200` |
| kubeopencode | `200` |
| hubble-ui | `403`, but **OPA's own** `{"error":"Forbidden"}` JSON body (`content-type: application/json`) — the *correct*, intended result of the 2026-08-14 OPA `ExternalAuth` gate fix (see `docs/backlog.md`), not the old bug |

**No production issue exists and none ever did**, as far as this investigation found. The
"everything 403" finding that partly motivated the Envoy Gateway migration (ADR-001) and
consumed significant debugging time across two sessions was chasing a test artifact, not a
real outage. This doesn't invalidate ADR-001's other stated goals (moving OIDC/ext_authz/rate
limiting off Cilium's Gateway onto a dedicated control plane is still a reasonable direction),
but the specific "Gateway is broken for real users" framing that motivated urgency should be
retired. **When testing Gateway reachability in future sessions, use a genuinely external
client (this machine, or `curl` from outside the cluster) — an in-cluster test pod is not a
valid stand-in and will produce false-negative 403s that look identical to a real bug.**

---

## Prevention checklist for future policy changes

Before adding or removing any `CiliumNetworkPolicy` or `CiliumClusterwideNetworkPolicy`
on `reserved:ingress`:

- [ ] Does `reserved:ingress` still have at least one ingress policy? (Removing all disables
      enforcement silently via `enforce_policy_on_l7lb=false`.)
- [ ] Is `allow-gateway-egress-to-cluster` present? (Required whenever any ingress policy
      exists, because L3 TUN traffic always hits the egress code path.)
- [ ] After applying, test from **both** a LAN browser and a Tailscale client. They exercise
      different `bpf_metadata` code paths and can fail independently.
- [ ] Check `envoy_cilium_access_denied` metric is zero after the change:
      ```bash
      kubectl exec -n kube-system <cilium-agent-pod> -- \
        cilium-dbg envoy admin stats | grep access_denied
      ```
