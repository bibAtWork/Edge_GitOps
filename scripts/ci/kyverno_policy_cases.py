"""Admission fixtures and expected results, independent of the CEL expressions.

The offline engine does not run Kubernetes defaulting or RBAC. Deployments
therefore explicitly contain the API default (10). Live RBAC/webhook tests stay
in scripts/test-argo-operator-scope.py; they complement this fast PR check.
"""
from copy import deepcopy

SYSTEM_NAMESPACES = ("kube-system", "flux-system", "kyverno")
BACKUP = "system:serviceaccount:backup-system:backup-workflow"
OPERATOR = "system:serviceaccount:backup-system:argo-operator"


def pod():
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "fixture", "namespace": "default"},
            "spec": {"securityContext": {"runAsNonRoot": True, "fsGroup": 1000},
                     "containers": [{"name": "main", "image": "docker.io/library/nginx:1.27.5",
                                     "resources": {"limits": {"memory": "32Mi"}},
                                     "securityContext": {"readOnlyRootFilesystem": True},
                                     "readinessProbe": {"exec": {"command": ["true"]}}}]}}


def controller(kind="Deployment"):
    return {"apiVersion": "apps/v1", "kind": kind,
            "metadata": {"name": "fixture", "namespace": "default"},
            "spec": {"revisionHistoryLimit": 10, "template": {"spec": pod()["spec"]}}}


def recovery(kind="PersistentVolume", name="rpc-fixture"):
    return {"apiVersion": "v1" if kind == "PersistentVolume" else "longhorn.io/v1beta2",
            "kind": kind, "metadata": {"name": name},
            "spec": {"csi": {"volumeHandle": "rpc-fixture"}} if kind == "PersistentVolume"
            else {"dataSource": "snap://snapshot-fixture"} if kind == "Volume" else {}}


def workflow(application="immich"):
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow",
            "metadata": {"name": "fixture", "namespace": "backup-system"},
            "spec": {"workflowTemplateRef": {"name": "recovery-point"},
                     "arguments": {"parameters": [{"name": "application", "value": application}]}}}


