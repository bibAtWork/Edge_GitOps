#!/usr/bin/env python3
"""The 1-node and 3-node profiles must reference the same components.

docs/backlog.md, "The 3-node overlay would not produce a working cluster":
found 2026-08-25 with 1-node listing 33 infrastructure components and 3-node
listing 16 -- an 18-component gap nothing caught, because kubeconform and
kustomize build both succeed on an incomplete-but-valid overlay just as readily
as a complete one. Fixed 2026-09-15 by porting the missing components and adding
3-node-config (mirroring 1-node-config's operator/config split), but a fix with
nothing to stop it recurring is exactly how it happened the first time: 3-node's
overlay was simply not touched as new components were added to 1-node over three
weeks.

This compares the SET of base/infrastructure/<NN-name> components each profile
references -- across both its base overlay and its own -config overlay
together, since a component split into operator/config (the pattern every
CRD-dependent component uses) is only "present" once both halves are counted. A
component intentionally exclusive to one profile is not expected to exist yet --
if one ever is, add it to INTENTIONAL_ONLY_IN below with a comment saying why,
rather than loosening the check itself.
"""
from pathlib import Path
import re
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, report  # noqa: E402

# profile -> components intentionally present in ONLY that profile. Empty on both
# sides today -- as of 2026-09-15 the two profiles are full parity. A real,
# permanent difference should be rare: these are infrastructure components, not
# profile-specific tuning (that belongs in each overlay's own patches/, which this
# check does not touch at all).
INTENTIONAL_ONLY_IN = {
    "1-node": set(),
    "3-node": set(),
}


def components(profile, root=ROOT):
    """The NN-name component directories a profile's kustomizations reach."""
    names, visited = set(), set()

    def visit(path):
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        for part in path.parts:
            if re.fullmatch(r"[0-9]{2}-[a-z0-9-]+", part):
                names.add(part)
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        for resource in data.get("resources", []):
            child = path.parent / resource
            if child.is_dir():
                visit(child / "kustomization.yaml")

    for suffix in ("", "-config"):
        visit(Path(root) / "cluster" / "overlays" / f"{profile}{suffix}" / "kustomization.yaml")
    return names


def divergences(one, three, allowed=INTENTIONAL_ONLY_IN):
    errors = []
    for c in sorted((one - three) - allowed["1-node"]):
        errors.append(f"{c} is in 1-node/1-node-config but not 3-node/3-node-config")
    for c in sorted((three - one) - allowed["3-node"]):
        errors.append(f"{c} is in 3-node/3-node-config but not 1-node/1-node-config")
    # An allowlist entry for a component BOTH profiles actually reference is
    # stale, not protective -- catch it rather than let it silently do nothing.
    for c in sorted(allowed["1-node"] & three):
        errors.append(f'INTENTIONAL_ONLY_IN["1-node"] lists {c}, but 3-node references it too -- remove the allowlist entry')
    for c in sorted(allowed["3-node"] & one):
        errors.append(f'INTENTIONAL_ONLY_IN["3-node"] lists {c}, but 1-node references it too -- remove the allowlist entry')
    return errors


def main():
    one, three = components("1-node"), components("3-node")
    if not one or not three:
        print("ERROR: found zero components in one or both profiles -- the extraction "
              "regex or the overlay paths are wrong, not that either overlay is truly empty")
        return 1
    return report(
        divergences(one, three), "{n} divergence(s) found between the two profiles:\n",
        f"1-node and 3-node reference the same {len(one)} infrastructure components.",
        footer="Either port the missing component to the profile lacking it, or -- if the "
               "difference is genuinely intentional -- add it to INTENTIONAL_ONLY_IN in this "
               "job with a comment explaining why.")


if __name__ == "__main__":
    sys.exit(main())
