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

## A second, distinct Cilium 403 pattern (2026-08-12, unresolved)

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

## Follow-up (2026-08-12, still unresolved): ran the actual diagnostic, found a new lead, ruled it out

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

## Envoy Gateway migration attempt (2026-08-14): a deeper, still-unresolved Cilium policy bug

ADR-001 (`docs/adr/0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md`) decided to move
L7 routing off Cilium's own Gateway API implementation onto a dedicated Envoy Gateway
control plane — partly to sidestep the entire `reserved:ingress`/L7LB-entity bug class
documented above. Envoy Gateway was installed (`28-envoy-gateway/`, kept in the cluster,
namespace `envoy-gateway-system`, chart `gateway-helm@1.8.3`) and `homelab-gateway` was cut
over to it. **The cutover was rolled back the same session** — every app became completely
unreachable (not 403, full TCP timeout), and the root cause was never found despite an
extensive, methodical investigation. Documenting in full because the finding is bigger than
Gateway API: **this looks like a general Cilium policy-realization bug affecting ordinary
pod-to-pod traffic, not just the special Gateway L7LB entity.**

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
