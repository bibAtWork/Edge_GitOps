"""Extract the deployed policy and require it to match the reviewed source."""
import sys
from pathlib import Path
import yaml


def policy_from_manifest(text):
    manifest = yaml.safe_load(text)
    return manifest["data"]["analytics-trino.rego"]


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    policy = policy_from_manifest((root / "cluster/base/infrastructure/24-opa/configmap.yaml").read_text())
    assert policy == (root / "analytics/trino.rego").read_text(), "Deployed OPA policy differs from source"
    Path(sys.argv[1]).write_text(policy)
