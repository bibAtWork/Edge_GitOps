#!/usr/bin/env python3
"""Every Kustomize `replacements` target in the SUC config actually resolved.

Code review on PR #386 (round 2), verified empirically: a `replacements` target
whose `select` matches nothing fails silently. `kustomize build` exits 0 and the
field just keeps whatever literal was written in the source Plan file -- which
commonly happens to be the version that was correct when the placeholder was last
hand-edited, so a stale-but-plausible-looking value ships with no error anywhere.
Renaming a Plan (a typo in `metadata.name`, or in a `replacements[].targets[].
select.name`) is enough to trigger it. The per-field CI cross-check that the
single-source design removed (a nine-way pin comparison) was incidentally the only
thing that would have caught this class of drift; this replaces that coverage.

So: render for real, then assert -- by kind and name, not by grepping the whole file
-- that every field `replacements` is supposed to drive equals versions.env's value.
Round 4 review found the last of the eleven targets (DESIRED_KUBERNETES_VERSION) had
no check, so a broken select there would have shipped the stale placeholder with
TalosFleetK8sVersionDrift silently blind -- the class of bug this exists to catch.
"""
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, kustomize, parse_docs, read_versions_env  # noqa: E402

CONFIG = "cluster/base/infrastructure/15-system-upgrade-controller/config"


def tag(image):
    """What follows the last colon (yq: sub(".*:", "")); unchanged if there is none."""
    return re.sub(r".*:", "", image) if isinstance(image, str) else image


def plan(name, *path):
    return ("Plan", name, path)


def fleet_env(var):
    return ("CronJob", "talos-fleet-health", ("spec", "jobTemplate", "spec", "template", "spec", "containers", ("name", "check"), "env", ("name", var), "value"))


def resolve(node, path):
    """Values reached by walking path; a (key, value) step selects the list items with that key."""
    nodes = [node]
    for step in path:
        nxt = []
        for n in nodes:
            if isinstance(step, tuple):
                key, wanted = step
                nxt += [item for item in (n or []) if isinstance(item, dict) and item.get(key) == wanted]
            elif isinstance(n, dict) and step in n:
                nxt.append(n[step])
        nodes = nxt
    return nodes


def expectations(talos, kubernetes):
    """(description, (kind, name, path), transform, expected value)"""
    same = lambda v: v  # noqa: E731
    return [
        ("talos-controlplane spec.version", plan("talos-controlplane", "spec", "version"), same, talos),
        ("talos-controlplane spec.upgrade.image tag", plan("talos-controlplane", "spec", "upgrade", "image"), tag, talos),
        ("talos-worker spec.version", plan("talos-worker", "spec", "version"), same, talos),
        ("talos-worker spec.prepare.image tag", plan("talos-worker", "spec", "prepare", "image"), tag, talos),
        ("talos-worker spec.upgrade.image tag", plan("talos-worker", "spec", "upgrade", "image"), tag, talos),
        ("talos-on-demand spec.version", plan("talos-on-demand", "spec", "version"), same, talos),
        ("talos-on-demand spec.upgrade.image tag", plan("talos-on-demand", "spec", "upgrade", "image"), tag, talos),
        ("talos-kubernetes spec.version", plan("talos-kubernetes", "spec", "version"), same, kubernetes),
        ("talos-kubernetes spec.upgrade.image tag", plan("talos-kubernetes", "spec", "upgrade", "image"), tag, talos),
        ("talos-fleet-health DESIRED_TALOS_VERSION", fleet_env("DESIRED_TALOS_VERSION"), same, talos),
        ("talos-fleet-health DESIRED_KUBERNETES_VERSION", fleet_env("DESIRED_KUBERNETES_VERSION"), same, kubernetes),
    ]


def unresolved(docs, talos, kubernetes):
    """-> (ok lines, error lines)."""
    ok, errors = [], []
    for description, (kind, name, path), transform, expected in expectations(talos, kubernetes):
        matches = [d for d in docs if d.get("kind") == kind and d.get("metadata", {}).get("name") == name]
        values = [transform(v) for d in matches for v in resolve(d, path)]
        if values == [expected]:
            ok.append(f"  ok    {description} = {expected}")
        else:
            actual = "\n".join("null" if v is None else str(v) for v in values) or "null"
            errors.append(f"{description} is '{actual}', expected '{expected}' (from versions.env). A Kustomize "
                          f"`replacements` target that matches nothing fails silently rather than erroring -- "
                          f"this usually means the resource was renamed and the replacement's `select` no longer "
                          f"finds it.")
    return ok, errors


def main():
    text, error = kustomize(ROOT / CONFIG)
    if error is not None:
        print(f"::error::kubectl kustomize failed for {CONFIG}: {error}")
        return 1
    env = read_versions_env()
    ok, errors = unresolved(parse_docs(text), env.get("TALOS_VERSION", ""), env.get("KUBERNETES_VERSION", ""))
    print("\n".join(ok))
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
