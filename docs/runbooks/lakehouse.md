# Lakehouse operations

The deployed slice is Dagster in namespace `lakehouse`, CloudNativePG for Dagster metadata, and the `lakehouse-data` bucket on the existing SeaweedFS S3 service. Spark, dbt, Delta Lake and Unity Catalog remain source prototypes and are not deployed. Dagster is available at `https://lakehouse.homelab.data-harness.org` through the shared Keycloak edge client and OPA; only `platform-admin` and `app-operator` may enter. The workload uses the upstream `dagster/dagster-k8s` image, whose tagged digest Renovate can update.

Flux fetches the public `bibAtWork/lakehouse` source repository at the commit pinned in `cluster/base/applications/lakehouse/source.yaml` and generates the code and configuration ConfigMaps from its root Kustomization. To deploy a code change, merge it in that repository, then update the commit pin here. Reloader restarts the Dagster Deployment after the ConfigMaps change. There is no application image build or GitHub package credential.

The `lakehouse-s3` Secret is encrypted with SOPS under `cluster/base/secrets/`. It contains an identity limited to `Read`, `Write`, and `List` on `lakehouse-data`. The same credential is part of SeaweedFS's static `seaweedfs_s3_config` in its encrypted Secret. This duplication is required by the current static S3 identity configuration and the Kubernetes namespace boundary; the SeaweedFS administrator credential is **not** copied into `lakehouse`.

To rotate the lakehouse S3 key, use the offline SOPS age key to edit **both** `cluster/base/infrastructure/01-seaweedfs/s3-secret.yaml` and `cluster/base/secrets/lakehouse-s3-secret.yaml` in one PR. Generate a fresh random access key and secret key, update the `lakehouse` identity in the JSON configuration, and put matching values in the application Secret. Keep the identity actions scoped to `lakehouse-data`. After Flux reconciles, confirm the Dagster pod restarts and materialize `sample_bronze_data`; it should create `bronze/sample_data.csv` in the bucket. Never place decrypted manifests or credentials in Git, PR text, or shell history.

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
