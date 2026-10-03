#!/usr/bin/env python3
"""Render the pinned Reloader chart and check the Deployment, not just values."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "cluster/base/infrastructure/09-reloader/helmrelease.yaml"
REPOSITORIES = ROOT / "cluster/base/infrastructure/sources/helm-repositories.yaml"


def check(helm="helm"):
    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    repositories = list(yaml.safe_load_all(REPOSITORIES.read_text(encoding="utf-8")))
    repo = next(r["spec"]["url"] for r in repositories
                if r.get("kind") == "HelmRepository" and r["metadata"]["name"] == "stakater")
    values = release["spec"]["values"]
    desired = values["reloader"]["deployment"]
    chart = release["spec"]["chart"]["spec"]

    with tempfile.TemporaryDirectory(prefix="reloader-render-", dir=ROOT,
                                     ignore_cleanup_errors=True) as directory:
        env = os.environ.copy()
        for key, child in (("HELM_CACHE_HOME", "cache"),
                           ("HELM_CONFIG_HOME", "config"),
                           ("HELM_DATA_HOME", "data")):
            env[key] = str(Path(directory) / child)
        values_path = Path(directory) / "values.yaml"
        values_path.write_text(yaml.safe_dump(values), encoding="utf-8")
        added = subprocess.run([helm, "repo", "add", "stakater", repo],
                               capture_output=True, text=True, env=env)
        assert added.returncode == 0, added.stderr
        result = subprocess.run(
            [helm, "template", "reloader", "stakater/" + chart["chart"],
             "--version", chart["version"], "--namespace", "kube-system",
             "--values", str(values_path)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, result.stderr
        rendered = result.stdout
    deployment = next(d for d in yaml.safe_load_all(rendered)
                      if d and d.get("kind") == "Deployment"
                      and d["metadata"]["name"] == "reloader-reloader")
    pod = deployment["spec"]["template"]["spec"]
    assert len(pod["containers"]) == 1, "Unexpected Reloader container layout"
    container = pod["containers"][0]
    for key, value in desired["securityContext"].items():
        assert pod["securityContext"][key] == value, f"Pod securityContext.{key} ignored"
    for key, value in desired["containerSecurityContext"].items():
        assert container["securityContext"][key] == value, f"Container securityContext.{key} ignored"
    assert container["resources"] == desired["resources"], "Reloader resources ignored"
    assert container["image"].endswith(":" + values["image"]["tag"]), "Reloader image pin ignored"
    print(f"Reloader {chart['version']} Deployment renders security contexts, resources and image pin")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    check(args.helm)
