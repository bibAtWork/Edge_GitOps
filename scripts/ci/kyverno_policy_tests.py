#!/usr/bin/env python3
"""Evaluate repository CEL policies with the same Kyverno release as admission.

No cluster or secrets required. Namespace labels, operation and UserInfo are
explicit; the CLI builds admission requests (DELETE has a null object). A skipped
rule cannot pass a positive/negative case. Mutation compares the whole resulting
object, including preservation of explicitly chosen history limits.

CLI archives are verified against reviewed upstream SHA256 sums. When changing
the controller release, update kyverno-cli-checksums.json from the official
release checksums.txt. KYVERNO_CLI may name a locally installed matching binary.
RBAC, API defaulting and webhook registration still need live dry-run checks.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402
from kyverno_policy_cases import cases  # noqa: E402

POLICIES = ROOT / "cluster/base/infrastructure/19-kyverno/policies"


def check_coverage(policies, fixtures):
    """Every CEL admission policy must have fixtures; image trust has its own CI."""
    names = {p["metadata"]["name"] for p in policies
             if p.get("kind") in ("ValidatingPolicy", "MutatingPolicy")}
    covered = {c["policy"] for c in fixtures}
    errors = []
    if names != covered:
        errors.append(f"missing fixtures: {sorted(names - covered)}; unknown policies: {sorted(covered - names)}")
    for policy in policies:
        if policy.get("kind") == "ValidatingPolicy":
            expected = {c["expected"] for c in fixtures if c["policy"] == policy["metadata"]["name"]}
            if not {"pass", "fail"} <= expected:
                errors.append(f"{policy['metadata']['name']} needs both pass and fail fixtures")
    return errors


def judge_report(report, policy, expected, process="admission review"):
    results = report.get("results", [])
    if len(results) != 1 or results[0].get("policy") != policy:
        return "expected exactly one result for the tested policy"
    row = results[0]
    # The CLI report utility labels by the policy's background-enabled setting,
    # even though apply constructs admission requests for all Kubernetes CEL
    # policies. Identity policies disable background and must retain that setting.
    if row.get("properties", {}).get("process") != process:
        return f"expected report process {process}"
    if row.get("result") != expected:
        return f"expected {expected}, got {row.get('result')}: {row.get('message')}"
    return None


def judge_mutation(resource, expected):
    return None if resource == expected else "mutated resource differs from the entire expected object"


def judge_exclusion(returncode, stdout, stderr, expected):
    # matchConstraints exclusions emit no report; matchConditions exclusions
    # emit a skip result. Empty output is allowed ONLY for an expected exclusion
    # with a clean exit. Every validation policy also has evaluated pass/fail
    # fixtures, so a broken file cannot silently pass through exclusions alone.
    return None if expected == "skip" and returncode == 0 and not stdout.strip() and not stderr.strip() else "missing policy report"


def install_cli(directory, version):
    override = os.environ.get("KYVERNO_CLI")
    if override:
        binary = Path(override).resolve()
    else:
        system = platform.system().lower()
        if system not in ("linux", "windows") or platform.machine().lower() not in ("amd64", "x86_64"):
            raise ValueError("set KYVERNO_CLI to a matching binary on this platform")
        checksums = json.loads(Path(__file__).with_name("kyverno-cli-checksums.json").read_text())
        checksum = checksums.get(version, {}).get(f"{system}_x86_64")
        if not checksum:
            raise ValueError(f"review and record the upstream CLI checksum for {version} {system}")
        archive_name = f"kyverno-cli_{version}_{system}_x86_64." + ("zip" if system == "windows" else "tar.gz")
        url = f"https://github.com/kyverno/kyverno/releases/download/{version}/{archive_name}"
        with urllib.request.urlopen(url, timeout=60) as response:
            archive = response.read()
        if hashlib.sha256(archive).hexdigest() != checksum:
            raise ValueError("Kyverno CLI archive checksum mismatch")
        binary = directory / ("kyverno.exe" if system == "windows" else "kyverno")
        # Extract only the binary, never arbitrary archive paths.
        if system == "windows":
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                content = bundle.read("kyverno.exe")
        else:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
                content = bundle.extractfile("kyverno").read()
        binary.write_bytes(content)
        binary.chmod(0o755)
    found = subprocess.run([str(binary), "version"], capture_output=True, text=True, check=True).stdout
    if f"Version: {version.removeprefix('v')}\n" not in found.replace("\r\n", "\n"):
        raise ValueError(f"CLI must match admission controller {version}; got {found.strip()}")
    return binary


def evaluate(binary, policy_path, policy, case, directory):
    resource = directory / "resource.yaml"
    values = directory / "values.yaml"
    user = directory / "user.yaml"
    output = directory / "mutation.yaml"
    namespace = case["resource"]["metadata"].get("namespace", "default")
    resource.write_text(yaml.safe_dump(case["resource"]), encoding="utf-8")
    values.write_text(yaml.safe_dump({"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Values",
                                    "metadata": {"name": "fixture"},
                                    "namespaceSelector": [{"name": namespace, "labels": {
                                        "kubernetes.io/metadata.name": namespace}}],
                                    "globalValues": {"request.operation": case["operation"]}}), encoding="utf-8")
    user.write_text(yaml.safe_dump({"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "UserInfo",
                                  "metadata": {"name": "fixture"},
                                  "userInfo": {"username": case["username"]}}), encoding="utf-8")
    command = [str(binary), "apply", str(policy_path), "-r", str(resource), "-f", str(values),
               "-u", str(user), "--remove-color"]
    mutating = policy["kind"] == "MutatingPolicy"
    if mutating:
        output.unlink(missing_ok=True)
        command += ["--output", str(output)]
    else:
        if policy["spec"]["validationActions"] != [case["action"]]:
            return f"expected validationActions [{case['action']}]"
        command += ["--policy-report", "--output-format", "json"]
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
    # Denied fixtures can produce a nonzero exit. Only an actual fail report
    # counts; compilation/CLI errors never satisfy a negative fixture.
    try:
        if mutating:
            if process.returncode != 0 or not output.exists():
                return f"mutation failed: {process.stdout} {process.stderr}"
            docs = [d for d in yaml.safe_load_all(output.read_text(encoding="utf-8")) if d is not None]
            return judge_mutation(docs[0] if len(docs) == 1 else docs, case["expected"])
        if not process.stdout.strip():
            return judge_exclusion(process.returncode, process.stdout, process.stderr, case["expected"])
        label = "background scan" if policy["spec"].get("evaluation", {}).get("background", {}).get("enabled", True) else "admission review"
        return judge_report(json.loads(process.stdout), case["policy"], case["expected"], label)
    except (ValueError, OSError) as error:
        return f"invalid CLI output: {error}: {process.stdout[:300]} {process.stderr[:300]}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-dir", type=Path, default=POLICIES, help="alternate policies for regression verification")
    args = parser.parse_args()
    files = {}
    for path in args.policy_dir.glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc and doc.get("kind") in ("ValidatingPolicy", "MutatingPolicy"):
                files[doc["metadata"]["name"]] = (path, doc)
    fixtures = cases()
    errors = check_coverage([doc for _, doc in files.values()], fixtures)
    if errors:
        print("\n".join(errors))
        return 1
    release = yaml.safe_load((POLICIES.parent / "helmrelease.yaml").read_text(encoding="utf-8"))
    version = release["spec"]["values"]["admissionController"]["container"]["image"]["tag"]
    with tempfile.TemporaryDirectory(prefix="kyverno-policy-tests-") as temporary:
        directory = Path(temporary)
        binary = install_cli(directory, version)
        print(f"Evaluating {len(fixtures)} admission fixtures with Kyverno {version}")
        for case in fixtures:
            path, policy = files[case["policy"]]
            error = evaluate(binary, path, policy, case, directory)
            if error:
                errors.append(f"{case['policy']}: {case['label']}: {error}")
                print(f"FAIL {errors[-1]}")
    print(f"{len(fixtures) - len(errors)}/{len(fixtures)} fixtures passed across {len(files)} CEL policies")
    return int(bool(errors))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Policy regression check failed: {error}", file=sys.stderr)
        sys.exit(1)
