#!/usr/bin/env python3
"""Require every declared Longhorn PVC to have a recovery-policy decision."""

from __future__ import annotations

import subprocess
import sys

import yaml


PROFILES = ("1-node", "3-node")


def render(overlay: str) -> list[dict]:
    result = subprocess.run(
        [
            "kubectl",
            "kustomize",
            "--load-restrictor",
            "LoadRestrictionsNone",
            f"cluster/overlays/{overlay}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"[{overlay}] kubectl kustomize failed:\n{result.stderr.strip()}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def table_rows(value: str) -> list[list[str]]:
    rows = []
    for line in value.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            rows.append(line.split())
    return rows


def check_profile(profile: str) -> list[str]:
    errors: list[str] = []
    try:
        docs = render(profile) + render(f"{profile}-config")
    except RuntimeError as error:
        return [str(error)]

    policies = [
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("namespace") == "backup-system"
        and doc.get("metadata", {}).get("name") == "recovery-policy"
    ]
    if len(policies) != 1:
        return [f"[{profile}] expected one backup-system/recovery-policy, found {len(policies)}"]
    policy = policies[0].get("data", {})

    protected: set[str] = set()
    for row in table_rows(policy.get("datasets", "")):
        if len(row) != 5:
            errors.append(f"[{profile}] malformed datasets row: {' '.join(row)}")
            continue
        if row[3].startswith("pvc:"):
            protected.add(row[3])

    excluded: set[str] = set()
    for row in table_rows(policy.get("excluded-volumes", "")):
        if len(row) < 3 or not row[0].startswith("pvc:"):
            errors.append(f"[{profile}] malformed excluded-volumes row: {' '.join(row)}")
            continue
        excluded.add(row[0])

    overlap = protected & excluded
    for source in sorted(overlap):
        errors.append(f"[{profile}] {source} is both protected and excluded")

    claims = set()
    for doc in docs:
        if doc.get("kind") != "PersistentVolumeClaim":
            continue
        storage_class = doc.get("spec", {}).get("storageClassName", "")
        if not storage_class.startswith("longhorn"):
            continue
        metadata = doc.get("metadata", {})
        namespace = metadata.get("namespace", "default")
        claims.add(f"pvc:{namespace}/{metadata.get('name', '')}")

    classified = protected | excluded
    for source in sorted(claims - classified):
        errors.append(
            f"[{profile}] {source} uses Longhorn but is absent from recovery-policy "
            "datasets and excluded-volumes"
        )
    for source in sorted((protected | excluded) - claims):
        errors.append(f"[{profile}] recovery-policy classifies missing {source}")

    if not claims:
        errors.append(f"[{profile}] rendered no Longhorn PVCs; coverage check compared nothing")
    return errors


def main() -> int:
    errors = [error for profile in PROFILES for error in check_profile(profile)]
    if errors:
        print(f"{len(errors)} recovery volume coverage error(s):\n")
        for error in errors:
            print(f"  ERROR: {error}")
        return 1
    print("Every declared Longhorn PVC is protected or explicitly excluded in both profiles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
