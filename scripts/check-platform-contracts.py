#!/usr/bin/env python3
"""Render and check contracts that schema validation cannot prove (requires PyYAML)."""
import ipaddress
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse
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


def check_kubernetes_oidc(profile, docs):
    """Keep the identity provider, API authenticator and RBAC grants aligned."""
    machine_config = ROOT / f"cluster/overlays/{profile}/talos-machineconfigs/controlplane.yaml"
    machine_docs = [d for d in yaml.safe_load_all(machine_config.read_text(encoding="utf-8")) if d]
    configs = [d for d in machine_docs if "cluster" in d]
    assert len(configs) == 1, f"{profile}: expected one Talos cluster machine config"
    args = configs[0]["cluster"]["apiServer"]["extraArgs"]
    expected_args = {
        "oidc-issuer-url": "https://keycloak.homelab.data-harness.org/realms/homelab",
        "oidc-client-id": "kubernetes",
        "oidc-username-claim": "email",
        "oidc-username-prefix": "oidc:",
        "oidc-groups-claim": "groups",
        "oidc-groups-prefix": "oidc:",
    }
    assert {key: args.get(key) for key in expected_args} == expected_args, \
        f"{profile}: Talos Kubernetes OIDC arguments drifted"

    expected_bindings = {
        "oidc-platform-admin-cluster-admin": ("oidc:platform-admin", "cluster-admin"),
        "oidc-viewer-view": ("oidc:viewer", "view"),
    }
    for name, (group, role) in expected_bindings.items():
        binding = get(docs, "ClusterRoleBinding", name)
        assert binding["roleRef"] == {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role,
        }, f"{profile}: {name} role changed"
        assert binding.get("subjects") == [{
            "kind": "Group", "name": group, "apiGroup": "rbac.authorization.k8s.io",
        }], f"{profile}: {name} subjects changed"

    cluster_groups = {
        subject.get("name")
        for d in docs if d["kind"] == "ClusterRoleBinding"
        for subject in d.get("subjects", []) if subject.get("kind") == "Group"
    }
    assert "oidc:app-operator" not in cluster_groups, \
        f"{profile}: app operators must remain namespace-scoped"
    assert "oidc:self-service" not in cluster_groups, \
        f"{profile}: self-service users must not receive Kubernetes cluster access"


ROUTE_AUTH = "gitops.homelab/auth"
ROUTE_AUTH_CLIENT = "gitops.homelab/auth-client"
ROUTE_PUBLIC_REASON = "gitops.homelab/public-reason"
ROUTE_AUTH_TYPES = {
    "native-oidc",        # the application signs users in against Keycloak itself
    "gateway-oidc",       # Envoy's oauth2 filter signs them in, and OPA gates the host
    "identity-provider",  # this is Keycloak, which has to be reachable to log in at all
    "deny",               # refused at the edge for everyone
    "redirect",           # only ever redirects, never reaches a backend
    "public",             # deliberately unauthenticated; must say why
}


def realm_clients():
    realm = yaml.safe_load(
        (ROOT / "cluster/base/infrastructure/26-keycloak/realm-config/homelab.yaml").read_text(encoding="utf-8"))
    return {client["clientId"]: client for client in realm["clients"]}


