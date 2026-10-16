#!/usr/bin/env python3
"""Require real, age-encrypted credentials before private apps reach Flux.

The missing GHCR Secret left both applications in ImagePullBackOff on
2026-10-16. Checking only workload references cannot catch that: Kubernetes
accepts the Pod and retries anonymous pulls indefinitely.
"""

from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "cluster/base/applications/sre-agent/agent-auth-secret.yaml":
        ("sre-agent", "agent-auth", "Opaque", "API_TOKEN"),
    "cluster/base/applications/sre-agent/agent-gemini-secret.yaml":
        ("sre-agent", "agent-gemini", "Opaque", "api-key"),
    "cluster/base/applications/sre-agent/ghcr-pull-secret.yaml":
        ("sre-agent", "ghcr-pull", "kubernetes.io/dockerconfigjson", ".dockerconfigjson"),
    "cluster/base/applications/real-estate-screener/ghcr-pull-secret.yaml":
        ("real-estate-screener", "ghcr-pull", "kubernetes.io/dockerconfigjson", ".dockerconfigjson"),
}


def errors(root: Path = ROOT) -> list[str]:
    failures = []
    age_recipient = yaml.safe_load((root / ".sops.yaml").read_text(encoding="utf-8"))[
        "creation_rules"][0]["age"]
    for relative, (namespace, name, secret_type, key) in EXPECTED.items():
        path = root / relative
        if not path.is_file():
            failures.append(f"{relative}: missing")
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not a YAML object")
            if doc.get("kind") != "Secret" or doc.get("metadata", {}).get("name") != name:
                raise ValueError(f"expected Secret/{name}")
            if doc["metadata"].get("namespace") != namespace or doc.get("type") != secret_type:
                raise ValueError("namespace or Secret type differs from workload reference")
            value = (doc.get("stringData") or {}).get(key)
            if not isinstance(value, str) or not value.startswith("ENC[AES256_GCM,"):
                raise ValueError(f"{key} must be SOPS ciphertext")
            recipients = [item.get("recipient") for item in (doc.get("sops") or {}).get("age", [])]
            if age_recipient not in recipients:
                raise ValueError("SOPS age recipient differs from the cluster key")
            if namespace == "sre-agent":
                kustomization = root / "cluster/base/applications/sre-agent/kustomization.yaml"
            else:
                kustomization = root / "cluster/base/applications/real-estate-screener/kustomization.yaml"
            resources = yaml.safe_load(kustomization.read_text(encoding="utf-8"))["resources"]
            if path.name not in resources:
                raise ValueError("not included in the application Kustomization")
        except (AttributeError, OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as error:
            failures.append(f"{relative}: {error}")
    return failures


def main() -> int:
    failures = errors()
    if failures:
        print("Private application credential prerequisites are incomplete:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("Both private applications have age-encrypted pull and runtime Secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
