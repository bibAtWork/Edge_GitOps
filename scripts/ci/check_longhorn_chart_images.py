#!/usr/bin/env python3
"""Require the pinned Longhorn images to match the exact chart release cohort."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "cluster/base/infrastructure/32-longhorn/helmrelease.yaml"
IMAGE_GROUPS = {
    "longhorn": ("manager", "engine", "instanceManager", "shareManager"),
    "csi": ("attacher", "provisioner", "resizer", "snapshotter",
            "nodeDriverRegistrar", "livenessProbe"),
}


def mismatches(pinned, chart_defaults):
    errors = []
    for group, components in IMAGE_GROUPS.items():
        for component in components:
            actual = pinned.get(group, {}).get(component, {})
            expected = chart_defaults[group][component]
            for field in ("repository", "tag"):
                if actual.get(field) != expected[field]:
                    errors.append(f"image.{group}.{component}.{field}: "
                                  f"pinned {actual.get(field)!r}, chart {expected[field]!r}")
    return errors


def check(helm="helm"):
    docs = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    repository = next(d["spec"]["url"] for d in docs if d.get("kind") == "HelmRepository")
    release = next(d for d in docs if d.get("kind") == "HelmRelease")
    chart = release["spec"]["chart"]["spec"]
    values = release["spec"]["values"]

    with tempfile.TemporaryDirectory(prefix="longhorn-chart-") as directory:
        env = os.environ.copy()
        for key, child in (("HELM_CACHE_HOME", "cache"),
                           ("HELM_CONFIG_HOME", "config"),
                           ("HELM_DATA_HOME", "data")):
            env[key] = str(Path(directory) / child)

        def run(*args):
            result = subprocess.run([helm, *args], capture_output=True,
                                    text=True, env=env)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout

        run("repo", "add", "longhorn", repository)
        chart_ref = f"longhorn/{chart['chart']}"
        version = chart["version"]
        defaults = yaml.safe_load(run("show", "values", chart_ref, "--version", version))
        errors = mismatches(values["image"], defaults["image"])
        assert not errors, "Longhorn chart/image cohort mismatch:\n" + "\n".join(errors)

        values_path = Path(directory) / "values.yaml"
        values_path.write_text(yaml.safe_dump(values), encoding="utf-8")
        rendered = run("template", "longhorn", chart_ref, "--version", version,
                       "--namespace", "longhorn-system", "--values", str(values_path))

    global_manager = next(d for d in yaml.safe_load_all(rendered)
                          if d and d.get("kind") == "Deployment"
                          and d["metadata"]["name"] == "longhorn-global-manager")
    container = global_manager["spec"]["template"]["spec"]["containers"][0]
    manager = values["image"]["longhorn"]["manager"]
    assert container["image"].endswith(
        f"/{manager['repository']}:{manager['tag']}"), "Global manager image ignores the pin"
    assert "global" in container["command"], "Global manager command changed; review the cohort"
    print(f"Longhorn {version} renders global-manager with its matching manager and CSI image cohort")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--helm", default="helm")
    check(parser.parse_args().helm)
