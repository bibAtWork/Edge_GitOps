#!/usr/bin/env python3
"""Check rendered Trivy quotas against concurrency and per-image resources.

A Job limit counts Jobs, not scanner containers. The Cilium chart currently
generates six images (one regular plus five init containers), invisible to a
root Kustomize render. Keep that observed floor, and include any larger Pod
templates declared directly in the overlay. Recheck this floor on chart updates.
"""
from decimal import Decimal
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import kustomize, parse_docs, report  # noqa: E402

OBSERVED_IMAGE_COUNT = 6


def quantity(value):
    match = re.fullmatch(r"([0-9.]+)(Ki|Mi|Gi|Ti|K|M|G|T|m)?", str(value))
    if not match:
        raise ValueError(f"unsupported resource quantity: {value}")
    units = {"": 1, "m": Decimal("0.001"), "K": 1000, "M": 1000**2,
             "G": 1000**3, "T": 1000**4, "Ki": 1024, "Mi": 1024**2,
             "Gi": 1024**3, "Ti": 1024**4}
    return Decimal(match[1]) * units[match[2] or ""]


def pod_spec(doc):
    spec = doc.get("spec", {})
    if doc["kind"] == "Pod":
        return spec
    if doc["kind"] == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def check_capacity(docs, concurrency):
    values = next(d["spec"]["values"] for d in docs
                  if d["kind"] == "HelmRelease" and d["metadata"]["name"] == "trivy-operator")
    quota = next(d["spec"]["hard"] for d in docs if d["kind"] == "ResourceQuota"
                 and d["metadata"].get("namespace") == "trivy-system")
    server = next(d for d in docs if d["kind"] == "Deployment"
                  and d["metadata"]["name"] == "trivy-server")
    errors = []
    operator = values["operator"]
    if operator.get("scanJobsConcurrentLimit") != concurrency or "scanJobConcurrentLimit" in operator:
        errors.append(f"scanJobsConcurrentLimit must be explicitly {concurrency}, without the misspelled key")
    image_count = max([OBSERVED_IMAGE_COUNT] + [
        len(pod_spec(d).get("containers", [])) + len(pod_spec(d).get("initContainers", []))
        for d in docs
    ])
    resident = [values["resources"]] + [c["resources"] for c in pod_spec(server)["containers"]]
    scanner = values["trivy"]["resources"]
    # One extra scanner's budget gives rollout/scheduling headroom.
    for section, resource in (("requests", "cpu"), ("requests", "memory"), ("limits", "memory")):
        key = f"{section}.{resource}"
        needed = sum(quantity(r[section][resource]) for r in resident)
        needed += (concurrency * image_count + 1) * quantity(scanner[section][resource])
        if quantity(quota[key]) < needed:
            errors.append(f"{key}={quota[key]} cannot cover {concurrency} x {image_count}-image scans "
                          f"plus resident services and headroom (needs {needed})")
    return errors


def main():
    errors = []
    for profile, concurrency in (("1-node", 1), ("3-node", 2)):
        rendered, error = kustomize(f"cluster/overlays/{profile}")
        if error:
            errors.append(f"{profile}: {error}")
        else:
            errors.extend(f"{profile}: {error}" for error in check_capacity(parse_docs(rendered), concurrency))
    return report(errors, "Trivy scan capacity errors:", "Both profiles budget for six-image Trivy scans.")


if __name__ == "__main__":
    sys.exit(main())