def opa_admin_only_hosts(docs):
    rego = get(docs, "ConfigMap", "opa-policies")["data"]["main.rego"]
    block = re.search(r"admin_only_apps := [{](.*?)[}]", rego, re.S)
    assert block, "OPA's admin_only_apps set moved or was renamed; update check_route_auth"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def check_route_auth(profile, docs):
    """Every HTTPRoute states how it authenticates, and the statement is checked.

    The Gateway's OPA ext_authz ends in `else := {"allowed": true}`: a request
    with no Authorization header passes, because browser apps do their own
    login. So a new route with neither a native login nor a gateway policy is
    public, and nothing says so -- no error, no alert, just a working URL. This
    makes the choice explicit and ties each claim to something that exists: the
    Keycloak client (and its redirect URI for that host) for OIDC routes, the
    SecurityPolicy and OPA's admin_only_apps for gateway-enforced ones, the
    deny policy or the redirect-only rules for the rest."""
    clients = realm_clients()
    admin_only = opa_admin_only_hosts(docs)
    routes = [d for d in docs if d["kind"] == "HTTPRoute"]
    assert routes, f"{profile}: no HTTPRoutes rendered; this check would prove nothing"
    policies = [d for d in docs if d["kind"] == "SecurityPolicy"]
    keycloak_host = urlparse(next(
        e["value"] for e in get(docs, "Deployment", "keycloak")["spec"]["template"]["spec"]["containers"][0]["env"]
        if e["name"] == "KC_HOSTNAME")).hostname
    gateway_hosts = set()

    for route in routes:
        namespace, name = route["metadata"]["namespace"], route["metadata"]["name"]
        who = f"{profile}: HTTPRoute {namespace}/{name}"
        annotations = route["metadata"].get("annotations", {})
        auth = annotations.get(ROUTE_AUTH)
        assert auth in ROUTE_AUTH_TYPES, (
            f"{who} must declare how it authenticates: annotation {ROUTE_AUTH} set to one of "
            f"{sorted(ROUTE_AUTH_TYPES)} (got {auth!r}). OPA lets a request with no Authorization "
            f"header through, so without a native login or a gateway policy this route is public.")
        hosts = route["spec"].get("hostnames", [])
        client_id = annotations.get(ROUTE_AUTH_CLIENT)
        mine = [p for p in policies if p["metadata"]["namespace"] == namespace and any(
            t["kind"] == "HTTPRoute" and t["name"] == name for t in p["spec"].get("targetRefs", []))]
        has_oidc = any("oidc" in p["spec"] for p in mine)

        if auth in ("native-oidc", "gateway-oidc"):
            assert hosts, f"{who} declares {auth} but has no hostnames"
            assert client_id in clients, (
                f"{who} declares {auth} with {ROUTE_AUTH_CLIENT}={client_id!r}, which is not a "
                f"client in realm-config/homelab.yaml (register it there)")
            registered = {urlparse(uri).hostname for uri in clients[client_id].get("redirectUris", [])
                          if urlparse(uri).scheme in ("http", "https")}
            assert set(hosts) <= registered, (
                f"{who} serves {sorted(set(hosts) - registered)} but Keycloak client {client_id!r} has no "
                f"redirect URI on that host, so nothing signs users in there")
        else:
            assert not client_id, f"{who} declares {auth}, which takes no {ROUTE_AUTH_CLIENT}"

        if auth == "gateway-oidc":
            assert has_oidc, f"{who} declares gateway-oidc but no SecurityPolicy.oidc targets it"
            gateway_hosts |= set(hosts)
        else:
            assert not has_oidc, f"{who} has a SecurityPolicy.oidc targeting it but declares {auth!r}"

        if auth == "identity-provider":
            assert hosts == [keycloak_host], \
                f"{who} declares identity-provider but Keycloak itself serves {keycloak_host}"
        elif auth == "deny":
            deny = [p["spec"]["authorization"] for p in mine if "authorization" in p["spec"]]
            assert len(deny) == 1 and deny[0].get("defaultAction") == "Deny" and not deny[0].get("rules"), \
                f"{who} declares deny but no SecurityPolicy refuses everything on it"
        elif auth == "redirect":
            assert route["spec"]["rules"] and all(
                not rule.get("backendRefs") and rule.get("filters")
                and all(f["type"] == "RequestRedirect" for f in rule["filters"])
                for rule in route["spec"]["rules"]), f"{who} declares redirect but a rule reaches a backend"
        elif auth == "public":
            assert annotations.get(ROUTE_PUBLIC_REASON, "").strip(), \
                f"{who} is deliberately public and must say why in {ROUTE_PUBLIC_REASON}"

    assert admin_only == gateway_hosts, (
        f"{profile}: OPA's admin_only_apps {sorted(admin_only)} must be exactly the hosts of the "
        f"gateway-oidc routes {sorted(gateway_hosts)} -- a host listed there without the route "
        f"declaring it (or the reverse) is gated by only one of the two layers")


