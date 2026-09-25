#!/usr/bin/env python3
"""Reject the REPLACED-BY-KUSTOMIZE sentinel surviving a render.

The version pins in the SUC Plans and the fleet-health CronJob are Kustomize
`replacements` targets sourced from versions.env. Their literals in git are the
sentinel, never a version.

They used to be real versions, on the reasoning that a build should still
"produce something sane" if a replacement were dropped. That is backwards: it
produced a Plan pinned to a stale but perfectly installable version, with
nothing reporting anything -- the Plans said v1.13.9 and one said v1.32.3 while
the cluster ran v1.13.0/v1.36.0, and Renovate read those literals as if they
were real pins.

A sentinel cannot be pulled, so a dropped replacement fails. This makes it fail
HERE, before merge, rather than at image-pull time on a node that has already
been cordoned.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, kustomize  # noqa: E402

SENTINEL = "REPLACED-BY-KUSTOMIZE"

# The *-config overlays, NOT the base ones. The Plans and the gate CronJobs are
# CRD-dependent and live in each profile's config overlay; the base overlays pull
# in only the SUC operator. Checking a base overlay would render nothing
# containing a sentinel and pass unconditionally -- which is exactly what the
# first draft of this job did for 1-node, and what checking "3-node" instead of
# "3-node-config" would silently start doing again once 3-node gained its own
# operator/config split (2026-09-15, docs/backlog.md "3-node overlay would not
# produce a working cluster") -- before that, 3-node's base overlay held the
# Plans directly, which is the only reason "3-node" ever caught anything here.
OVERLAYS = ("1-node-config", "3-node-config")


def sentinel_lines(text):
    """`line-number:line` for each surviving sentinel, like grep -n."""
    return [f"{number}:{line}" for number, line in enumerate(text.splitlines(), 1) if SENTINEL in line]


def main():
    failed = False
    for overlay in OVERLAYS:
        text, error = kustomize(ROOT / "cluster" / "overlays" / overlay)
        if error is not None:
            print(f"ERROR: kubectl kustomize failed for {overlay}")
            failed = True
            continue
        if not text.strip():
            print(f"ERROR: {overlay} rendered empty")
            failed = True
            continue
        hits = sentinel_lines(text)
        if hits:
            print(f"ERROR: sentinel survived the {overlay} render -- a replacements target is missing or mis-pathed:")
            print("\n".join(hits))
            failed = True
    if not failed:
        print("No unreplaced sentinels.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
