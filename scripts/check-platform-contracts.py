#!/usr/bin/env python3
"""Render and check contracts that schema validation cannot prove (requires PyYAML)."""
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFERRED_GROUPS = {
    "cilium.io", "postgresql.cnpg.io", "barmancloud.cnpg.io", "argoproj.io",
    "operator.victoriametrics.com", "longhorn.io", "gateway.networking.k8s.io",
}


def render(path):
    result = subprocess.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(path)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def get(docs, kind, name):
    matches = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def acyclic(graph):
    def visit(node, stack):
        assert node not in stack, f"Dependency cycle: {stack + [node]}"
        for dep in graph.get(node, []):
            assert dep in graph, f"Missing dependency: {dep}"
            visit(dep, stack + [node])
    for node in graph:
        visit(node, [])


def check_profile(profile):
    root = render(f"cluster/overlays/{profile}")
    config = render(f"cluster/overlays/{profile}-config")
    docs = root + config
    identities = [(d["apiVersion"].split("/")[0], d["kind"],
                   d["metadata"].get("namespace"), d["metadata"]["name"]) for d in docs]
    assert len(identities) == len(set(identities)), "Duplicate ownership across Flux inventories"
    for d in root:
        assert d["apiVersion"].split("/")[0] not in DEFERRED_GROUPS, d["metadata"]
    bindings = [d for d in docs if d["kind"] in ("RoleBinding", "ClusterRoleBinding")
                and any(s.get("name") == "oidc:app-operator" for s in d.get("subjects", []))]
    bound_namespaces = {b["metadata"].get("namespace") for b in bindings}
    assert {"immich", "paperless"} <= bound_namespaces
    application_namespaces = {d["metadata"]["name"] for d in docs if d["kind"] == "Namespace"
                              and d["metadata"].get("labels", {}).get("homelab.local/application") == "true"}
    platform_namespaces = {d["metadata"]["name"] for d in root if d["kind"] == "Namespace"} - {"immich", "paperless"}
    assert bound_namespaces <= application_namespaces
    assert not bound_namespaces & platform_namespaces
    assert all(b["kind"] == "RoleBinding" and b["roleRef"]["name"] == "edit" for b in bindings)

    gate = get(root, "Kustomization", "operators-ready")["spec"]
    cfg = get(root, "Kustomization", "config")["spec"]
    assert cfg["dependsOn"] == [{"name": "operators-ready"}]
    assert gate["dependsOn"] == [{"name": "flux-system"}]
    assert not gate.get("wait"), "wait:true overrides explicit healthChecks"
    roots = [d for d in root if d["kind"] == "Kustomization" and d["metadata"]["name"] == "flux-system"]
    assert all(not d["spec"].get("wait") and not d["spec"].get("healthChecks") for d in roots)
    releases = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in root if d["kind"] == "HelmRelease"}
    graph = {key: [(dep.get("namespace", key[0]), dep["name"]) for dep in d["spec"].get("dependsOn", [])]
             for key, d in releases.items()}
    acyclic(graph)
    checked = {(h["namespace"], h["name"]) for h in gate["healthChecks"]}
    assert checked <= releases.keys()
    assert {("cnpg-system", "cloudnative-pg"), ("cnpg-system", "plugin-barman-cloud"),
            ("backup-system", "argo-workflows"), ("monitoring", "vmstack"),
            ("kube-system", "cilium"), ("cert-manager", "cert-manager")} <= checked
    forbidden = {("seaweedfs", "seaweedfs"), ("immich", "immich"), ("zot", "zot")}
    def check_gate_dependency(key):
        assert key not in forbidden, f"Gate waits on config consumer {key}"
        for dep in graph[key]:
            check_gate_dependency(dep)
    for key in checked:
        check_gate_dependency(key)

    otel = yaml.safe_load(get(root, "ConfigMap", "otel-gateway-config")["data"]["config.yaml"])
    for signal in ("metrics", "logs", "traces"):
        pipeline = otel["service"]["pipelines"][signal]
        assert "otlp" in pipeline["receivers"]
        assert all(e in otel["exporters"] for e in pipeline["exporters"])
    assert set(otel["receivers"]["otlp"]["protocols"]) == {"grpc", "http"}
    service = get(root, "Service", "otel-collector-gateway")
    assert {p["port"] for p in service["spec"]["ports"]} == {4317, 4318}
    for kind, name in (("Deployment", "otel-collector-gateway"), ("DaemonSet", "otel-agent")):
        assert get(root, kind, name)["metadata"]["annotations"]["reloader.stakater.com/auto"] == "true"
    ingress = get(config, "CiliumNetworkPolicy", "allow-otlp-gateway-ingress")["spec"]["ingress"][0]
    assert {p["port"] for p in ingress["toPorts"][0]["ports"]} == {"4317", "4318"}
    client = ingress["fromEndpoints"][0]
    assert client["matchLabels"]["homelab.local/telemetry-client"] == "true"
    assert {"key": "io.kubernetes.pod.namespace", "operator": "Exists"} in client["matchExpressions"]
    expected = {"monitoring", "zot", "kube-system", "gateway-system", "immich", "paperless", "keycloak", "kubeopencode-system"}
    actual = {d["metadata"]["name"] for d in docs if d["kind"] == "Namespace"
              and d["metadata"].get("labels", {}).get("homelab.local/gateway-access") == "true"}
    assert expected <= actual, ("Existing gateway namespace lost capability", expected - actual)
    assert actual - expected <= application_namespaces, "New gateway capability must belong to an application"
    print(f"{profile}: {len(root)} root + {len(config)} config resources; contracts passed")


if __name__ == "__main__":
    for profile in ("1-node", "3-node"):
        check_profile(profile)
    assert render("cluster/base/operator-readiness")
    assert render("cluster/templates/application")
    print("Operator marker and application template render successfully")
