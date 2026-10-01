# Backlog

Open work, verified against the repository and cluster on **2026-09-28**. Priorities
are maintainability, efficiency, then security. Completed work belongs in commit
and PR history, architecture decisions and runbooks, rather than this list.
Remove an item when its completion criterion is met; record verification in its PR.

## Private security review

`docs/security-review.md` is local, gitignored and never committed. Do not copy
its findings, resource names or exploit details into this public file, commits or
PR descriptions. Public items below retain only previously published concerns.

## Maintainability

### Credential rotation reminders and verification

**Open.** `bootstrap/scripts/rotate-secrets.py` provides rotation operations, but
there is no tracked rotation schedule/log or scheduled reminder in `.github/`.
Automatic TLS and bound ServiceAccount token rotation do not cover manually
managed credentials.

Add an owner, interval and last verified rotation date for each manual credential,
then produce reminders without changing credentials unattended. Pair each rotation
with a round-trip check of the new credential and proof that all consumers updated.

**Complete when:** overdue rotations produce a deduplicated reminder, and the
runbook records successful post-rotation verification and consumer checks.

### Production recovery and Talos rollback rehearsal

**Open.** Scheduled recovery points, promotion, retention, isolated restore tests
and AWS/PITR drills exist. The steps that replace a production volume or database
are still marked **not drilled** in [the recovery runbook](runbooks/backup-recovery.md).
There is also no documented or rehearsed `talosctl rollback` procedure in the repo.
Lowering a version pin and running an upgrade is a different operation.

Plan a controlled recovery rehearsal with an isolated application cutover and a
separate Talos rollback exercise. Include maintenance timing, stopping writers,
application-level checks, escrow access and the route back if validation fails.
The node currently runs Talos v1.14.1 and Kubernetes v1.37.0; the old claims that
upgrades never ran or that it still runs v1.13.6 are obsolete.

**Complete when:** both procedures have dated evidence covering the final cutover
or rollback and the application checks, and their runbooks reflect what was tested.

### End-to-end SSO smoke tests

**Needs verification.** The earlier review recorded incomplete interactive login
coverage. Declarative client/RBAC checks and Argo admission dry runs do not prove
a browser or CLI login. No new login evidence was produced during this cleanup.

Repeat login and role checks for Grafana, Paperless, Immich and the Kubernetes CLI,
including an ordinary user and an administrator. Use the current Keycloak clients;
KubeOpenCode has been retired and is outside the scope.

**Complete when:** dated results show successful login and the expected permissions
for each supported role, including denied access for an unauthorized role.

### Controller progress after API-server restarts

**Needs rehearsal.** A prior incident left system-upgrade-controller Running but
without functioning watches. Current Flux and upgrade alerts cover availability,
reconciliation failures, stale gates and stuck Jobs; that does not demonstrate
that every healthy-looking controller resumes work after an API-server restart.

During a planned restart, observe a new reconciliation or scheduled operation for
each critical controller. Add a progress signal only where the exercise exposes a
remaining gap, using bounded thresholds and an alert for a stale signal itself.

**Complete when:** the rehearsal demonstrates resumed controller work, and any
observed silent stall has a tested alert or documented recovery action.

### Deferred: read the AWS vault's data back

**Deferred.** Promotion and AWS retention still run metadata-only `restic check`.
Local retention reads a rotating daily 1/7 data subset; AWS restore drills read
selected recovery points, but neither checks all older vault packs on a schedule.

Evaluate a bounded weekly AWS data check, such as `--read-data-subset=5G`, with a
success timestamp, failure/staleness alerts and a rehearsed repair procedure. A
sampling strategy must advance across runs; using Sunday's day-of-week slice
would reread the same subset every week. Revisit after restic format/copy changes,
a failed AWS restore, or a lapse in restore-drill coverage.

**Complete when adopted:** data-read coverage, transfer budget, alert behavior and
a recovery procedure are documented and demonstrated. This remains an explicit
deferral, not a missing step in the existing promotion contract.

## Efficiency

### Measure and rightsize individual workloads