def cases():
    result = []

    def add(policy, label, resource, expected, operation="CREATE", username="fixture-user", **extra):
        result.append(dict(policy=policy, label=label, resource=deepcopy(resource), expected=expected,
                           operation=operation, username=username, **extra))

    # A failure in an Audit policy is still a policy failure, not a denial.
    workload = {"disallow-latest-tag": "Deny", "restrict-image-registries": "Deny",
                "require-resource-limits": "Audit", "require-readonly-rootfs": "Audit"}
    for policy, action in workload.items():
        for operation in ("CREATE", "UPDATE"):
            add(policy, f"compliant {operation}", pod(), "pass", operation, action=action)
        bad = pod()
        c = bad["spec"]["containers"][0]
        if policy == "disallow-latest-tag":
            c["image"] = "docker.io/library/nginx:latest"
        elif policy == "restrict-image-registries":
            c["image"] = "untrusted.example/nginx:1.0"
        elif policy == "require-resource-limits":
            c["resources"] = {"limits": {"cpu": "1"}}
        else:
            c["securityContext"] = {"readOnlyRootFilesystem": False}
        add(policy, "noncompliant", bad, "fail", action=action)
        for namespace in SYSTEM_NAMESPACES:
            bad["metadata"]["namespace"] = namespace
            add(policy, f"excluded namespace {namespace}", bad, "skip", action=action)
        bad["metadata"]["namespace"] = "default"
        add(policy, "DELETE is outside workload rule", bad, "skip", "DELETE", action=action)
        if policy != "require-readonly-rootfs":
            init = pod()
            init["spec"]["initContainers"] = [c]
            add(policy, "noncompliant init image/container", init, "fail", action=action)

    for image, expected in (("nginx", "fail"), ("registry.example:5000/nginx", "fail"),
                            ("registry.example:5000/nginx:1", "pass"),
                            ("nginx@sha256:" + "a" * 64, "pass")):
        obj = pod()
        obj["spec"]["containers"][0]["image"] = image
        add("disallow-latest-tag", f"tag syntax {image[:45]}", obj, expected, action="Deny")
    for image, expected in (("ghcr.io/owner/app:1", "pass"), ("reg.kyverno.io/kyverno/kyverno:v1", "pass"),
                            ("tenant.azurecr.io/app:1", "pass"), ("ghcr.io.attacker.example/app:1", "fail"),
                            ("tenant.azurecr.io.attacker.example/app:1", "fail")):
        obj = pod()
        obj["spec"]["containers"][0]["image"] = image
        add("restrict-image-registries", f"registry {image}", obj, expected, action="Deny")

    for kind in ("Deployment", "StatefulSet", "DaemonSet"):
        obj = controller(kind)
        add("require-probes", f"{kind} with readiness", obj, "pass", action="Audit")
        c = obj["spec"]["template"]["spec"]["containers"][0]
        c["livenessProbe"] = c.pop("readinessProbe")
        add("require-probes", f"{kind} with liveness", obj, "pass", "UPDATE", action="Audit")
        c.pop("livenessProbe")
        add("require-probes", f"{kind} without probes", obj, "fail", action="Audit")
    obj = controller()
    obj["spec"]["template"]["spec"]["containers"].append({"name": "sidecar", "image": "nginx:1"})
    add("require-probes", "every regular container needs a probe", obj, "fail", action="Audit")
    for namespace in SYSTEM_NAMESPACES:
        obj["metadata"]["namespace"] = namespace
        add("require-probes", f"excluded {namespace}", obj, "skip", action="Audit")

    obj = pod()
    add("restrict-hostpath-volumes", "no volumes", obj, "pass", action="Audit")
    obj["spec"]["volumes"] = [{"name": "data", "emptyDir": {}}]
    add("restrict-hostpath-volumes", "emptyDir", obj, "pass", "UPDATE", action="Audit")
    obj["spec"]["volumes"].append({"name": "host", "hostPath": {"path": "/tmp"}})
    add("restrict-hostpath-volumes", "hostPath among safe volumes", obj, "fail", action="Audit")
    for namespace in ("longhorn-system", "kube-system", "falco", "monitoring-agents", "cattle-system",
                      "tailscale", "seaweedfs", "flux-system", "kyverno"):
        obj["metadata"]["namespace"] = namespace
        add("restrict-hostpath-volumes", f"excluded {namespace}", obj, "skip", action="Audit")

    policy = "require-fsgroup-for-ephemeral-volumes"
    obj = pod()
    add(policy, "no ephemeral volume", obj, "skip", action="Deny")
    obj["spec"]["volumes"] = [{"name": "data", "ephemeral": {"volumeClaimTemplate": {
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}}}]
    add(policy, "nonroot ephemeral with fsGroup", obj, "pass", action="Deny")
    obj["spec"]["securityContext"].pop("fsGroup")
    add(policy, "nonroot ephemeral without fsGroup", obj, "fail", action="Deny")
    add(policy, "UPDATE without fsGroup", obj, "fail", "UPDATE", action="Deny")
    for namespace in SYSTEM_NAMESPACES:
        obj["metadata"]["namespace"] = namespace
        add(policy, f"excluded {namespace}", obj, "skip", action="Deny")
    obj["metadata"]["namespace"] = "default"
    obj["spec"]["securityContext"]["runAsNonRoot"] = False
    add(policy, "root workload is outside match condition", obj, "skip", action="Deny")

    policy = "backup-system-clone-scope"
    for kind in ("PersistentVolume", "Volume", "Snapshot"):
        for operation in ("CREATE", "UPDATE", "DELETE"):
            add(policy, f"{kind} clone {operation}", recovery(kind), "pass", operation, BACKUP, action="Deny")
            add(policy, f"{kind} production {operation}", recovery(kind, "production-fixture"),
                "fail", operation, BACKUP, action="Deny")
        add(policy, f"{kind} other identity", recovery(kind, "production-fixture"), "skip", username="admin", action="Deny")
    for kind in ("PersistentVolume", "Volume"):
        obj = recovery(kind)
        obj["spec"] = {}
        add(policy, f"{kind} missing clone source", obj, "fail", username=BACKUP, action="Deny")
        add(policy, f"{kind} DELETE uses request name, needs no spec", obj, "pass", "DELETE", BACKUP, action="Deny")
        obj["spec"] = {"csi": {"volumeHandle": "production"}} if kind == "PersistentVolume" else {"dataSource": "backup://production"}
        add(policy, f"{kind} wrong clone source", obj, "fail", username=BACKUP, action="Deny")

    policy = "argo-operator-workflow-scope"
    for app in ("immich", "paperless", "keycloak", "seaweedfs"):
        add(policy, f"approved app {app}", workflow(app), "pass", username=OPERATOR, action="Deny")
    for app in ("unknown", "$(id)", 'immich"; id; "'):
        add(policy, f"unapproved app {app}", workflow(app), "fail", username=OPERATOR, action="Deny")
    for ref in ("retention", "promote", "reconcile", "drill-filer-aws"):
        obj = workflow()
        obj["spec"]["workflowTemplateRef"]["name"] = ref
        add(policy, f"other template {ref}", obj, "fail", username=OPERATOR, action="Deny")
    for key, value in (("entrypoint", "remove-clones"), ("serviceAccountName", "argo-admin"),
                       ("templates", [{"name": "inline"}])):
        obj = workflow()
        obj["spec"][key] = value
        add(policy, f"extra spec field {key}", obj, "fail", username=OPERATOR, action="Deny")
    for label, arguments in (("missing arguments", None), ("no application", {"parameters": []}),
                             ("second parameter", {"parameters": [{"name": "application", "value": "immich"}, {"name": "extra", "value": "x"}]}),
                             ("valueFrom", {"parameters": [{"name": "application", "value": "immich", "valueFrom": {"path": "/tmp/x"}}]}),
                             ("artifacts", {"parameters": [{"name": "application", "value": "immich"}], "artifacts": []})):
        obj = workflow()
        obj["spec"].pop("arguments")
        if arguments is not None:
            obj["spec"]["arguments"] = arguments
        add(policy, label, obj, "fail", username=OPERATOR, action="Deny")
    obj = workflow()
    obj["spec"]["workflowTemplateRef"]["clusterScope"] = True
    add(policy, "cluster template option", obj, "fail", username=OPERATOR, action="Deny")
    for identity in ("admin", "system:serviceaccount:backup-system:argo-admin"):
        add(policy, f"other identity {identity}", workflow("unknown"), "skip", username=identity, action="Deny")
    for operation in ("UPDATE", "DELETE"):
        add(policy, f"outside CREATE rule {operation}", workflow("unknown"), "skip", operation, OPERATOR, action="Deny")

    for operation in ("CREATE", "UPDATE"):
        for history in (10, 2, 0):
            obj = controller()
            obj["spec"]["revisionHistoryLimit"] = history
            expected = deepcopy(obj)
            expected["spec"]["revisionHistoryLimit"] = 3 if history == 10 else history
            add("set-revision-history-limit", f"history {history} {operation}", obj, expected, operation)
    for namespace in SYSTEM_NAMESPACES:
        obj = controller()
        obj["metadata"]["namespace"] = namespace
        add("set-revision-history-limit", f"excluded {namespace}", obj, deepcopy(obj))
    return result
