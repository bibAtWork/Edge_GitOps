#!/usr/bin/env python3
"""Reject :latest (and untagged) images in every rendered overlay and in the
Talos machine configs.

Ported from a shell pipeline; each rule below is the same grep/sed/awk it was,
and each carries the lesson that shaped it.
"""
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, kustomize  # noqa: E402

OVERLAYS = ("1-node", "3-node", "1-node-config", "3-node-config")

# The `-?` is load-bearing. Container specs render as list items
# (`      - image: foo:latest`), and the previous pattern required whitespace
# immediately before `image:`, so the leading dash broke every match. This gate
# passed on everything for as long as it existed, including on the one image in
# this repo that did use :latest -- which is exactly what it was written to
# catch.
LATEST = re.compile(r"^\s*-?\s*image:\s+\S+:latest(\s|$)")

# An image with NO tag is :latest too, and says so nowhere -- LATEST cannot see
# it. local-path-provisioner's helper pod carried a bare `busybox` for the life
# of this repo, inside a ConfigMap, so neither this gate nor schema validation
# touched it. A digest pin is fine; a bare repository name is not.
IMAGE_LINE = re.compile(r"^\s*-?\s*image:")
IMAGE_VALUE = re.compile(r'^.*image:\s*"?([^"\s]+)"?.*$')

# The overlay loop only sees what Kustomize renders. Talos machine configs are
# not in any kustomization -- they are applied by hand via talosctl -- so an
# image pinned in an inlineManifest is invisible to it. That blind spot is not
# hypothetical: the CNI bootstrap Job sat on cilium-cli-ci:latest until that tag
# went distroless and broke a fresh bootstrap outright, and this gate passed on
# every run in between. So the files are read directly, since there is nothing
# to render.
MACHINE_CONFIG_LATEST = re.compile(r"image:\s*\S+:latest(\s|$)")


def latest_lines(text):
    return [line for line in text.splitlines() if LATEST.search(line)]


def untagged_images(text):
    """Image references with no tag. Digest pins are fine, and so are the wildcard
    values Kyverno policies use as a match key (docker.io/*): those are policy
    expressions rather than pullable container references."""
    found = []
    for line in text.splitlines():
        if not IMAGE_LINE.search(line):
            continue
        value = IMAGE_VALUE.sub(r"\1", line)  # a line the pattern misses stays whole, as with sed
        if "@sha256:" in value or "*" in value:
            continue
        if ":" not in value.split("/")[-1]:
            found.append(value)
    return found


def machine_config_hits(files):
    hits = []
    for path in files:
        for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if MACHINE_CONFIG_LATEST.search(line):
                hits.append(f"{Path(path).relative_to(ROOT).as_posix()}:{number}:{line}")
    return hits


def main():
    failed = False
    for overlay in OVERLAYS:
        # Rendered to text first, and a failed or empty render is an error of
        # its own. Piping straight into grep hides the build's exit status
        # behind `|| true`, so a failed render reads as "no violations found"
        # and the gate passes on nothing at all.
        text, error = kustomize(ROOT / "cluster" / "overlays" / overlay)
        if error is not None:
            print(f"ERROR: kubectl kustomize failed for {overlay}")
            failed = True
            continue
        if not text.strip():
            print(f"ERROR: {overlay} rendered empty")
            failed = True
            continue
        untagged = untagged_images(text)
        hits = latest_lines(text)
        if untagged:
            print(f"ERROR: untagged images in {overlay} overlay (resolve to :latest at pull time):")
            print("\n".join(untagged))
            failed = True
        if hits:
            print(f"ERROR: :latest tags found in {overlay} overlay (bypasses Trivy gate and Renovate tracking):")
            print("\n".join(hits))
            failed = True

    files = sorted((ROOT / "cluster" / "overlays").glob("*/talos-machineconfigs/*.yaml"))
    hits = machine_config_hits(files)
    if hits:
        print("ERROR: :latest tags found in Talos machine configs (applied out-of-band, so nothing else tracks them):")
        print("\n".join(hits))
        failed = True

    if not failed:
        print("No :latest tags found.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
