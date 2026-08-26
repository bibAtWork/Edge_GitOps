# ADR-002: Flattened Hierarchical RBAC (Max-Depth-1)

**Date:** 2026-08-18
**Status:** Accepted — rollout in progress

## Context

Access control up to this point has been ad hoc per app: Grafana checks Keycloak group
membership with a one-off JMESPath expression, Paperless syncs Keycloak groups into Django
groups with no defined group contents yet, and Hubble UI / KubeOpenCode get a single binary
`is_admin` check in OPA's Rego (`24-opa/configmap.yaml`) because neither has ever had a
`groups` claim to read. `kubectl` access to the API server has no identity layer at all —
cert-based only (tracked in `docs/backlog.md`, "Kubernetes-native RBAC has no identity
layer"). None of this shares a model, so adding a new app or a new access tier means
inventing the logic again from scratch, and there is no single place to answer "what can
this person actually do across the cluster."

Deep hierarchical RBAC (Role D inherits C inherits B inherits A) is the standard alternative,
but it is prone to privilege creep — a change to a foundational role silently changes
everything downstream — and makes auditing "why does this person have this access" require
walking an arbitrarily long chain.

## Decision

Standardize on a **Flattened Hierarchical RBAC model, capped at inheritance depth 1**,
combined with horizontal (per-app) policy composition, for every identity-aware service in
this cluster.

**Level 0 — Base tiers** (never assigned to a user directly):
- **Reader** — read-only.
- **Maintainer** — standard operational access (create/read/update/delete on the resources
  the service exists to manage), explicitly excluding anything IAM-shaped (user/group/role
  management, workflow/global-config changes that affect other users).
- **Owner** — unrestricted, including the IAM-shaped access Maintainer is denied.

**Level 1 — Job-function roles** (what users actually get assigned):
- Each inherits **exactly one** Level 0 tier.
- Any extra permission a job function needs is added horizontally — a specific, independent
  permission grant on that one role — never by inheriting a second role.

Backed by INCITS 359-2012 (NIST RBAC Reference Model — Hierarchical and Constrained RBAC)
and NIST SP 800-53 Rev. 5 AC-3/AC-5/AC-6.

### How this is implemented in this repo

- **Mechanism: Keycloak Groups, not composite Realm Roles.** Grafana's `role_attribute_path`
  and Paperless's `SOCIAL_ACCOUNT_SYNC_GROUPS` (added for the Paperless SSO fix, PR #186)
  both already read the `groups` claim — reusing that avoids a new claim/mapper and keeps
  every app on the primitive it already understands. Level 0 tiers are top-level groups
  (`/reader`, `/maintainer`, `/owner`) that nothing is assigned to directly; Level 1 roles are
  subgroups nested exactly one level under one Level 0 group (e.g. `/owner/platform-admin`).
  The `groups` claim mapper stays leaf-name-only (`full.path: false`, unchanged) — each app
  keeps one small, explicit tier-mapping table (its horizontal composition) rather than
  parsing group paths, since two apps rarely grant identical capabilities for the same tier
  anyway.
- **Level 1 roles at initial rollout:** `platform-admin` (→ Owner), `app-operator` (→
  Maintainer), `viewer` (→ Reader) — one per tier to start. The model expects this list to
  grow (e.g. a future narrower `database-admin` under Maintainer) without ever touching
  Level 0.
- **Guardrail:** an OPA/Rego policy — reusing the policy engine already deployed for the
  Gateway's `admin_only_apps` gate, rather than a bespoke script — walks the Keycloak group
  tree and fails the check if any Level 1 group has more than one Level 0 parent, or any
  Level 0 group has a parent at all. Run in CI against the GitOps-declared group structure on
  every PR touching it, and on a schedule against Keycloak's live group tree, since the realm
  is managed by an idempotent setup Job rather than continuously Flux-reconciled the way the
  rest of the cluster is — a manual console change would otherwise go uncaught indefinitely.
- **Scope of this rollout:** every identity-aware surface in the cluster — Grafana, Paperless
  (already Keycloak-integrated); Hubble UI and KubeOpenCode (currently edge-authenticated via
  Envoy `SecurityPolicy.oidc` but only binary-gated downstream); Immich and zot
  (no Keycloak integration today — this adds it); and the Kubernetes API server itself, closing
  the gap `docs/backlog.md` tracks as deferred, via `apiServer.extraArgs`
  (`oidc-issuer-url`/`oidc-client-id`/`oidc-groups-claim`) in
  `cluster/overlays/1-node/talos-machineconfigs/controlplane.yaml`. Cert-based `kubectl` admin
  access is left untouched throughout as the break-glass path, and this piece lands last,
  after every other surface is proven, given it is the only one that touches the control
  plane of a single, non-HA node.

## Consequences

**Positive:**
- No privilege creep: a Level 0 change is predictable and auditable by construction — it can
  only ever affect users through exactly one hop.
- Auditing a person's access is two lookups: their one Level 1 group, and that group's one
  Level 0 parent plus its own horizontal extras — never a chain.
- One shared model and one shared claim (`groups`) across every app, instead of bespoke logic
  invented per integration.
- Direct NIST/INCITS mapping if this cluster is ever subject to a real compliance review.

**Negative / trade-offs:**
- More Level 1 groups over time than a deep hierarchy would need, by design — each new job
  function is a new flat group, not a new link in a chain.
- The max-depth-1 rule only holds if something enforces it — hence the OPA guardrail above;
  without it, this degrades into exactly the deep-inheritance problem it replaces the first
  time someone nests a subgroup under a subgroup for convenience.
- Widens blast radius short-term: four services go from "no RBAC" or "binary admin gate" to
  a real permission model in one initiative, and one stage touches the only control-plane
  node's `kube-apiserver` flags directly.
