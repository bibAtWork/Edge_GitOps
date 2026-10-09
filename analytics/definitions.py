"""Opt-in synthetic smoke pipeline. Replace its source with your application extracts."""
import os
import subprocess

from dagster import Definitions, asset, define_asset_job


@asset
def demo_events():
    # Imports are deferred so the code server does not need data credentials.
    import dlt
    from pyiceberg.catalog import load_catalog

    pipeline = dlt.pipeline(
        pipeline_name="analytics_demo", pipelines_dir="/tmp/dlt",
        destination=dlt.destinations.duckdb("/tmp/analytics-demo.duckdb"),
        dataset_name="demo",
    )
    pipeline.run(
        [{"id": 1, "event_type": "purchase", "amount": 12.0},
         {"id": 2, "event_type": "purchase", "amount": 8.0},
         {"id": 3, "event_type": "signup", "amount": 0.0}],
        table_name="events", write_disposition="replace",
    )
    arrow = pipeline.dataset().events.arrow().select(["id", "event_type", "amount"])
    catalog = load_catalog(
        "analytics", type="rest", uri="http://lakekeeper.analytics.svc:8181/catalog",
        warehouse="analytics",
        credential=f"{os.environ['LAKEKEEPER_CLIENT_ID']}:{os.environ['LAKEKEEPER_CLIENT_SECRET']}",
        **{"oauth2-server-uri": "http://keycloak.keycloak.svc/realms/homelab/protocol/openid-connect/token",
           "scope": "lakekeeper", "s3.endpoint": "http://seaweedfs-s3.seaweedfs.svc:8333",
           "s3.region": "us-east-1", "s3.access-key-id": os.environ["AWS_ACCESS_KEY_ID"],
           "s3.secret-access-key": os.environ["AWS_SECRET_ACCESS_KEY"],
           "s3.force-virtual-addressing": "false"},
    )
    catalog.create_namespace_if_not_exists("raw")
    table = catalog.create_table_if_not_exists("raw.demo_events", schema=arrow.schema)
    table.overwrite(arrow)
    return {"rows": len(arrow)}


@asset(deps=[demo_events])
def event_summary():
    subprocess.run(
        ["dbt", "build", "--project-dir", "/opt/analytics/dbt",
         "--profiles-dir", "/opt/analytics/dbt"], check=True,
    )


defs = Definitions(assets=[demo_events, event_summary],
                   jobs=[define_asset_job("analytics_demo", selection="*")])
