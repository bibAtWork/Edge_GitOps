#!/usr/bin/env python3
"""
Live test of what Argo's app-operator tier may create in backup-system
(cluster/base/infrastructure/19-kyverno/policies/argo-operator-workflow-scope.yaml
plus argo-operator's Role in 37-backup-system/sso-rbac.yaml).

Impersonates the ServiceAccounts Argo itself impersonates for an SSO session
and submits Workflows with `kubectl create --dry-run=server`, so every request
goes through real RBAC and real admission but nothing is created and nothing
runs. Needs a kubeconfig that may impersonate ServiceAccounts (cluster-admin
does) -- run it from a workstation, no pod required:

  python3 scripts/test-argo-operator-scope.py

Stdlib only, like every other script here.
"""
import json
import subprocess
import sys

NS = "backup-system"
POLICY = "argo-operator-workflow-scope"
OPERATOR = f"system:serviceaccount:{NS}:argo-operator"
ADMIN = f"system:serviceaccount:{NS}:argo-admin"
VIEWER = f"system:serviceaccount:{NS}:argo-viewer"


def workflow(ref="recovery-point", parameters=None, **spec):
    body = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"generateName": "scope-test-", "namespace": NS},
        "spec": {"workflowTemplateRef": {"name": ref}, **spec},
    }
    if parameters is not None:
        body["spec"]["arguments"] = {"parameters": parameters}
    return body


def application(value):
    return [{"name": "application", "value": value}]


# (label, identity, workflow, expectation) where expectation is
#   "allowed"  -- the request is admitted
#   "policy"   -- refused by argo-operator-workflow-scope specifically
#   "rbac"     -- refused by RBAC (forbidden), never reaching admission
CASES = [
    *[(f"operator starts a recovery point for {app}", OPERATOR,
       workflow(parameters=application(app)), "allowed")
      for app in ("immich", "keycloak", "paperless", "seaweedfs")],

    ("operator cannot start retention", OPERATOR,
     workflow("retention", [{"name": "repository", "value": "aws"}, {"name": "dry-run", "value": "false"}]),
     "policy"),
    ("operator cannot start promote", OPERATOR,
     workflow("promote", application("immich")), "policy"),
    ("operator cannot start a drill", OPERATOR,
     workflow("drill-filer-aws"), "policy"),
    ("operator cannot start reconcile with its timings changed", OPERATOR,
     workflow("reconcile", [{"name": "promote-after-hours", "value": "0"}]), "policy"),
    ("operator cannot name an unknown application", OPERATOR,
     workflow(parameters=application("nextcloud")), "policy"),
    ("operator cannot inject shell through the application argument", OPERATOR,
     workflow(parameters=application('immich"; id; "')), "policy"),
    ("operator cannot inject shell with a command substitution", OPERATOR,
     workflow(parameters=application("$(id)")), "policy"),
    ("operator cannot omit the application", OPERATOR,
     workflow(parameters=[]), "policy"),
    ("operator cannot omit arguments entirely", OPERATOR, workflow(), "policy"),
    ("operator cannot add a second argument", OPERATOR,
     workflow(parameters=application("immich") + [{"name": "extra", "value": "x"}]), "policy"),
    ("operator cannot override the entrypoint", OPERATOR,
     workflow(parameters=application("immich"), entrypoint="protect-dataset"), "policy"),
    # These two need no inputs, so Argo's own validation lets them through and
    # only this policy stands between a submitter and a template that deletes
    # every recovery clone / every restore archive with the backup credentials.
    ("operator cannot make remove-clones the entrypoint", OPERATOR,
     workflow(parameters=application("immich"), entrypoint="remove-clones"), "policy"),
    ("operator cannot make purge-restore-archives the entrypoint", OPERATOR,
     workflow(parameters=application("immich"), entrypoint="purge-restore-archives"), "policy"),
    ("operator cannot restate the default entrypoint either", OPERATOR,
     workflow(parameters=application("immich"), entrypoint="main"), "policy"),
    ("operator cannot set a service account", OPERATOR,
     workflow(parameters=application("immich"), serviceAccountName="argo-admin"), "policy"),
    ("operator cannot add inline templates", OPERATOR,
     workflow(parameters=application("immich"),
              templates=[{"name": "x", "container": {"image": "alpine", "command": ["id"]}}]), "policy"),
    ("operator cannot take an argument from a Secret or ConfigMap", OPERATOR,
     workflow(parameters=[{"name": "application", "value": "immich",
                           "valueFrom": {"configMapKeyRef": {"name": "x", "key": "y"}}}]), "policy"),
    ("operator cannot pass artifacts", OPERATOR,
     {**workflow(),
      "spec": {"workflowTemplateRef": {"name": "recovery-point"},
               "arguments": {"parameters": application("immich"),
                             "artifacts": [{"name": "a", "raw": {"data": "x"}}]}}},
     "policy"),
    ("operator cannot reference a cluster-scoped template", OPERATOR,
     {**workflow(parameters=application("immich")),
      "spec": {"workflowTemplateRef": {"name": "recovery-point", "clusterScope": True},
               "arguments": {"parameters": application("immich")}}}, "policy"),

    ("platform-admin is not restricted: retention dry run", ADMIN,
     workflow("retention", [{"name": "repository", "value": "local"}, {"name": "dry-run", "value": "true"}]),
     "allowed"),
    ("platform-admin is not restricted: entrypoint override", ADMIN,
     workflow(parameters=application("immich"), entrypoint="main"), "allowed"),
    ("viewer cannot create workflows at all", VIEWER,
     workflow(parameters=application("immich")), "rbac"),
]

def submit(identity, body):
    result = subprocess.run(
        ["kubectl", "create", "--dry-run=server", "-o", "name", "--as", identity, "-f", "-"],
        input=json.dumps(body), capture_output=True, text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def judge(expectation, returncode, output):
    if expectation == "allowed":
        return returncode == 0, "admitted"
    if expectation == "policy":
        return returncode != 0 and POLICY in output, f"refused by {POLICY}"
    return returncode != 0 and "forbidden" in output.lower() and POLICY not in output, "refused by RBAC"


def main():
    failures = 0
    for label, identity, body, expectation in CASES:
        returncode, output = submit(identity, body)
        ok, wanted = judge(expectation, returncode, output)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures += 1
            print(f"         wanted: {wanted}")
            print(f"         got (exit {returncode}): {output[:400]}")
    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{failures} CHECK(S) FAILED"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
