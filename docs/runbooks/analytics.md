# Lean analytics stack

The existing Flux source tracks `ops/talos_linux`; this stack targets that branch and both cluster profiles. The root applies only the analytics namespace and a **suspended** child Flux Kustomization. The child waits for `config`, which already waits for the operators. No analytics workloads start merely by merging this change.

## Components and sizing

| Component | Placement | CPU request | RAM request / limit |
| --- | --- | --- | --- |
| Lakekeeper | One deployment, Iceberg REST catalog | 100m | 128Mi / 512Mi |
| Trino | One pod, coordinator also runs queries | 2 | 6Gi / 6Gi |
| Lightdash | One deployment | 200m | 512Mi / 1536Mi |
| Dagster | Webserver, daemon, code server; same image | 300m total | 768Mi / 2304Mi total |
| PostgreSQL | Existing CNPG operator, one new instance | 100m | 512Mi / 1Gi |
| Pipeline | At most one temporary Kubernetes Job | 500m | 512Mi / 2Gi |

Baseline requests are about **2.7 CPU and 7.9Gi RAM**; one pipeline adds 0.5 CPU and 512Mi requested. Memory limits total about **11.3Gi idle / 13.3Gi with a run**. Reserve roughly 14Gi free for this stack plus capacity for existing workloads, Kubernetes and Longhorn. These are configured budgets, not measured usage. Trino has a 4Gi heap and 2Gi query memory cap; large joins may fail rather than expand. Its CPU limit is 2; pipeline CPU limit is 2.

PostgreSQL needs a 10Gi `longhorn-db` PVC; actual disk allocation includes Longhorn replicas. Iceberg objects grow in the existing SeaweedFS `analytics` bucket. No new object store, identity provider, policy engine, metrics backend or log backend is deployed. PostgreSQL is an unavoidable metadata backend for these applications, not a second analytics warehouse. Dagster is three pods but one logical service; dbt and dlt are libraries in its image, with DuckDB only used inside demo jobs.

## Prepare and activate

1. Merge the PR. Confirm `analytics` namespace exists and Flux `analytics` is suspended. Do not add the deferred directory directly to `platform-config`: ownership belongs to the child Flux Kustomization.
2. In the existing Keycloak `homelab` realm, create a confidential OIDC client **`analytics-engine`**. Enable client authentication and service-account/client-credentials flow; disable browser standard flow, direct password grants and implicit flow. Add a default client scope named `lakekeeper` and a hardcoded audience mapper for `analytics-lakekeeper` (include in access tokens). Do not link that scope/audience to browser clients. Lakekeeper requires audience, scope and `azp=analytics-engine`; this client is a trusted catalog administrator. The deployment discovers the existing external HTTPS issuer, so cluster DNS, routing and CA trust must resolve that issuer from its pod.
3. From a trusted machine with Python 3 and `kubectl`, check your context, then run `python3 analytics/prepare.py`. It copies the **existing SeaweedFS admin identity**, creates separate PostgreSQL passwords, two Trino service accounts, a stable Lakekeeper encryption key and Lightdash signing secret. It never prints secret values and preserves existing Secrets. No new SeaweedFS IAM identities are introduced. Securely back up these Secrets; losing Lakekeeper's encryption key prevents reading its stored storage credentials. To place credentials in Git later, encrypt with SOPS and update the repository's bootstrap secret inventory; never commit the script's runtime values unencrypted. For credential rotation, update all dependent Secrets together; rerunning this script intentionally does not rotate them.
4. Build and publish the pipeline image to a registry the nodes can pull from:

   ```sh
   docker build -f analytics/Dockerfile -t ghcr.io/bibatwork/edge-analytics:analytics-v1 .
   docker push ghcr.io/bibatwork/edge-analytics:analytics-v1
   ```

   Make the image public, or configure an `imagePullSecret` on the controller/runner service accounts and code-server pod. The PR CI builds and validates the image but does not publish it. For another image name/version, change **all** Dagster deployment images, the launcher `job_image`, `DAGSTER_CURRENT_IMAGE`, and `analytics/bootstrap-job.yaml`. Use immutable version tags or digests for subsequent releases.
