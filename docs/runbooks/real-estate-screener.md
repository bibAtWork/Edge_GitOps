# Real estate screener

URL: <https://screener.homelab.data-harness.org>. Sign in through Keycloak with
an app-operator/platform-admin account (or the existing administrator identity).
The Gateway terminates TLS using the existing wildcard certificate; external-dns
manages the route's hostname. LAN/Tailscale reachability follows the other apps.

Flux owns this deployment on `ops/talos_linux`, in both cluster profiles.
`cluster/base/applications/real-estate-screener` contains the workloads and search
configuration; `deferred` contains PostgreSQL, Gateway and network-policy resources
applied only after the platform operators are ready.

## Registry access

The workload manifests pin the tested GHCR digest from the application build.
It currently requires an amd64 node and a private GHCR pull credential. The
SOPS-encrypted `ghcr-pull` source is stored under `cluster/base/secrets` and
rendered into both application namespaces. See the SRE agent README for the
token requirements and SOPS rotation procedure. The existing overlay build, SOPS
enforcement, and platform contract checks reject missing or plaintext Secrets.
Never commit a plaintext registry token to this public GitOps repository.

## Database and migrations

CloudNativePG creates `screener-pg`, the database/owner `screener`, and its
`screener-pg-app` credentials Secret. Workloads read the Secret's `uri` key.
No manually configured database password is required. PostgreSQL uses a 10 GiB
`longhorn-db` volume with Retain semantics. Init containers run the application's
idempotent, advisory-lock-protected migrations before web and collection jobs.
This avoids immutable migration Job updates during later GitOps releases.

The screener database uses the platform's CNPG recovery pipeline. Its barman
sidecar archives WAL and daily base backups to a dedicated `screener-recovery`
SeaweedFS bucket. The credential in this application namespace has access only
to that bucket; it cannot read or delete the shared `recovery` repository. The
daily `recovery-point-real-estate-screener` workflow copies the base backup and
required WAL into restic, restores a scratch PostgreSQL cluster, and checks
that the recovered listing tables contain data. Only a validated point is
eligible for the 03:00 UTC offsite promotion. The important profile requires a
24-hour RPO, 8-hour RTO, and monthly restore proof. Longhorn Retain remains a
separate safeguard against accidental PVC deletion.

Acceptance after deployment: the new archive credential can access
`screener-recovery` but receives access denied for `recovery`; the CNPG cluster
reports WAL archiving healthy; a manual recovery-point Workflow reaches
`Succeeded` with a `VALIDATED` record; and its promotion reaches
`REMOTE_VERIFIED`. Keep the database and its archive until those checks pass.
Do not remove the namespace or CNPG cluster as a routine application rollback.

## Collection

The configured searches are Eigenheim (Haus) near Waldenbuch and Investment
(Heilbronn), each across ImmoScout24, Immo SWP, and Kleinanzeigen. The two
Immowelt searches are paused because the portal returned HTTP 403 to both the
collector and a direct probe on 2026-10-20. The two Sparkasse URLs are paused
because `/immobilien/treffer` now resolves to the editorial homepage without
listing cards. Previous observations remain in PostgreSQL. The original URLs
remain in Git history; do not retry blocked requests more aggressively.

Re-enable either provider only after a current portal URL returns real search
results and two consecutive manual collection runs produce successful search
records and usable detail evidence. A zero-result search is acceptable only when
the response is a real results page. For Immowelt, check that the portal permits
the collector's access before restoring the schedule.
Edit `cluster/base/applications/real-estate-screener/config/config.json` to
change the portal search URLs. Collection runs every 30 minutes, is bounded to
20 detail requests per run, and refreshes details after 24 hours. Fredy's Immo
SWP adapter provides search sightings only, so those listings have no detail
refresh.
Macro imports run daily at 04:15 Europe/Berlin, covering German bond/mortgage
series and DE/AT/FR European data where published. The default history starts
in 2020. The ConfigMap is hashed by Kustomize, so CronJobs get new configuration
on subsequent runs. No collection result is fabricated if a portal refuses access.
The `macro-bonds-backfill` command is for a one-off load of all available daily
Bundesbank 5/10/15/20-year yields from 1990 onward; the normal daily macro job
keeps its 2020 request window. Historical observations are browsable in the
screener's macro table with metric **Government bonds**, country **DE**, and
**All historical observations** selected.

Exact mortgage terms beyond the official maturity bands and regional announced
auction/cancellation history still need the application's documented import
formats; there is no built-in complete public auction feed.

## Checks and updates

Kubernetes checks `/health/live` for process liveness and `/health/checks` for
readiness. The latter verifies migrations and executes real queries against
listing, macro and auction views. Empty data is healthy on first deployment.
The central OTel gateway scrapes `/metrics` every 30 seconds and exports it to
VictoriaMetrics. These endpoints are available internally at
`http://screener-web.real-estate-screener.svc:3000`; the public URL remains gated
by OIDC. Only collection workers get general internet HTTPS egress.

`screener-auctions` runs daily at 04:45 Europe/Berlin and imports the current
public search overview for all 16 Bundesländer. Each state has its own batch and
source key; a state with a verified zero-result overview counts as a successful
import, while any failed state makes the Job fail after the others are attempted.
Check the latest Job, `/api/appointments`, and state freshness metrics after runs.
The job does not fetch court detail pages or reconstruct complete historical auctions.

The [Real Estate Screener BI dashboard](https://grafana.homelab.data-harness.org/d/real-estate-screener-bi)
uses VictoriaMetrics aggregates from that scrape. It shows the property inventory, asking-price
medians, provider and energy-class mix, upcoming auction notices by Bundesland, and current official
macro observations. These are observed screener records, not market-wide counts. The auction
panels include only the latest successful overview for states refreshed within 48 hours.
The **Fresh state overviews (of 16)** and per-state freshness panels expose missing or
stale coverage; do not treat a partial count as a national total.

The `Screener deployment check` workflow runs after relevant merges on the
existing `[self-hosted, homelab]` runner. It waits for Flux to apply the pushed
revision, checks database and web readiness, then runs HTTP functionality checks
through a temporary local port-forward. A queued workflow means no matching
runner is available, not proof that the service is running.

```bash
kubectl get cluster,pods,cronjobs -n real-estate-screener
kubectl get httproute,securitypolicy -n real-estate-screener
kubectl logs deployment/screener-web -n real-estate-screener
kubectl create job --from=cronjob/screener-macro screener-macro-manual -n real-estate-screener
```

For an update, use a new successfully tested image digest in the web, collector
and macro containers **and their migrate init containers**, then merge a PR.
Check migration compatibility before reverting to an older application image.
