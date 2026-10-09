"""One-shot catalog initialization; invoked only by the manual bootstrap Job."""
import argparse
import os
import requests
from pyarrow.fs import S3FileSystem


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--accept-terms", action="store_true", required=True)
    parser.parse_args()
    token = requests.post(
        "http://keycloak.keycloak.svc/realms/homelab/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": os.environ["LAKEKEEPER_CLIENT_ID"],
              "client_secret": os.environ["LAKEKEEPER_CLIENT_SECRET"], "scope": "lakekeeper"}, timeout=30,
    )
    token.raise_for_status()
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token.json()["access_token"]
    base = "http://lakekeeper.analytics.svc:8181/management/v1"
    info = session.get(base + "/info", timeout=30)
    info.raise_for_status()
    if not info.json()["bootstrapped"]:
        response = session.post(base + "/bootstrap", json={"accept-terms-of-use": True}, timeout=30)
        response.raise_for_status()
    s3 = S3FileSystem(access_key=os.environ["AWS_ACCESS_KEY_ID"],
                      secret_key=os.environ["AWS_SECRET_ACCESS_KEY"], region="us-east-1",
                      scheme="http", endpoint_override="seaweedfs-s3.seaweedfs.svc:8333",
                      allow_bucket_creation=True)
    s3.create_dir("analytics")
    existing = session.get(base + "/warehouse", params={"project-id": "00000000-0000-0000-0000-000000000000"}, timeout=30)
    existing.raise_for_status()
    if any(w["warehouse-name"] == "analytics" for w in existing.json()["warehouses"]):
        print("Analytics warehouse already exists; configuration preserved.")
        return
    response = session.post(base + "/warehouse", timeout=60, json={
        "warehouse-name": "analytics", "project-id": "00000000-0000-0000-0000-000000000000",
        "storage-profile": {"type": "s3", "bucket": "analytics", "region": "us-east-1",
                            "endpoint": "http://seaweedfs-s3.seaweedfs.svc:8333",
                            "path-style-access": True, "flavor": "s3-compat", "sts-enabled": False},
        "storage-credential": {"type": "s3", "credential-type": "access-key",
                               "access-key-id": os.environ["AWS_ACCESS_KEY_ID"],
                               "secret-access-key": os.environ["AWS_SECRET_ACCESS_KEY"]},
    })
    response.raise_for_status()
    print("Analytics warehouse created.")


if __name__ == "__main__":
    main()