# Talos's defaults; check_no_cluster_assigned_aliases asserts the machine configs
# do not override them, so these stay the truth about what the API server hands out.
CLUSTER_ASSIGNED = [ipaddress.ip_network("10.96.0.0/12"), ipaddress.ip_network("10.244.0.0/16")]


def host_aliases(node):
    """Every hostAliases entry anywhere in a manifest, HelmRelease values included."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "hostAliases":
                yield from value
            else:
                yield from host_aliases(value)
    elif isinstance(node, list):
        for item in node:
            yield from host_aliases(item)


def check_no_cluster_assigned_aliases(profile, docs):
    """No hostAliases in Git may name an address the API server assigns.

    hostAliases takes only a literal IP, which tempts a pin of a Service's
    ClusterIP -- but that is handed out when the Service is created, so a
    rebuild (exactly when the alias matters most) would ship a stale one with
    nothing to notice. argo-workflows.yaml did this for the Gateway's Service
    until its egress policy was fixed and the alias found unnecessary."""
    machine_config = ROOT / f"cluster/overlays/{profile}/talos-machineconfigs/controlplane.yaml"
    cluster = next(d["cluster"] for d in yaml.safe_load_all(machine_config.read_text(encoding="utf-8"))
                   if d and "cluster" in d)
    assert not {"podSubnets", "serviceSubnets"} & set(cluster.get("network", {})), \
        f"{profile}: the machine config overrides Talos's pod/service subnets; update CLUSTER_ASSIGNED"
    for d in docs:
        for alias in host_aliases(d):
            address = ipaddress.ip_address(alias["ip"])
            assert not any(address in network for network in CLUSTER_ASSIGNED), \
                (f"{profile}: {d['kind']}/{d['metadata']['name']} pins hostAliases {alias['ip']}, an "
                 f"address inside the cluster's service or pod range that is reassigned when the "
                 f"object behind it is recreated. Fix the network path instead of pinning it.")


def check_argo_operator_scope(profile, docs):
    """Argo's app-operator tier may start a recovery point, and nothing more.

    sso-rbac.yaml has to grant `create` on workflows and RBAC cannot say which
    one, so the boundary is two halves: the Role (what verbs exist at all) and
    Kyverno's argo-operator-workflow-scope (which Workflow a `create` may be).
    Argo's SSO mapping is a ServiceAccount annotation, not a RoleBinding, so
    check_kubernetes_oidc cannot see it -- this is what pins it instead."""
    role = get(docs, "Role", "argo-operator")
    for rule in role["rules"]:
        groups, resources, verbs = set(rule.get("apiGroups", [])), set(rule.get("resources", [])), set(rule["verbs"])
        assert "*" not in resources and "*" not in verbs and "*" not in groups, \
            f"{profile}: argo-operator must not use wildcards"
        assert not resources & {"secrets", "serviceaccounts", "roles", "rolebindings"}, \
            f"{profile}: argo-operator must not touch secrets or RBAC objects"
        if "argoproj.io" in groups:
            allowed = {"workflows": {"create", "get", "list", "watch", "delete"}}
            for resource in resources:
                assert verbs <= allowed.get(resource, {"get", "list", "watch"}), \
                    (f"{profile}: argo-operator may only create/delete workflows and read the "
                     f"rest of argoproj.io (update/patch would let an admitted Workflow be "
                     f"edited into another), got {sorted(verbs)} on {resource}")
    grants = [d for d in docs if d["kind"] in ("RoleBinding", "ClusterRoleBinding")
              and any(s.get("kind") == "ServiceAccount" and s.get("name") == "argo-operator"
                      for s in d.get("subjects", []))]
    assert [(g["kind"], g["roleRef"]["name"]) for g in grants] == [("RoleBinding", "argo-operator")], \
        f"{profile}: argo-operator must be bound to nothing but its own Role"

    argo = get(docs, "HelmRelease", "argo-workflows")["spec"]["values"]
    assert argo["controller"]["workflowRestrictions"]["templateReferencing"] == "Strict", \
        f"{profile}: the operator scope assumes templateReferencing: Strict"

    policy = get(docs, "ClusterPolicy", "argo-operator-workflow-scope")["spec"]
    assert policy["validationFailureAction"] == "Enforce", f"{profile}: operator scope must Enforce"
    assert len(policy["rules"]) >= 2, f"{profile}: operator scope lost a rule"
    for rule in policy["rules"]:
        [entry] = rule["match"]["any"]
        assert entry["subjects"] == [{"kind": "ServiceAccount", "name": "argo-operator",
                                      "namespace": "backup-system"}], \
            f"{profile}: {rule['name']} must judge argo-operator, and only it"
        assert entry["resources"]["kinds"] == ["argoproj.io/v1alpha1/Workflow"], \
            f"{profile}: {rule['name']} must match Workflows"
        assert entry["resources"]["operations"] == ["CREATE"], \
            f"{profile}: {rule['name']} must judge CREATE (argo-operator has no update or patch)"
    [pattern] = [r["validate"]["pattern"] for r in policy["rules"] if "pattern" in r.get("validate", {})]
    assert pattern["spec"]["workflowTemplateRef"] == {"name": "recovery-point"}, \
        f"{profile}: an operator may start recovery-point and nothing else"
    [parameter] = pattern["spec"]["arguments"]["parameters"]
    assert parameter["name"] == "application", f"{profile}: the one permitted argument is application"

    table = get(docs, "ConfigMap", "recovery-policy")["data"]["applications"]
    declared = {line.split("#")[0].split()[0] for line in table.splitlines() if line.split("#")[0].strip()}
    permitted = {name.strip() for name in parameter["value"].split("|")}
    assert permitted == declared, \
        (f"{profile}: argo-operator-workflow-scope permits {sorted(permitted)} but "
         f"recovery-policy declares {sorted(declared)} -- keep them the same")


def check_keycloak_kubernetes_client():
    realm_path = ROOT / "cluster/base/infrastructure/26-keycloak/realm-config/homelab.yaml"
    realm = yaml.safe_load(realm_path.read_text(encoding="utf-8"))
    clients = [client for client in realm["clients"] if client.get("clientId") == "kubernetes"]
    assert len(clients) == 1, "Keycloak must declare exactly one kubernetes client"
    client = clients[0]
    assert client.get("publicClient") is True, "kubectl's Kubernetes OIDC client must be public"
    assert client.get("standardFlowEnabled") is True
    for grant in ("implicitFlowEnabled", "directAccessGrantsEnabled",
                  "serviceAccountsEnabled", "authorizationServicesEnabled"):
        assert client.get(grant) is False, f"Kubernetes OIDC client unexpectedly enables {grant}"
    assert client.get("redirectUris"), "Kubernetes OIDC client needs a loopback redirect"
    redirects = [urlparse(uri) for uri in client["redirectUris"]]
    assert all(uri.scheme == "http" and uri.hostname in {"localhost", "127.0.0.1"}
               and uri.port and not uri.username and not uri.password for uri in redirects), \
        "Kubernetes OIDC redirects must remain loopback-only"
    assert client.get("attributes", {}).get("pkce.code.challenge.method") == "S256", \
        "The public Kubernetes OIDC client must require PKCE S256"

    setup_docs = list(yaml.safe_load_all(
        (ROOT / "cluster/base/infrastructure/26-keycloak/setup-job.yaml").read_text(encoding="utf-8")
    ))
    setup = next(d for d in setup_docs if d["kind"] == "ConfigMap"
                 and d["metadata"]["name"] == "keycloak-setup-script")
    script = setup["data"]["setup.py"]
    assert "link_default_scope(kubernetes_uuid, groups_scope_id)" in script, \
        "Kubernetes tokens must receive the groups claim without relying on an optional scope"


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
    check_kubernetes_oidc(profile, docs)
    check_argo_operator_scope(profile, docs)
    check_no_cluster_assigned_aliases(profile, docs)
    check_route_auth(profile, docs)

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
    check_keycloak_kubernetes_client()
    for profile in ("1-node", "3-node"):
        check_profile(profile)
    assert render("cluster/base/operator-readiness")
    assert render("cluster/templates/application")
    print("Operator marker and application template render successfully")
