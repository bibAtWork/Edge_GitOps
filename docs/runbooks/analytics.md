# Lean analytics stack

The live Flux source tracks `ops/talos_linux`. Both cluster profiles apply an analytics namespace and a **suspended** child Kustomization. The child waits for `config` and owns its workloads and Secrets. Merging this PR does not deploy those workloads.

## Components and capacity

| Component | Placement | CPU request | RAM request / limit |
| --- | --- | --- | --- |
| Lakekeeper | One Iceberg REST catalog | 100m | 128Mi / 512Mi |
| Trino | One coordinator/worker pod | 2 | 6Gi / 6Gi |
| Lightdash | One deployment | 200m | 512Mi / 1536Mi |
| Dagster | Webserver, daemon, code server | 300m total | 768Mi / 2304Mi total |
| PostgreSQL | Existing CNPG operator, one new instance | 100m | 512Mi / 1Gi |
| Pipeline | At most one temporary Kubernetes Job | 500m | 512Mi / 2Gi |

Steady-state requests are approximately **2.7 CPU / 7.9Gi RAM**; one pipeline adds 0.5 CPU / 512Mi. Memory limits total approximately **11.3Gi idle / 13.3Gi with a run**. Reserve roughly 14Gi free, in addition to existing workloads and node overhead. These are configured budgets, not measurements. Trino has a 4Gi heap, a 2Gi query-memory cap and a 2 CPU limit.

Each Python pod has an init container requesting 250m CPU / 256Mi RAM, limited to 1 CPU / 1Gi RAM. Kubernetes schedules the maximum of init and application requests per resource, not their sum: a cold start raises the three Dagster pods' CPU reservation to 750m total. Each Python pod also requests 512Mi of ephemeral storage with a 2Gi limit for downloaded dependencies and temporary data. Capacity planning must include concurrent downloads after node restarts.

