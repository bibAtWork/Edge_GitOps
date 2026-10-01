"""Route policies must preserve Gateway OPA authorization when attached."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("contracts", ROOT / "scripts/check-platform-contracts.py")
contracts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contracts)


class RouteExtAuth(unittest.TestCase):
    def test_route_policy_without_opa_fails(self):
        docs = contracts.render("cluster/overlays/1-node-config")
        docs += contracts.render("cluster/overlays/1-node")
        contracts.check_route_auth("1-node", docs)
        hubble = next(d for d in docs if d["kind"] == "SecurityPolicy"
                      and d["metadata"]["name"] == "hubble-oidc")
        del hubble["spec"]["extAuth"]
        with self.assertRaisesRegex(AssertionError, "repeat its OPA extAuth"):
            contracts.check_route_auth("1-node", docs)


if __name__ == "__main__":
    unittest.main()