**Open.** The Resource efficiency dashboard and
[rightsizing runbook](runbooks/resource-rightsizing.md) exist. The 2026-09-20
baseline showed CPU request headroom and limited aggregate memory-request margin;
it does not authorize blanket reductions. Retiring KubeOpenCode and correcting
Trivy scan capacity changed the baseline again.
The [2026-09-29 live commitment snapshot](runbooks/resource-rightsizing.md#2026-09-29-live-commitment-snapshot)
reconciles limits and requests with node accounting; it does not replace the
complete seven-day usage window needed for a rightsizing decision.

Collect a new complete seven-day window, including backup, scan and upgrade Jobs.
Rank remaining workloads by request-to-p95 ratio, then change one at a time with
the previous values and measured peak memory recorded in its PR.

**Complete per workload:** one operational week demonstrates the intended saving
without OOM, eviction, startup or latency regression. Keep the baseline current
after service or node-profile changes.

## Security

### Review Immich's major-version update

**Open decision.** The repository pins chart 0.12.0 and app v2.7.5. The application
tag is now tracked by Renovate, grouped with the chart/database updates for manual
review; the earlier claim of missing tracking is obsolete. The dependency dashboard
currently offers [the v3 update](https://github.com/bibAtWork/Edge_GitOps/pull/673).

Review the proposed migration and compatibility with the configured CNPG database,
OAuth and storage. Validate a recovery point by restoring it, exercise the update
on a disposable copy, and use the major-update checklist before production rollout.

**Complete when:** a reviewed decision is recorded; if approved, the migration and
application checks pass and the deployed version is documented.

### Harden application workload security contexts

**Open.** `require-readonly-rootfs` remains an **Audit** policy. Image-tag, registry,
recovery-boundary and fsGroup rules already deny violations; the old statement
that all Kyverno policies only audit is incorrect. Root-filesystem enforcement
needs its own compatibility evidence.

Prioritize the Immich server and machine-learning workloads, then the
VictoriaMetrics application components. For each container, test a non-root
user, disabled privilege escalation, dropped capabilities, a RuntimeDefault
seccomp profile and a read-only root filesystem with explicit writable mounts.
Check startup, upgrades, background jobs and restore paths before changing the
next workload. Record a specific reason for any control that cannot be enabled;
do not infer compatibility from a chart's default settings.

**Complete per workload:** the supported controls are enforced and normal and
Job activity passes, or an evidence-backed exception is recorded. A later
read-only-root policy enforcement change additionally needs regression fixtures
and successful live admission checks for both compliant workloads and exceptions.

### Review bound operator RBAC

**Open.** Audit the effective permissions granted to the VictoriaMetrics,
CloudNativePG and Longhorn controllers. Start from their RoleBindings and
ClusterRoleBindings, then compare each bound rule's verbs and resources with
the controller features actually enabled in this cluster. Prioritize broad
secret access and wildcard grants, but assess read-only rules separately from
write permissions. Avoid treating an unbound role report as a live grant.

Use supported chart values or upstream-scoped roles when reducing permissions.
Document permissions that remain necessary for enabled features and verify
reconciliation, upgrades, backup and restore paths after each change.

**Complete per operator:** the bound-role review and decisions are recorded,
unneeded grants are removed where supported, and the controller's operational
checks still pass.

### Deferred: upstream maintenance signal

**CVE change alerts implemented.** The image CVE collector now keeps a durable
baseline, emits new Critical CVE and fix-available events, and warns when its
inventory is stale. Standing per-image alerts remain as a weekly repeating
backlog; details and delivery limits are in [renovate-trivy-flow.md](renovate-trivy-flow.md).

An upstream-maintenance signal should measure the upstream project's latest
stable release rather than the age of the chart's appVersion. If adopted, pair it
with Renovate PR age and a last-successful-check signal so failed collection is
visible. Coverage and thresholds must account for normal project release cadence.

**Complete when adopted:** each signal demonstrates actionable changes, suppresses
the known false positives, and alerts when its own collection stops.
