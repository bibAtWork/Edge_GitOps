"""Helpers shared by the CI checks in this directory (stdlib plus PyYAML).

These checks used to be shell and Python embedded in workflow YAML. Moved here
so they can be run locally, read as code, and tested -- see tests/. Each one is
a pure function over what it checks, plus a small main() that does the
rendering and printing.
"""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


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
