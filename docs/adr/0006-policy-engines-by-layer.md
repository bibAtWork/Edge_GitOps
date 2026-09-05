# ADR-006: Kyverno and OPA Are Split by Layer, Not by Function

**Date:** 2026-09-05
**Status:** Accepted
**Related:** [ADR-001](0001-decoupling-l4-l7-routing-cilium-envoy-gateway.md), [ADR-002](0002-flattened-hierarchical-rbac.md)

## Context

This cluster runs two policy engines, and the recurring question is which one should enforce
rules and which should only report — whether, for instance, OPA might enforce while Kyverno
audits, or the reverse.

The question contains a hidden assumption worth making explicit: that the two are
interchangeable and the choice is one of preference. They are not, and it is not.

What is actually deployed:

- **Kyverno** (`v1.19.0`) is an admission controller. It owns eight validating webhooks and
  evaluates Kubernetes objects at `kubectl apply` / Flux reconcile time.
- **OPA** is a `Service` on `:9191` in `security`, wired as Envoy Gateway `ext_authz` by three
  `SecurityPolicy` resources. It is **not** an admission webhook — there is no OPA entry in
  `validatingwebhookconfiguration`. It evaluates HTTP requests: method, path, headers, JWT
  claims.

OPA never sees a Pod spec. Kyverno never sees an HTTP request.

## Decision

**The engines are divided by layer, not by function.**

| | Kyverno | OPA |
| --- | --- | --- |
| Question | *May this **resource** exist?* | *May this **request** proceed?* |
| Layer | Kubernetes admission | HTTP request authorization |
| Fires on | apply / reconcile | every request through the Gateway |
| Input | a Kubernetes object | method, path, headers, JWT |
| Language | YAML | Rego |

**Kyverno** owns anything expressed about a Kubernetes object: image registry allowlists,
resource limits, `securityContext`, required labels — and uniquely `mutate` and `generate`,
which OPA cannot do at all.

**OPA** owns anything about a request in flight: per-path rules, JWT claim inspection,
decisions that Rego expresses well and YAML expresses badly. It is also reusable outside
Kubernetes, which Kyverno is not.

### Corollary: audit-versus-enforce is a lifecycle, not a division of labour

Splitting one layer across two engines is rejected. Concretely, using OPA to enforce
Kubernetes admission rules would mean deploying **Gatekeeper** — a third policy system
alongside Kyverno — and that fails on three counts:

1. **Two languages for one intent.** A rule audited in Kyverno YAML must be rewritten in Rego
   to enforce it. Two definitions of "approved registry" will diverge, and the audit report
   then describes something other than what is enforced. That is worse than either alone,
   because the report is still trusted.
2. **Two webhooks in the admission path.** Both must be healthy for an apply to succeed — or
   `failurePolicy: Ignore` is set and enforcement is lost silently, which is a failure mode
   this repository has produced repeatedly in other guises.
3. **No tie-break.** When the two disagree there is no defined answer.

The progression from observing to blocking belongs *inside* one engine. Kyverno v1.19 carries
`failureAction: Audit | Enforce` at **rule** granularity, and `PolicyException` for carve-outs.
Write the rule once, watch it in Audit, promote that rule to Enforce, and except the cases
that legitimately cannot comply. One language, one source of truth, and the report always
matches what is enforced.

## Consequences

- **Gatekeeper will not be added.** Kyverno already occupies the admission layer.
- **`restrict-image-registries` is `Enforce`** as of 2026-09-05. It was promoted only once it
  reported 305 pass / 0 fail, so promotion changed nothing running and changes only what may
  start. Verified by rejection, not by reading the field: a pod from `evil.example.com` is
  refused with the policy's own message while a pod from `docker.io/library` is admitted.
- **`require-resource-limits` stays `Audit`.** It fails 71 resources, most of them Longhorn
  and Cilium workloads that are unbounded deliberately. It needs `PolicyException` resources
  naming that set before promotion — which has the side benefit of making a tolerated baseline
  explicit rather than muted.
- **A rule promoted to Enforce must be verified against a request that reaches Kyverno.** Pod
  Security Standards run first and are enforced on all 30 namespaces; a non-compliant test pod
  is rejected by PSS before Kyverno evaluates it, which looks like a pass and proves nothing.
- **Neither engine substitutes for the other's absence.** Kyverno cannot restrict who may call
  `/hubble`; OPA cannot stop an unapproved image being admitted.

## Status of the OPA path

**The `ext_authz` path works.** An earlier reading of this concluded it was inert; that was
wrong, and the error is worth recording because it is easy to repeat.

OPA's HTTP access log records `/health` probes with `"Received request"`. Its **gRPC
`ext_authz` decisions are not logged that way** — they appear as decision-log entries carrying
`decision_id` and the full Envoy `input.attributes`. Grepping for `"Received request"`
therefore returns only kubelet probes and makes a working authorizer look dead.

Verified by driving traffic and watching the decisions: four requests to `immich`, `paperless`,
`grafana` and `zot` produced four decisions with matching `host` values, each
`{"allowed": true}` — via the policy's own rule 5, *no Authorization header → browser OIDC
flow → allow through*, since those apps handle their own session auth.

Two structural details that also look like faults and are not:

- The Gateway-level policy `homelab-gateway-authz` reports `Overridden=True` for
  `keycloak-admin`, `hubble-ui` and `kubeopencode`. That is Envoy Gateway precedence working
  as designed: a route-level `SecurityPolicy` supersedes a Gateway-level one. Two of those
  three declare their own `extAuth` to the same OPA service.
- An unauthenticated request to `hubble` returns 302 and never reaches OPA. Also correct —
  `oidc` runs before `extAuth`, so the redirect happens first. Testing an admin-only app
  unauthenticated cannot exercise the authorization layer.

`failOpen: false` on all three policies is therefore load-bearing rather than theoretical: OPA
is genuinely in the request path, and an OPA outage denies those routes. That is the intended
trade, but it means OPA's availability is now part of the Gateway's availability.
