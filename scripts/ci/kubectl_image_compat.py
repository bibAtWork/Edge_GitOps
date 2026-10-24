#!/usr/bin/env python3
"""Require an executable bundled kubectl compatible with both upgrade endpoints.

The base branch's Kubernetes pin is the CI proxy for the current API server;
the Sunday gate checks the actual live server. Inspect the *image* rather than
inferring its contents from a Rancher tag or Renovate group.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = "cluster/base/infrastructure/15-system-upgrade-controller/config/versions.env"
HELM = "cluster/base/infrastructure/15-system-upgrade-controller/operator/helmrelease.yaml"
GATES = (
    "cluster/base/infrastructure/15-system-upgrade-controller/config/upgrade-gate-cronjob.yaml",
    "cluster/base/infrastructure/15-system-upgrade-controller/config/talos-version-gate-cronjob.yaml",
)
WATCHED = ("cluster/base/infrastructure/15-system-upgrade-controller/",
           ".github/workflows/gitops-lint.yml", "scripts/ci/kubectl_image_compat.py")
VERSION = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
BINARY = re.compile(r"^/usr/bin/kubectl(?P<version>1\.\d+\.\d+)$")


def minor(version):
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"invalid Kubernetes version: {version!r}")
    return int(match.group("major")), int(match.group("minor"))


def pinned_version(text):
    match = re.search(r"^KUBERNETES_VERSION=(v\d+\.\d+\.\d+)$", text, re.M)
    if not match:
        raise ValueError("KUBERNETES_VERSION is missing or malformed")
    return match.group(1)


def normalized(image):
    return image.removeprefix("docker.io/")


def source(root, path):
    full_path = root / path
    if os.name == "nt":
        full_path = Path("\\\\?\\" + str(full_path))
    return full_path.read_text(encoding="utf-8")


def configured_image(root=ROOT):
    release = yaml.safe_load(source(root, HELM))
    patches = [patch for renderer in release["spec"]["postRenderers"]
               for patch in renderer["kustomize"]["patches"]]
    config = next(yaml.safe_load(patch["patch"]) for patch in patches
                  if patch["target"].get("kind") == "ConfigMap"
                  and patch["target"].get("name") == "system-upgrade-controller-config")
    image = config["data"]["SYSTEM_UPGRADE_JOB_KUBECTL_IMAGE"]
    for path in GATES:
        cronjob = yaml.safe_load(source(root, path))
        gate_image = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
        if normalized(gate_image) != normalized(image):
            raise ValueError(f"{path} uses {gate_image}, but upgrade Jobs use {image}; update them together")
    return image


def bundled_versions(output):
    """Read version evidence emitted by real executables inside the image."""
    result = set()
    for record in output.split("BINARY:")[1:]:
        lines = record.splitlines()
        binary = BINARY.fullmatch(lines[0].strip()) if lines else None
        reported = re.search(r'"gitVersion"\s*:\s*"(v\d+\.\d+\.\d+)"', record)
        if not binary or not reported or reported.group(1) != "v" + binary.group("version"):
            raise ValueError(f"bundled kubectl path and reported version disagree: {record[:120]!r}")
        result.add(reported.group(1))
    if not result:
        raise ValueError("the image exposes no executable, version-reporting bundled kubectl")
    return result


def compatibility(versions, current, target):
    current_minor, target_minor = minor(current), minor(target)
    usable = sorted(version for version in versions
                    if all(minor(version)[0] == api[0]
                           and abs(minor(version)[1] - api[1]) <= 1
                           for api in (current_minor, target_minor)))
    if not usable:
        raise ValueError(f"no bundled kubectl is within one minor of both {current} and {target}; "
                         f"found {', '.join(sorted(versions))}")
    return usable


def run(command):
    return subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True).stdout


def main():
    try:
        image = configured_image()
        base = os.environ.get("BASE_REF", "ops/talos_linux")
        current = pinned_version(run(["git", "show", f"origin/{base}:{VERSIONS}"]))
        target = pinned_version(source(ROOT, VERSIONS))
        changed = run(["git", "diff", "--name-only", f"origin/{base}...HEAD"]).splitlines()
        if not any(path.startswith(WATCHED) for path in changed):
            print(f"kubectl image declarations agree; no upgrade inputs changed ({image})")
            return 0
        shell = ('set -eu; found=0; for f in /usr/bin/kubectl1.*; do '
                 '[ -x "$f" ] || continue; found=1; printf "BINARY:%s\\n" "$f"; '
                 '"$f" version --client -o json; done; [ "$found" -eq 1 ]')
        output = run(["docker", "run", "--rm", "--entrypoint", "/bin/sh", image, "-c", shell])
        usable = compatibility(bundled_versions(output), current, target)
        print(f"{image}: {', '.join(usable)} supports both base {current} and target {target}")
        return 0
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print(f"::error::kubectl image compatibility: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
