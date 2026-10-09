#!/usr/bin/env python3
"""Provision runtime credentials without writing plaintext files or logging values.

Requires kubectl and authenticated cluster admin access. Existing Secrets are preserved.
Keycloak client must already exist (see runbook). Back up generated Secrets securely.
"""
import base64
import getpass
import hashlib
import json
import secrets
import subprocess


def kubectl(*args, **kwargs):
    return subprocess.run(["kubectl", *args], check=True, capture_output=True,
                          text=True, **kwargs).stdout


def read(name, namespace="analytics"):
    result = kubectl("get", "secret", name, "-n", namespace,
                     "--ignore-not-found", "-o", "json")
    return json.loads(result) if result.strip() else None


def ensure(name, values, kind="Opaque"):
    existing = read(name)
    if existing:
        return {k: base64.b64decode(v).decode() for k, v in existing["data"].items()}
    manifest = {"apiVersion": "v1", "kind": "Secret", "type": kind,
                "metadata": {"name": name, "namespace": "analytics"},
                "stringData": values}
    kubectl("apply", "-f", "-", input=json.dumps(manifest))
    print(f"Created {name}; preserve it in your encrypted secret backup.")
    return values


def password_hash(password):
    salt = secrets.token_bytes(16)
    iterations = 200000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{iterations}:{salt.hex()}:{digest.hex()}"


def main():
    # Namespace is managed by Flux; fail rather than provision into a wrong context.
    kubectl("get", "namespace", "analytics")
    source = read("seaweedfs-s3-secret", "seaweedfs")
    if not source:
        raise RuntimeError("Existing SeaweedFS admin Secret not found")
    s3 = ensure("analytics-s3", {k: base64.b64decode(source["data"][k]).decode()
                                for k in ("admin_access_key_id", "admin_secret_access_key")})
    for name in ("dagster", "lightdash"):
        ensure(f"analytics-{name}-db", {"username": name, "password": secrets.token_urlsafe(32)},
               "kubernetes.io/basic-auth")
    ensure("analytics-runtime", {"lakekeeper-encryption-key": secrets.token_urlsafe(32),
                                "lightdash-secret": secrets.token_hex(32),
                                "trino-internal-secret": secrets.token_urlsafe(48)})
    creds = ensure("analytics-trino-client", {"dbt-password": secrets.token_urlsafe(32),
                                             "bi-password": secrets.token_urlsafe(32)})
    ensure("analytics-trino", {"password.db": "\n".join(
        f"analytics-{name}:{password_hash(creds[name+'-password'])}" for name in ("dbt", "bi")) + "\n"})
    oauth = read("analytics-oauth")
    if oauth:
        client = {k: base64.b64decode(v).decode() for k, v in oauth["data"].items()}
    else:
        client = ensure("analytics-oauth", {"client-id": "analytics-engine",
                                            "client-secret": getpass.getpass("Client secret: ")})
    pg = read("analytics-dagster-db")
    ensure("analytics-run-env", {"PGPASSWORD": base64.b64decode(pg["data"]["password"]).decode(),
        "PGHOST": "analytics-pg-rw.analytics.svc", "PGUSER": "dagster", "PGDATABASE": "dagster",
        "LAKEKEEPER_CLIENT_ID": client["client-id"], "LAKEKEEPER_CLIENT_SECRET": client["client-secret"],
        "AWS_ACCESS_KEY_ID": s3["admin_access_key_id"], "AWS_SECRET_ACCESS_KEY": s3["admin_secret_access_key"],
        "TRINO_DBT_PASSWORD": creds["dbt-password"], "REQUESTS_CA_BUNDLE": "/etc/analytics/tls/ca.crt"})
    print("Credentials prepared. Build the image and complete warehouse bootstrap before running the demo.")


if __name__ == "__main__":
    main()
