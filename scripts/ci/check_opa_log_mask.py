#!/usr/bin/env python3
"""Evaluate the exact ConfigMap policy against synthetic credential headers."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONFIGMAP = ROOT / "cluster/base/infrastructure/24-opa/configmap.yaml"


def check(opa="opa"):
    policy = yaml.safe_load(CONFIGMAP.read_text(encoding="utf-8"))["data"]["log-mask.rego"]
    headers = {
        "Authorization": "Bearer synthetic-credential-marker",
        "cookie": "session=synthetic-credential-marker",
        "Proxy-Authorization": "Bearer synthetic-credential-marker",
        "x-api-key": "synthetic-credential-marker",
        "x-request-id": "safe",
    }
    event = {"input": {"attributes": {"request": {"http": {"headers": headers}}}}}
    with tempfile.TemporaryDirectory(prefix="opa-mask-", dir=ROOT,
                                     ignore_cleanup_errors=True) as directory:
        rego_path = Path(directory) / "log-mask.rego"
        input_path = Path(directory) / "decision.json"
        rego_path.write_text(policy, encoding="utf-8")
        input_path.write_text(json.dumps(event), encoding="utf-8")
        result = subprocess.run(
            [opa, "eval", "--format", "json", "--data", str(rego_path),
             "--input", str(input_path), "data.system.log.mask"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        actual = set(json.loads(result.stdout)["result"][0]["expressions"][0]["value"])
    expected = {f"/input/attributes/request/http/headers/{name}" for name in headers
                if name != "x-request-id"}
    assert actual == expected, f"OPA masked {actual}, expected {expected}"
    print("OPA masks synthetic credential headers and keeps ordinary headers")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--opa", default="opa")
    check(parser.parse_args().opa)
