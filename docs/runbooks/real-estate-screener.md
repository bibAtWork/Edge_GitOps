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

The image is pinned to the tested GHCR digest from the application build:
`ghcr.io/bibatwork/real-estate-screener@sha256:83a22c119a446b7847c8e91f4e86be1b6bfc7a86fbc4fe1b51f94834f14ccb8a`.
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

The deployment has **no configured backup or offsite restore procedure yet**.
Retain is protection against accidental volume deletion, not against disk failure.
Archived listing snapshots, renovation history and imported auction records may
be impossible to reconstruct later. Integrate this database with the platform's
CNPG recovery pipeline before relying on it as the sole historical record.
Do not remove the namespace or CNPG cluster as a routine application rollback.

## Collection

The initial search is Berlin houses for sale on ImmoScout24, every 30 minutes.
Edit `config/config.json` to select the desired Fredy searches. Collection is
bounded to 20 detail requests per run and refreshes details after 24 hours.
Macro imports run daily at 04:15 Europe/Berlin, covering German bond/mortgage
series and DE/AT/FR European data where published. The default history starts
in 2020. The ConfigMap is hashed by Kustomize, so CronJobs get new configuration
on subsequent runs. No collection result is fabricated if a portal refuses access.

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

`screener-auctions` is staged with `suspend: true` until the image containing the
`auction-sync` command is published and pinned in all screener workloads. After that
image rolls out, run a one-off Job from the CronJob, check its imported count and the
Auction Appointments page, then set `suspend: false` in Git. The job reads the public
Berlin search overview; it does not fetch court detail pages.

The [Real Estate Screener BI dashboard](https://grafana.homelab.data-harness.org/d/real-estate-screener-bi)
uses VictoriaMetrics aggregates from that scrape. It shows the property inventory, asking-price
medians, provider and energy-class mix, upcoming Berlin auction notices, and current official
macro observations. These are observed screener records, not market-wide counts. The auction
panels use only the latest successful Berlin search overview; an import failure leaves the last
successful data in place and should be visible through the import timestamp.

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
