#!/usr/bin/env python3
"""Guards on versions.env's KUBERNETES_VERSION and TALOS_VERSION pins.

  version_pins.py release     KUBERNETES_VERSION names a real, plain vX.Y.Z release
  version_pins.py minor-step  a PR advances KUBERNETES_VERSION by at most one minor
  version_pins.py downgrade   a PR that lowers either pin carries `confirmed-downgrade`

`minor-step` and `downgrade` compare the working tree with the PR's base branch
(BASE_REF, the branch name as GitHub gives it; compared against origin/<BASE_REF>).
"""
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, VERSIONS_ENV, git_ref_exists, git_show, github_headers, probe, read_versions_env  # noqa: E402

VERSIONS_ENV_REL = VERSIONS_ENV.relative_to(ROOT).as_posix()
PLAIN_VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


def parse_env(text):
    values = {}
    for line in (text or "").splitlines():
        match = re.match(r"^([A-Z_]+)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


# ── release ──────────────────────────────────────────────────────────────────
#
# kubernetes/kubernetes tags every release vX.Y.Z. Two guards, because either one
# alone lets a pre-release through -- found in code review on PR #386 (round 2):
#
# 1. The pin must be a plain vX.Y.Z. A -rc/-alpha/-beta pin resolves to a real tag
#    and would otherwise pass the lookup below.
# 2. The EXACT-match ref endpoint (/git/ref/, singular). The plural /git/refs/ has
#    no GET route at all in GitHub's current REST API spec (only PATCH/DELETE) --
#    the historical GET form some code still relies on is /git/matching-refs/,
#    which is explicitly a PREFIX match: "If the :ref doesn't exist in the
#    repository, but existing refs start with :ref, they will be returned as an
#    array". Querying that form for v1.36.3 would answer positively off a
#    v1.36.3-rc.0 tag alone -- passing the check on a version that was never
#    actually released. /git/ref/ (git/get-ref in the same spec) is documented
#    200/404/409 with no prefix behavior.
#
# And a third, from a failure: an unauthenticated call from a shared runner got a
# 403 (rate limit) and this reported "no tag v1.37.0" for a tag that exists. 404 is
# the only answer that means the tag is missing; anything else is "could not
# verify", said as such, after retries, and the call authenticates when it can.

def release_report(version, probe_fn=probe):
    """-> (lines, ok)."""
    if not PLAIN_VERSION.match(version):
        return [f"::error::KUBERNETES_VERSION is '{version}', which is not a plain vX.Y.Z release tag. "
                f"Pre-release tags (-rc.N, -alpha.N, -beta.N) are not permitted as an upgrade target."], False
    verdict, status = probe_fn(f"https://api.github.com/repos/kubernetes/kubernetes/git/ref/tags/{version}",
                               headers=github_headers())
    if verdict == "exists":
        return [f"  ok    kubernetes/kubernetes@{version} exists"], True
    if verdict == "missing":
        return [f"::error::kubernetes/kubernetes has no tag {version} (HTTP {status}). KUBERNETES_VERSION in "
                f"versions.env does not name a real release -- talosctl upgrade-k8s would fail against it."], False
    return [f"::error::Could not verify kubernetes/kubernetes@{version} (HTTP {status}: a rate limit or an "
            f"outage, not a missing tag). Re-run this check."], False


def cmd_release(probe_fn=probe):
    lines, ok = release_report(read_versions_env().get("KUBERNETES_VERSION", ""), probe_fn)
    print("\n".join(lines))
    return 0 if ok else 1


# ── minor-step ───────────────────────────────────────────────────────────────
#
# talosctl upgrade-k8s only supports an N -> N or N -> N+1 minor step
# (upgrade.Path.IsSupported() upstream); a bigger jump fails after SUC has already
# cordoned the node. Renovate's regex manager on this literal pin has no notion of
# "propose one minor at a time" -- it always proposes the newest kubernetes/
# kubernetes release it sees, which is frequently more than one minor ahead. There
# is no Renovate-side setting that constrains a raw-string regex manager to a fixed
# step size, so this is enforced here instead: a PR that skips a minor fails CI and
# needs a human to land the intermediate minor(s) first, the same way a downgrade
# needs the confirmed-downgrade label rather than applying freely.
#
# Known limitation (round 5 code review): this compares git to git -- the PR's pin
# against its base branch's -- not against what the cluster is actually running.
# upgrade-k8s itself validates IsSupported() against the real running version,
# which this cannot see. If bumps land in git faster than they get applied (a
# missed window, a failed Job), the pin can drift two or more minors ahead of the
# cluster while every individual PR still passes this check one minor at a time.
# talos_fleet_k8s_version_drift (fleet-health) is what actually catches that case,
# by comparing the running version against the pin directly.

def minor_of(env_text):
    match = re.search(r"^KUBERNETES_VERSION=(v[0-9]+\.[0-9]+)", env_text or "", re.M)
    return match.group(1) if match else ""


def minor_step(old, new):
    """-> (ok, message). old/new are 'vMAJOR.MINOR', or '' when absent."""
    if not old:
        return True, None
    old_major, old_minor = old.split(".")[0], int(old.split(".")[1])
    new_major, new_minor = new.split(".")[0], int(new.split(".")[1])
    if old_major != new_major:
        return False, (f"KUBERNETES_VERSION changes major version ({old} -> {new}). This check only reasons "
                       f"about minor-version steps; a major bump needs manual confirmation that talosctl "
                       f"upgrade-k8s supports it at all.")
    delta = new_minor - old_minor
    if delta > 1:
        return False, (f"KUBERNETES_VERSION jumps {delta} minors ({old} -> {new}). talosctl upgrade-k8s only "
                       f"supports an N -> N or N -> N+1 step; land {old_major}.{old_minor + 1} first, then the "
                       f"next minor, and so on.")
    return True, f"  ok    {old} -> {new} (delta: {delta} minor)"


def base_ref():
    base = os.environ.get("BASE_REF", "")
    return f"origin/{base}"


# `git show` printing nothing must not read as "nothing to compare": a missing
# KUBERNETES_VERSION on an otherwise-valid base branch (this pin's own first PR)
# is a legitimate no-op, but a base ref that does not resolve at all (a fetch-depth
# problem, a typo carried from the event payload, a branch renamed or deleted
# mid-PR) is not, and both used to print the same "nothing to compare against" and
# exit 0 -- the same fail-open shape as the `|| true`-guarded grep and the
# hardcoded Plan-name loop fixed earlier in the same PR. Failing loudly on the
# unresolvable ref, and only that, keeps the distinction.

def cmd_minor_step():
    ref = base_ref()
    if not git_ref_exists(ref):
        print(f"::error::base ref '{ref}' does not resolve; the minor-skip guard cannot compare anything.")
        return 1
    old = minor_of(git_show(ref, VERSIONS_ENV_REL))
    new = minor_of(VERSIONS_ENV.read_text(encoding="utf-8"))
    if not new:
        print(f"::error::KUBERNETES_VERSION is missing from {VERSIONS_ENV_REL}")
        return 1
    if not old:
        print(f"No prior KUBERNETES_VERSION on {os.environ.get('BASE_REF', '')} -- nothing to compare against.")
        return 0
    ok, message = minor_step(old, new)
    print(message if ok else f"::error::{message}")
    return 0 if ok else 1


# ── downgrade ────────────────────────────────────────────────────────────────
#
# git-revert-triggered rollback (see versions.env) means reverting a version bump
# takes the exact same reviewed path forward bumps do. But Talos does not uniformly
# support downgrading across arbitrary version pairs, and a Kubernetes apiserver can
# refuse to serve objects a newer version already wrote in a newer schema -- so a
# decrease is not unconditionally safe the way a revert's mechanics imply, and gets
# flagged here rather than applying silently.

def version_tuple(version):
    return tuple(int(n) for n in re.findall(r"[0-9]+", version))


def is_downgrade(old, new):
    """True when new is strictly lower than old. Absent on either side is not a decrease."""
    if not old or not new or old == new:
        return False
    return version_tuple(new) < version_tuple(old)


def downgraded_keys(old_env, new_env, keys=("TALOS_VERSION", "KUBERNETES_VERSION")):
    return [k for k in keys if is_downgrade(old_env.get(k, ""), new_env.get(k, ""))]


def downgrade_verdict(old_env, new_env, labels):
    """-> (lines, ok)."""
    downgraded = downgraded_keys(old_env, new_env)
    if not downgraded:
        return ["No version decrease in this PR."], True
    names = " " + " ".join(downgraded)
    lines = [f"Version decrease detected in:{names}"]
    if "confirmed-downgrade" in labels:
        return lines + ["confirmed-downgrade label present -- proceeding."], True
    return lines + [f"::error::This PR decreases{names} in versions.env. Talos and Kubernetes do not uniformly "
                    f"support downgrading across arbitrary version pairs, so this needs the same explicit review "
                    f"a forward bump gets. Add the 'confirmed-downgrade' label once that review has happened."], False


def cmd_downgrade():
    ref = base_ref()
    if not git_ref_exists(ref):
        print(f"::error::base ref '{ref}' does not resolve; the downgrade guard cannot compare anything.")
        return 1
    # PR_LABELS travels via the environment, as JSON, rather than being
    # interpolated into a script: the script's quoting stays independent of
    # whatever a label happens to contain.
    lines, ok = downgrade_verdict(parse_env(git_show(ref, VERSIONS_ENV_REL)),
                                  parse_env(VERSIONS_ENV.read_text(encoding="utf-8")),
                                  json.loads(os.environ.get("PR_LABELS", "[]")))
    print("\n".join(lines))
    return 0 if ok else 1


COMMANDS = {"release": cmd_release, "minor-step": cmd_minor_step, "downgrade": cmd_downgrade}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: version_pins.py {'|'.join(COMMANDS)}")
    sys.exit(COMMANDS[sys.argv[1]]())
