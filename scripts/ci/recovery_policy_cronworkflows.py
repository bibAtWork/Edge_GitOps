#!/usr/bin/env python3
"""Every recovery-policy application has a recovery-point CronWorkflow, and vice versa.

Restores a narrower version of a check 598beee removed: that one bound the OLD
backup pipeline's policy file to the jobs implementing it, and was removed
because both had stopped existing (ADR-012 cutover). This one binds the CURRENT
policy (recovery-policy.yaml) to the CURRENT implementation. Nothing else does:
the reconciler reads the policy directly and would back up an application added
to it with no matching CronWorkflow, but the pre-upgrade gate used to derive its
own list of applications from the CronWorkflows' names, so that same application
would silently never be proven before an upgrade (fixed alongside this,
upgrade-gate-script.yaml -- the gate now also reads the policy directly, which
makes a missing CronWorkflow fail gate proof for that application instead of
hiding it; this check exists to catch the mismatch at PR time instead of at
11:00 the following Sunday).
"""
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, kustomize, parse_docs, report  # noqa: E402

# Recovery policy and CronWorkflows are CRD-dependent resources and therefore
# live in each profile's config overlay.
OVERLAYS = ("1-node-config", "3-node-config")


def policy_applications(policy):
    apps = set()
    for line in policy.get("data", {}).get("applications", "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            apps.add(line.split()[0])
    return apps


def consistency_errors(overlay, docs):
    """-> (errors, checked). checked is False for an overlay with no policy at all."""
    policy = next((d for d in docs if d.get("kind") == "ConfigMap"
                   and d.get("metadata", {}).get("name") == "recovery-policy"), None)
    if policy is None:
        print(f"[{overlay}] no ConfigMap/recovery-policy found -- this overlay does not run "
              f"the recovery system, skipping")
        return [], False

    policy_apps = policy_applications(policy)
    if not policy_apps:
        return [f"[{overlay}] recovery-policy's applications table is empty -- cannot compare anything"], True

    cron_apps = set()
    for d in docs:
        if d.get("kind") == "CronWorkflow" and d.get("metadata", {}).get("namespace") == "backup-system":
            match = re.match(r"^recovery-point-(.+)$", d["metadata"]["name"])
            if match:
                cron_apps.add(match.group(1))

    errors = []
    for app in sorted(policy_apps - cron_apps):
        errors.append(
            f'[{overlay}] recovery-policy declares application "{app}" but there is no '
            f"recovery-point-{app} CronWorkflow in backup-system -- it will never be proven "
            f"by the pre-upgrade gate")
    for app in sorted(cron_apps - policy_apps):
        errors.append(
            f'[{overlay}] recovery-point-{app} CronWorkflow exists but "{app}" is not in '
            f"recovery-policy's applications table -- it is running unplanned, or the "
            f"policy is stale")
    return errors, True


def main():
    errors = []
    # An overlay with no policy is skipped; every overlay skipping fails closed below.
    checked = []
    for overlay in OVERLAYS:
        text, error = kustomize(ROOT / "cluster" / "overlays" / overlay)
        if error is not None:
            errors.append(f"[{overlay}] kubectl kustomize failed:\n{error}")
            continue
        found, was_checked = consistency_errors(overlay, parse_docs(text))
        errors.extend(found)
        if was_checked:
            checked.append(overlay)

    if not checked and not errors:
        errors.append(
            "no overlay has a recovery-policy ConfigMap -- this check compared nothing. "
            "If the recovery system was removed intentionally, remove this job; if not, "
            "something broke every overlay's reference to 37-backup-system.")

    return report(
        errors, "{n} consistency error(s) found:\n",
        f"Every recovery-policy application has a matching CronWorkflow, and vice versa "
        f"(checked: {', '.join(checked)}).")


if __name__ == "__main__":
    sys.exit(main())
