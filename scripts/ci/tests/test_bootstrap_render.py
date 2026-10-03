"""A changed bootstrap LAN and domain must survive both rendered profiles."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("apply_config", ROOT / "bootstrap/scripts/apply-config.py")
apply_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_config)


class BootstrapRender(unittest.TestCase):
    def test_three_node_bootstrap_has_schedulable_nodes(self):
        script = (ROOT / "bootstrap/scripts/bootstrap-3node.sh").read_text(encoding="utf-8")
        machine = (ROOT / "cluster/overlays/3-node/talos-machineconfigs/controlplane.yaml").read_text(encoding="utf-8")
        self.assertIn('for node in "$NODE1_IP" "$NODE2_IP" "$NODE3_IP"; do', script)
        self.assertIn('.talos/generated/controlplane.yaml', script)
        self.assertIn("allowSchedulingOnControlPlanes: true", machine)

    def test_nondefault_values_render_across_profiles(self):
        # Keep this short: the repository may already live near MAX_PATH on Windows.
        root = ROOT / f"t{uuid.uuid4().hex[:4]}"
        root.mkdir()
        try:
            cluster = root / "cluster"
            cluster.mkdir()
            sources = {
                "talos1.yaml": "overlays/1-node/talos-machineconfigs/controlplane.yaml",
                "talos3.yaml": "overlays/3-node/talos-machineconfigs/controlplane.yaml",
                "lb1.yaml": "overlays/1-node-config/lb-ipam.yaml",
                "lb3.yaml": "overlays/3-node-config/lb-ipam.yaml",
                "host.yaml": "base/infrastructure/10-network-policies/host-ingress.yaml",
                "opa.yaml": "base/infrastructure/24-opa/configmap.yaml",
                "hubble-route.yaml": "base/infrastructure/05-cilium/config/httproute.yaml",
                "hubble-auth.yaml": "base/infrastructure/05-cilium/config/hubble-securitypolicy.yaml",
                "realm.yaml": "base/infrastructure/26-keycloak/realm-configmap.yaml",
                "keycloak.yaml": "base/infrastructure/26-keycloak/deployment.yaml",
                "grafana.yaml": "base/infrastructure/04-grafana/helmrelease.yaml",
                "cert.yaml": "base/infrastructure/11-ingress-gateway/wildcard-cert.yaml",
                "tailscale.yaml": "base/infrastructure/14-tailscale-operator/config/subnet-router-hostnetwork.yaml",
                "canary-route.yaml": "base/applications/canary/route.yaml",
                "immich-route.yaml": "base/infrastructure/16-immich/config/httproute.yaml",
                "paperless-route.yaml": "base/infrastructure/17-paperless-ngx/config/httproute.yaml",
                "argo-route.yaml": "base/infrastructure/37-backup-system/config/httproute.yaml",
                "zot-route.yaml": "base/infrastructure/12-zot/config/httproute.yaml",
            }
            for name, source in sources.items():
                shutil.copy2(ROOT / "cluster" / source, cluster / name)
            (root / "bootstrap").mkdir()
            shutil.copy2(ROOT / "bootstrap/rendered-values.json", root / "bootstrap/rendered-values.json")
            original = json.loads((root / "bootstrap/rendered-values.json").read_text(encoding="utf-8"))
            custom = {
                "effective_domain": "lab.example.test",
                "gateway_ip": "10.77.8.200",
                "subnet": "10.77.8.0/24",
            }
            changed = apply_config.render_cluster_values(root, custom)
            self.assertGreater(len(changed), 15)
            self.assertEqual(apply_config.render_cluster_values(root, custom), [])
            self.assertEqual(json.loads((root / "bootstrap/rendered-values.json").read_text()), custom)

            for profile in ("1-node", "3-node"):
                resources = [name for name in sources if not name.startswith("talos")
                             and name not in ("lb1.yaml", "lb3.yaml")]
                resources.append("lb1.yaml" if profile == "1-node" else "lb3.yaml")
                (cluster / "kustomization.yaml").write_text(
                    "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n"
                    + "".join(f"  - {name}\n" for name in resources), encoding="utf-8")
                rendered = subprocess.run(
                    ["kubectl", "kustomize", str(cluster)],
                    capture_output=True, text=True, encoding="utf-8", check=True,
                ).stdout
                self.assertNotIn(original["effective_domain"], rendered)
                self.assertNotIn(original["gateway_ip"], rendered)
                self.assertNotIn(original["subnet"], rendered)
                self.assertIn(custom["effective_domain"], rendered)
                self.assertIn(custom["gateway_ip"], rendered)
                self.assertIn(custom["subnet"], rendered)
                machine = (cluster / ("talos1.yaml" if profile == "1-node" else "talos3.yaml")).read_text(encoding="utf-8")
                self.assertIn(f"validSubnets:\n        - {custom['subnet']}", machine)
                self.assertIn(f"advertisedSubnets:\n      - {custom['subnet']}", machine)

            host_policy = (cluster / "host.yaml").read_text(encoding="utf-8")
            self.assertEqual(host_policy.count(f"cidr: {custom['subnet']}"), 2)
            opa = (cluster / "opa.yaml").read_text(encoding="utf-8")
            self.assertIn(custom["effective_domain"], opa)
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