5. Set `spec.suspend: false` in `cluster/base/applications/analytics/flux.yaml` and commit. Flux starts PostgreSQL, TLS certificates and the workloads. Trino may remain unready until the warehouse exists; this does not prevent Lakekeeper being reached. Wait for the `analytics-pg` cluster and Lakekeeper deployment to become ready.
6. Read [Lakekeeper's license/terms](https://docs.lakekeeper.io/about/license/). Applying the following manual job accepts its terms and creates the catalog and bucket:

   ```sh
   kubectl apply -f analytics/bootstrap-job.yaml
   kubectl wait -n analytics --for=condition=complete job/analytics-bootstrap --timeout=180s
   kubectl logs -n analytics job/analytics-bootstrap
   ```

   It checks the bootstrap status and preserves an existing warehouse. The `analytics` bucket is created on SeaweedFS; Lakekeeper validates storage access. A rerun after Job completion requires deleting the completed Job first. It does not modify the existing global SeaweedFS bucket-init job.
7. Open Dagster with `kubectl port-forward -n analytics svc/dagster-webserver 3000:3000`; launch the **`analytics_demo`** job. No schedule is enabled. The synthetic demo uses dlt to normalize three events into temporary DuckDB, writes a real Iceberg `raw.demo_events` table through PyIceberg/Lakekeeper, then runs dbt and its tests to build `iceberg.marts.event_summary`. Repeated runs replace synthetic input. Replace the source function with application database/API extracts and add explicitly scoped source egress policies and credentials before scheduling real ingestion. dbt does the in-warehouse transformations; dlt handles source extraction/state. Neither needs its own permanent deployment.
8. Open Lightdash with `kubectl port-forward -n analytics svc/lightdash 8080:8080` and create the initial administrator. Connect a project to Trino host `trino.analytics.svc`, port `8443`, catalog `iceberg`, schema `marts`, HTTPS and user `analytics-bi`. Retrieve its password from `analytics-trino-client` on your trusted machine; never put it in a tracked profile. Lightdash trusts Trino's CA using `NODE_EXTRA_CA_CERTS`. Its database keeps connection credentials encrypted with the stable Lightdash secret.

## Transformations, metrics and dashboards as code

`analytics/dbt` contains the dbt project, tests, Lightdash metric definitions and a sample chart/dashboard in `lightdash/`. Connect/deploy that dbt project using the Lightdash CLI against the port-forwarded Lightdash instance. Install a CLI version matching the pinned Lightdash release; run `lightdash login http://localhost:8080` and select your project. Project creation/initial connection is a one-time setup operation.

For local dbt/Lightdash compilation, copy `profiles.yml` to an **untracked temporary directory** and change the host to `localhost` when using `kubectl port-forward -n analytics svc/trino 8443:8443`; export the Trino dbt password and copy `trino-tls`'s public `ca.crt` to a local trusted file (set `cert` accordingly). The in-cluster profile and job image must keep the service DNS name. Use the dbt account for model deployment and the BI account for Lightdash queries.

From `analytics/dbt`, with that local profiles directory configured:

```sh
lightdash deploy
lightdash upload --dashboards analytics-smoke-test --charts demo-event-counts
```

Create an `analytics` space before uploading, matching `spaceSlug`. The dashboard should show `purchase: 2` and `signup: 1`. Edit SQL/YAML through PRs and upload reviewed changes. Use `lightdash download` to capture UI edits back into Git. The chart/dashboard YAML follows Lightdash's content-as-code format; it is not automatically applied by Flux. Automated content publication and Lightdash project provisioning are future work. [Official template/CLI examples](https://github.com/lightdash/lightdash-templates/tree/main/templates/events_explorer) describe the upload/download workflow.

## Boundaries and remaining gaps

* Lakekeeper OSS runs **`allowall` authorization** with restrictive OIDC authentication. It is private and accepts one trusted machine client; both Trino and ingestion jobs possess catalog-wide privileges. **It has no per-user catalog grants in this configuration.** Native OSS Lakekeeper authorization requires adding OpenFGA; the Cedar option belongs to Lakekeeper Plus. Existing OPA is used by Trino, not presented as a Lakekeeper authorization backend.
* Trino uses two HTTPS/password service identities. Existing OPA permits dbt access to raw/staging/marts and BI reads to marts; unknown users and BI writes are denied. OPA REST is exposed on the cluster service but Cilium grants analytics only POSTs to the dedicated decision endpoints. SQL grants/impersonation/catalog mutation are not enabled. Lightdash uses one shared BI identity, so individual dashboard users are not propagated into engine policies. `information_schema` metadata is readable by the BI account. No row filters or sensitive-column masks are configured yet.
* SeaweedFS retains its shared admin key, per repository architecture. Data-plane pods can therefore access other buckets if compromised; namespace and pod-label network restrictions are the boundary, not bucket-scoped authorization or STS credential vending. Lightdash receives no direct S3 credentials. Protect trusted pipeline code accordingly.
* There are no public routes and no browser Keycloak SSO here. Dagster OSS has no built-in user authentication in this deployment: use trusted `kubectl` port-forward access only. Lightdash uses native login; its port-forward HTTP setting has `SECURE_COOKIES=false`. Before adding ingress, add the repository's Gateway/OIDC policies and HTTPS settings; never simply expose these services.
* This is an Iceberg catalog, not a Unity Catalog replacement: no business glossary/search, automatic classification, global cross-engine enforcement, durable query audit archive, or unified column lineage. Dagster asset relationships and dbt artifacts provide a limited starting point. There is no Elasticsearch/OpenSearch dependency.
* There is no HA: Trino and the database are single-instance. Back up analytics PostgreSQL, signing/encryption secrets and Iceberg objects/metadata together and test a restore. This PR does **not** register analytics with the existing ADR-012 recovery system. Do not store irreplaceable data before doing so. Keep the warehouse through rollback; disabling deployment does not constitute a backup.
* Pod logs flow through the existing logging infrastructure. There are no new scrape rules, dashboards or alerts for VictoriaMetrics yet; add Trino metrics/JMX and application-specific alerts after measuring usage. Dagster compute-log persistence is disabled to avoid an extra log store; completed job logs disappear after its one-hour TTL (VictoriaLogs retains collected pod logs according to existing retention).

## Validation and rollback

CI renders the opt-in directories, tests OPA allow/deny cases and builds the pipeline image to check pinned dependencies, Dagster launcher schema, asset definitions and dbt parsing. Existing repository CI covers both profiles, SOPS enforcement and platform contracts. These checks cannot prove SeaweedFS compatibility, TLS, OIDC or a live dbt/Lightdash query; the synthetic smoke job above is the acceptance test on the cluster.

For rollback, suspend the analytics Flux Kustomization and scale the six application deployments to zero; stop active Jobs. Suspension alone does not stop running pods. Leave `analytics-pg`, its PVC, SeaweedFS objects and runtime Secrets intact. Do not delete the namespace or prune the database to save RAM without a verified backup.
