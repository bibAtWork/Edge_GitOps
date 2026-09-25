"""Helpers shared by the CI checks in this directory (stdlib plus PyYAML).

These checks used to be shell and Python embedded in workflow YAML. Moved here
so they can be run locally, read as code, and tested -- see tests/. Each one is
a pure function over what it checks, plus a small main() that does the
rendering and printing.
"""
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[2]
VERSIONS_ENV = ROOT / "cluster/base/infrastructure/15-system-upgrade-controller/config/versions.env"


def kustomize(path):
    """Render a kustomization. -> (text, None) or (None, error message).

    LoadRestrictionsNone: the 1-node-config overlay's 19-kyverno/config reads
    its policies from outside its own component directory. Flux renders that
    fine, but plain `kubectl kustomize` refuses it.
    """
    result = subprocess.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(path)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout, None


def parse_docs(text):
    return [d for d in yaml.safe_load_all(text) if d]


def report(errors, header, success, footer=None):
    """Print the verdict the way every check here does; -> process exit code."""
    if not errors:
        print(success)
        return 0
    print(header.format(n=len(errors)))
    for error in errors:
        print(f"  ERROR: {error}")
    if footer:
        print()
        print(footer)
    return 1


def read_versions_env(path=VERSIONS_ENV):
    """KEY=value lines of versions.env -> dict. Comments and blank lines are skipped."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def git_show(ref, path):
    """Contents of path at ref, or None when the ref has no such file."""
    result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8")
    return result.stdout if result.returncode == 0 else None


def git_ref_exists(ref):
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=ROOT,
                          capture_output=True).returncode == 0


def fetch(url, data=None, headers=None, method=None, timeout=30):
    """-> (HTTP status, body). Status is None when nothing answered at all."""
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        return None, str(error).encode()


def probe(url, definitely_missing=(404,), attempts=3, delay=2.0, headers=None, fetch_fn=fetch):
    """Does url answer 200? -> ("exists" | "missing" | "unknown", last status).

    The three answers are different facts and callers must not blur them: a 404
    from a source that documents it means the thing is not there, while a rate
    limit, a 5xx or no answer at all says nothing about it. Only the latter are
    retried. (A 403 from GitHub's unauthenticated API on a shared runner once
    failed a check with "no tag v1.37.0" for a tag that exists.)
    """
    status = None
    for attempt in range(attempts):
        status, _ = fetch_fn(url, headers=headers)
        if status == 200:
            return "exists", status
        if status in definitely_missing:
            return "missing", status
        retryable = status is None or status in (403, 429) or status >= 500
        if not retryable:
            break
        if attempt + 1 < attempts:
            time.sleep(delay * (attempt + 1))
    return "unknown", status


def github_headers():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "edge-gitops-ci"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