PostgreSQL needs one retained 10Gi `longhorn-db` PVC (this repository's class has one replica). Iceberg objects grow in the existing SeaweedFS `analytics` bucket. PostgreSQL is application metadata storage; Trino queries Iceberg. DuckDB is temporary storage inside demo jobs. Existing Keycloak, SeaweedFS, OPA, VictoriaLogs and VictoriaMetrics remain available; no search service or additional operator is introduced.

## Upstream images and startup dependencies

All Python workloads use **`docker.io/library/python:3.12.12-slim-bookworm`**. There is no custom image, image build, registry push or image publication step. Pipeline code, dbt files and dependency locks are delivered through a ConfigMap generated from Git.

An init container runs `analytics/install-runtime.sh`, creates a virtual environment in a pod-local `emptyDir`, and installs the committed dependency lock with **`--require-hashes --only-binary=:all:`**. Main containers mount that runtime read-only and run as UID 1000 with a read-only root filesystem. Init containers receive neither application credential environment variables nor Kubernetes API tokens. Only the Dagster webserver/daemon main containers receive scoped API tokens needed to launch Jobs.

`requirements-control.lock` keeps dbt, dlt and Arrow out of the three always-running Dagster pods. Pipeline and catalog-bootstrap Jobs use `requirements.lock`. There is no persistent dependency cache and nothing is installed on nodes. Each new pod downloads again; existing pods do not download for each query or asset step. Startup therefore depends on PyPI availability and is slower than a prebuilt runtime. Cilium grants these pods HTTPS egress only to `pypi.org` and `files.pythonhosted.org` for dependency installation.

To update dependencies, edit the corresponding `.txt` input and regenerate its committed lock using uv with Python 3.12 resolution:

```sh
uv pip compile --python-version 3.12 --generate-hashes --no-header analytics/requirements.txt -o analytics/requirements.lock
uv pip compile --python-version 3.12 --generate-hashes --no-header analytics/requirements-control.txt -o analytics/requirements-control.lock
```

Review lock changes and let CI exercise installation. Do not install additional packages dynamically from pipeline code. ConfigMaps are bounded to 1MiB; the current code and two locks fit in one. Coordinate code/config changes with active runs because these mounts are updated by Kubernetes; Reloader restarts the control plane when configuration changes.

## Secrets and activation

Secrets are Kubernetes manifests under `cluster/base/applications/analytics/deferred/secrets`, encrypted with the repository's SOPS age recipient. Flux decrypts and reconciles them. The committed files contain **encrypted inactive template values**, which must be regenerated before activation.

There is no `analytics/prepare.py`, imperative Secret creation or duplicated `analytics-run-env` Secret. Workloads and launched Jobs reference the canonical Secrets directly. CNPG creates Lakekeeper's application Secret. Its managed roles consume the dedicated Dagster/Lightdash Secrets. The analytics S3 Secret is a namespaced copy of the existing SeaweedFS admin identity, generated from the same bootstrap config; no separate S3 identity is created.

1. Use your existing **gitignored** `bootstrap/config.json` and existing age private key. Set `analytics.enabled` to `true`. If you already created the Keycloak service client, set `analytics.lakekeeper_client_secret` to its current secret; otherwise leave it empty and bootstrap generates one to configure on that client. Run the existing `python3 bootstrap/scripts/apply-config.py`. It generates the analytics credentials, encrypts the manifests and preserves configured/decryptable values, including stable signing and encryption keys. Review and commit the encrypted changes. Do not use a fresh blank bootstrap config against an existing cluster.
2. Create the Keycloak `homelab` realm client **`analytics-engine`** with the same secret. Enable client authentication and service accounts/client-credentials flow; disable browser standard flow, direct password grants and implicit flow. Add a default client scope `lakekeeper` with a hardcoded access-token audience `analytics-lakekeeper`; do not attach that scope/audience to browser clients. Lakekeeper requires that audience, the scope and `azp=analytics-engine`. Configure the client using the decrypted OAuth Secret on your trusted machine; never copy its value into tracked YAML.
3. Merge the PR, then set `spec.suspend: false` in `cluster/base/applications/analytics/flux.yaml` and commit. Flux applies Secrets and starts PostgreSQL, TLS and workloads. Confirm the CNPG cluster and Lakekeeper become ready. Lakekeeper must be able to discover the existing external HTTPS issuer from its pod; no TLS verification is disabled.
4. Read [Lakekeeper's license/terms](https://docs.lakekeeper.io/about/license/). The following manual Job explicitly accepts them and creates the catalog and `analytics` bucket:

   ```sh
   kubectl apply -f analytics/bootstrap-job.yaml
   kubectl wait -n analytics --for=condition=complete job/analytics-bootstrap --timeout=600s
   kubectl logs -n analytics job/analytics-bootstrap
   ```

   The Job checks bootstrap status and preserves an existing warehouse. Delete the completed Job before a rerun. It installs pinned dependencies at startup like other Python Jobs and does not modify the global SeaweedFS bucket-init Job.
5. Open Dagster using `kubectl port-forward -n analytics svc/dagster-webserver 3000:3000` and launch **`analytics_demo`**. No schedule is enabled. The synthetic job uses dlt to normalize three events into temporary DuckDB, writes Iceberg `raw.demo_events` through PyIceberg/Lakekeeper, then runs dbt/tests to build `iceberg.marts.event_summary`. Repeated demo runs replace synthetic input. Real source extraction requires source-specific credentials/egress and durable incremental state; the demo's local dlt state is ephemeral.
6. Open Lightdash with `kubectl port-forward -n analytics svc/lightdash 8080:8080`; create its initial administrator and a project. Configure Trino host `trino.analytics.svc`, port `8443`, HTTPS, catalog `iceberg`, schema `marts`, user `analytics-bi` and its password from `analytics-trino-client`. Lightdash trusts the Trino CA through `NODE_EXTRA_CA_CERTS`.

To rotate an application credential, change its value in the existing private bootstrap config, run `apply-config.py` and commit the encrypted changes. Trino password hashes are regenerated only for changed passwords. SeaweedFS rotation must update every generated consumer from the same canonical values, including analytics. Coordinate a Keycloak client-secret rotation with its encrypted analytics copy. Keep Lakekeeper's encryption key and Lightdash's signing secret stable unless following their documented migration procedures.

## Transformations, metrics and dashboards as code

`analytics/dbt` holds SQL, tests, Lightdash metrics and chart/dashboard YAML under `lightdash/`. Code/config changes are GitOps updates and do not require building images. dbt and dlt are runtime libraries, not separate deployed services.

For local dbt/Lightdash CLI use, copy `profiles.yml` to an **untracked** local directory. When using `kubectl port-forward -n analytics svc/trino 8443:8443`, set its host to `localhost`, point `cert` to a local copy of the public `trino-tls` CA and supply `TRINO_DBT_PASSWORD` securely. Keep service DNS in the in-cluster profile. Use dbt credentials for transformations and BI credentials for Lightdash queries.

Install a matching Lightdash CLI, log in to the port-forwarded instance, select the project and create an `analytics` space. From `analytics/dbt`, with the local profiles directory configured:

```sh
lightdash deploy
lightdash upload --dashboards analytics-smoke-test --charts demo-event-counts
```

The dashboard should show `purchase: 2` and `signup: 1`. Review SQL/YAML through PRs; upload reviewed content. `lightdash download` captures UI edits into Git. Initial project setup and content publication remain explicit CLI operations; Flux does not publish dashboard YAML automatically. [Official CLI examples](https://github.com/lightdash/lightdash-templates/tree/main/templates/events_explorer) describe this workflow.

## Native Trino permissions

Trino reads `deferred/config/trino-rules.json` through a ConfigMap using its supported file-access-control plugin. It refreshes rules every 30 seconds. There is no analytics Rego, OPA callback or OPA REST listener change.

| Identity | Permissions |
| --- | --- |
| `analytics-dbt` | Read raw; create/manage staging and marts |
| `analytics-bi` | Read marts; no writes |
| Other identities | Denied |

Catalog/table permissions are explicit, including denial of the default `system` catalog. Queries are limited to the two service identities; impersonation and catalog procedures are disabled. Trino 483 file access control has no table-procedure rules: `ALTER TABLE ... EXECUTE` cannot be restricted independently by this policy. Keep these accounts for trusted services; stronger procedure controls require another access-control implementation. Built-in functions and Trino's `information_schema` semantics are provided by Trino. Metadata visibility is not equivalent to hiding all catalog metadata. No row filters or sensitive-column masks are configured.

CI launches the **same upstream Trino version**, uses disposable memory tables under the `iceberg` catalog name, then applies the deployed rules unchanged. It checks BI reads/denied raw reads/denied writes, dbt raw reads/staging writes/denied raw writes, and unknown-user denial. This tests our intended configuration through Trino's SQL interface, without recreating its authorization engine. Authentication, TLS and Iceberg connectivity require the cluster smoke test. [Trino file access-control documentation](https://trino.io/docs/current/security/file-system-access-control.html) describes rule semantics and defaults.

## Remaining boundaries and gaps

* Lakekeeper OSS has **`allowall` authorization** with restrictive OIDC authentication and private network access. Trino and ingestion Jobs are trusted catalog administrators. There are no native per-user catalog grants; those require adding OpenFGA. OPA is not presented as Lakekeeper's authorization backend. This is not equivalent to Unity Catalog.
* Lightdash queries use one shared BI identity, so dashboard users are not propagated into Trino policy. There is no global cross-engine enforcement, business glossary/search, automated classification or unified column lineage. Dagster asset relationships/dbt artifacts are only a starting point.
* SeaweedFS retains its shared admin key per repository architecture. Compromised data-plane pods could access other buckets. Cilium and trusted pipeline code are the boundary; there is no bucket-scoped authorization or STS credential vending. Lightdash has no S3 credentials.
* No public routes or browser SSO are introduced. Dagster OSS has no built-in user authentication in this deployment; use trusted `kubectl` port-forward access only. Lightdash uses native login and HTTP port-forward settings (`SECURE_COOKIES=false`). Add the repository's Gateway/OIDC policies and HTTPS settings before exposing either UI.
* Trino/PostgreSQL are single-instance. Analytics is not yet registered with the ADR-012 recovery system. Back up PostgreSQL, signing/encryption Secrets and Iceberg objects/metadata together and test restoration before storing irreplaceable data. SOPS protects credentials in Git; it does not replace Kubernetes RBAC or etcd encryption.
* Existing infrastructure collects pod logs. No new VictoriaMetrics scrape rules, application alerts or operational dashboards are added. Dagster compute-log persistence is disabled; Job pods expire after one hour, while VictoriaLogs follows existing retention.

## Validation and rollback

CI installs hash-pinned dependencies in the upstream image using the actual init script, validates Dagster definitions/launcher schema and parses dbt. It renders and schema-checks both inventories and the manual bootstrap Job, then exercises the permission expectations through Trino. Existing CI checks both profiles, secret encryption/bootstrap coverage and platform contracts. No image is built or published.

For rollback, suspend analytics Flux reconciliation, scale the six application deployments to zero and stop active Jobs. Suspension alone does not stop pods. Keep PostgreSQL/PVCs, SeaweedFS objects and encrypted Secrets intact. Do not delete the namespace or prune the database without a verified backup.
