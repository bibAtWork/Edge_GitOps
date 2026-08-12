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
