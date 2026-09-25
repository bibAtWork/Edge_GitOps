#!/usr/bin/env python3
"""HelmRelease sourceRef and dependsOn references must resolve, per overlay.

A HelmRelease whose chart source or dependency does not exist in the same
rendered overlay is never reconciled, and nothing else notices.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, kustomize, parse_docs, report  # noqa: E402

OVERLAYS = ("1-node-config", "3-node-config")


def reference_errors(overlay, docs):
    errors = []
    helm_repos = {d["metadata"]["name"] for d in docs if d.get("kind") == "HelmRepository"}
    helm_releases = {d["metadata"]["name"] for d in docs if d.get("kind") == "HelmRelease"}

    for doc in docs:
        if doc.get("kind") != "HelmRelease":
            continue
        name = doc["metadata"]["name"]

        # sourceRef -- the chart source must exist in the same overlay
        source = doc.get("spec", {}).get("chart", {}).get("spec", {}).get("sourceRef", {})
        if source.get("kind") == "HelmRepository":
            ref = source.get("name", "")
            if ref not in helm_repos:
                errors.append(f"[{overlay}] HelmRelease/{name}: sourceRef HelmRepository/{ref} not found in overlay")

        # dependsOn -- every named dependency must exist
        for dep in doc.get("spec", {}).get("dependsOn", []):
            dep_name = dep.get("name", "")
            if dep_name and dep_name not in helm_releases:
                errors.append(f"[{overlay}] HelmRelease/{name}: dependsOn HelmRelease/{dep_name} not found in overlay")
    return errors


def main():
    errors = []
    for overlay in OVERLAYS:
        text, error = kustomize(ROOT / "cluster" / "overlays" / overlay)
        if error is not None:
            errors.append(f"[{overlay}] kubectl kustomize failed:\n{error}")
            continue
        errors.extend(reference_errors(overlay, parse_docs(text)))
    return report(errors, "{n} reference error(s) found:\n", "All HelmRelease references resolve correctly.")


if __name__ == "__main__":
    sys.exit(main())
