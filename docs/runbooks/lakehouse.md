# Lakehouse operations

The deployed slice is Dagster in namespace `lakehouse`, CloudNativePG for Dagster metadata, and the `lakehouse-data` bucket on the existing SeaweedFS S3 service. Spark, dbt, Delta Lake and Unity Catalog remain source prototypes and are not deployed. Dagster is available at `https://lakehouse.homelab.data-harness.org` through the shared Keycloak edge client and OPA; only `platform-admin` and `app-operator` may enter. The workload uses the upstream `dagster/dagster-k8s` image, whose tagged digest Renovate can update.

The real-estate BI schedule runs at 04:00 UTC. Bronze pages the existing screener's read-only export, including full stored descriptions, into SeaweedFS. Silver deduplicates current records before extracting additional property facts from descriptions with Gemini 3.5 Flash-Lite. The model can fill only missing schema fields, with verbatim source quotes and local value/unit checks; unchanged descriptions use a SeaweedFS cache. Gold stores KPI objects and publishes daily aggregate samples to VictoriaMetrics. Grafana provisions `real-estate-lakehouse-bi` from Git, including Metabase-style new-property and average asking-price-per-m² views by three-digit postcode, first-seen month and quarter. These cohorts cover properties still present in the screener's current export; removed listings from the old Metabase data source are not reconstructed. The dashboard links to the screener's property list for record-level drilldown. The screener remains the sole property-site collector. Check Dagster's three asset materializations, `gold/screener/current.json`, and the Gold snapshot-age panel when the dashboard is empty or stale. The lakehouse reads the screener web Service and writes aggregate samples to VictoriaMetrics; no screener database credential is copied into this namespace.

Silver sends the complete description to Google's Gemini API on a cache miss. Its API key is the SOPS-encrypted `google-gemini-api-key` Secret in the `lakehouse` namespace. The `lakehouse-bi-egress` policy allows HTTPS only to `generativelanguage.googleapis.com` for this external call. HTTP 429 after bounded retries, the 250-new-call budget, or the 300-second enrichment budget leaves uncached fields empty so Silver and Gold can publish; Dagster metadata and the Grafana skipped-enrichment panel show the affected property counts. A missing key or other API failure still fails Silver without replacing the existing Gold object. Do not log descriptions or credentials when investigating errors. The cache and Silver evidence contain quoted listing text and stay in `lakehouse-data`.
Both the SRE agent and lakehouse Kustomizations reference the same encrypted source file under `cluster/base/secrets`; Kustomize renders the Secret into each namespace. Rotate its value once in that source file. Lakehouse has its own L7 DNS rule so Cilium can resolve the named HTTPS destination; the cluster-wide L4 DNS rule excludes lakehouse to avoid shadowing the DNS proxy.

Flux fetches the public `bibAtWork/lakehouse` source repository at the commit pinned in `cluster/base/applications/lakehouse/source.yaml` and generates the code and configuration ConfigMaps from its root Kustomization. To deploy a code change, merge it in that repository, then update the commit pin here. Reloader restarts the Dagster Deployment after the ConfigMaps change. There is no application image build or GitHub package credential.

The `lakehouse-s3` Secret is encrypted with SOPS under `cluster/base/secrets/`. It contains an identity limited to `Read`, `Write`, and `List` on `lakehouse-data`. The same credential is part of SeaweedFS's static `seaweedfs_s3_config` in its encrypted Secret. This duplication is required by the current static S3 identity configuration and the Kubernetes namespace boundary; the SeaweedFS administrator credential is **not** copied into `lakehouse`.

To rotate the lakehouse S3 key, use the offline SOPS age key to edit **both** `cluster/base/infrastructure/01-seaweedfs/s3-secret.yaml` and `cluster/base/secrets/lakehouse-s3-secret.yaml` in one PR. Generate a fresh random access key and secret key, update the `lakehouse` identity in the JSON configuration, and put matching values in the application Secret. Keep the identity actions scoped to `lakehouse-data`. Reloader restarts the SeaweedFS filer when its S3 Secret changes and restarts Dagster when its Secret changes. Confirm both rollouts before materializing `sample_bronze_data`; it should create `bronze/sample_data.csv` in the bucket. Never place decrypted manifests or credentials in Git, PR text, or shell history.

To inspect rollout and failures:

```sh
flux get kustomizations -A
kubectl -n lakehouse get pods,svc,cluster
kubectl -n lakehouse describe pod -l app=lakehouse-dagster
kubectl -n lakehouse logs deployment/lakehouse-dagster -c web
kubectl -n lakehouse logs deployment/lakehouse-dagster -c daemon
kubectl -n seaweedfs logs job/seaweedfs-bucket-init
```

The `lakehouse-pg` PVC uses Longhorn. It has no dedicated offsite recovery workflow yet, so do not treat Dagster history or objects in `lakehouse-data` as recoverable from offsite backups. Add both to the recovery system before putting irreplaceable data in the lakehouse.
