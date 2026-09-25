#!/usr/bin/env python3
"""
Reproducible end-to-end test of Argo Workflows' SSO RBAC tiers
(cluster/base/infrastructure/37-backup-system/sso-rbac.yaml).

Creates (idempotently) one test user per non-admin ADR-002 tier, logs
each one in through the REAL Keycloak authorization-code flow against
the REAL argo-workflows OIDC client, and checks the REAL K8s-backed API
responses Argo returns for a read and a write in two resource types --
and, for the operator tier, that it can start a recovery point and
nothing else (19-kyverno/policies/argo-operator-workflow-scope.yaml). One
recovery point of keycloak is really started per run and finishes on its own;
every other create is refused. The policy's own wider case matrix, without the
SSO round trip and without starting anything, is scripts/test-argo-operator-scope.py.

Not runnable from a laptop: needs (a) the Gateway reachable on the ports the
cluster uses for it (a pod carrying the argo-workflows-server label gets that
from allow-argo-server-gateway-egress, and resolves both hostnames by
ordinary DNS, exactly as the server itself does) and (b) direct egress to
Keycloak's admin API for test-user setup, which nothing outside the keycloak
namespace has by default (that policy only opens port 10443 to Envoy, not
Keycloak). Run from inside the cluster, temporarily granted (b):

  kubectl run rbac-test -n backup-system --restart=Never \
    --image=python:3.12-slim \
    --overrides='{"metadata":{"labels":{"app.kubernetes.io/name":"argo-workflows-server"}}}' \
    --env="KEYCLOAK_ADMIN_USER=admin@homelab.internal" \
    --env="KEYCLOAK_ADMIN_PASSWORD=$(kubectl get secret keycloak-admin-user -n keycloak -o jsonpath='{.data.password}' | base64 -d)" \
    --command -- sleep 3600
  kubectl exec -i rbac-test -n backup-system -- sh -c "cat > /tmp/t.py" < scripts/test-argo-sso-rbac.py

  kubectl apply -f - <<'EOF'
  apiVersion: cilium.io/v2
  kind: CiliumNetworkPolicy
  metadata: {name: temp-rbac-test-keycloak-egress, namespace: backup-system}
  spec:
    endpointSelector: {matchLabels: {app.kubernetes.io/name: argo-workflows-server}}
    egress:
      - toEndpoints: [{matchLabels: {app: keycloak, io.kubernetes.pod.namespace: keycloak}}]
        toPorts: [{ports: [{port: "8080", protocol: TCP}]}]
  ---
  apiVersion: cilium.io/v2
  kind: CiliumNetworkPolicy
  metadata: {name: temp-rbac-test-keycloak-ingress, namespace: keycloak}
  spec:
    endpointSelector: {matchLabels: {app: keycloak}}
    ingress:
      - fromEndpoints: [{matchLabels: {app.kubernetes.io/name: argo-workflows-server, io.kubernetes.pod.namespace: backup-system}}]
        toPorts: [{ports: [{port: "8080", protocol: TCP}]}]
  EOF

  kubectl exec -n backup-system rbac-test -- python3 /tmp/t.py

  # Cleanup -- these three are test-only scaffolding, never meant to persist:
  kubectl delete pod rbac-test -n backup-system
  kubectl delete cnp temp-rbac-test-keycloak-egress -n backup-system
  kubectl delete cnp temp-rbac-test-keycloak-ingress -n keycloak

The two Keycloak test users this creates (ensure_test_user, idempotent)
are left in place across runs on purpose, so reruns stay cheap; the pod
and both CiliumNetworkPolicies are not meant to persist and should be
deleted after each run.

Requires env vars: KEYCLOAK_ADMIN_USER, KEYCLOAK_ADMIN_PASSWORD
(the permanent realm admin, e.g. admin@homelab.internal + its secret).
Set SKIP_START_RECOVERY_POINT=1 to skip the one real recovery point it starts.
"""
import http.client
import json
import os
import re
import secrets
import ssl
import sys
import urllib.parse

KC_HOST = "keycloak.homelab.data-harness.org"  # OAuth flow only -- what a real browser hits
ARGO_HOST = "argo.homelab.data-harness.org"
REALM = "homelab"
ARGO_CLIENT_ID = "argo-workflows"

ADMIN_USER = os.environ["KEYCLOAK_ADMIN_USER"]
ADMIN_PASSWORD = os.environ["KEYCLOAK_ADMIN_PASSWORD"]

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TIERS = [
    # (group path, username/email, expected Role -- for the printed report only)
    ("/reader/viewer", "test-viewer@homelab.internal", "argo-viewer (read-only)"),
    ("/maintainer/app-operator", "test-operator@homelab.internal", "argo-operator (start recovery points only)"),
]


def conn(host):
    return http.client.HTTPSConnection(host, 443, timeout=15, context=CTX)


def kc_admin_conn():
    # Plain internal HTTP, not through the Gateway: keycloak-admin-deny
    # (SecurityPolicy, keycloak namespace) blocks the admin API externally
    # by design -- confirmed live (a non-JSON response came back for
    # /admin/... through the Gateway's hostname). Test-fixture setup isn't
    # part of the user-facing flow this script actually verifies, so it
    # uses the same internal path setup-job.yaml/config-cli-job.yaml
    # already use, same as every other admin-API script in this repo.
    return http.client.HTTPConnection("keycloak.keycloak.svc.cluster.local", 80, timeout=15)


def kc_api(method, path, token=None, data=None, form=False):
    c = kc_admin_conn()
    headers = {}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None and form:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, (json.loads(raw) if raw else None), dict(r.getheaders())


def get_admin_token():
    # master, not REALM ("homelab"): admin API rights over the homelab realm
    # come from the master realm's own admin-cli login (setup-job.yaml's
    # get_token() does the same) -- authenticating against homelab's own
    # admin-cli client instead returns a token with no such rights, which
    # is a 403 on every /admin/realms/homelab/... call, not an auth failure
    # at login time (confirmed live: login itself succeeds, first admin API
    # call then 403s with {"error":"HTTP 403 Forbidden"}).
    status, body, _ = kc_api(
        "POST", "/realms/master/protocol/openid-connect/token",
        data={"client_id": "admin-cli", "username": ADMIN_USER, "password": ADMIN_PASSWORD, "grant_type": "password"},
        form=True,
    )
    assert status == 200, f"admin login failed: {status} {body}"
    return body["access_token"]


def ensure_test_user(tok, email, group_path):
    """Idempotent: create the user if missing, always reset its password and
    group membership so reruns converge rather than accumulate drift."""
    status, existing, _ = kc_api("GET", f"/admin/realms/{REALM}/users?username={urllib.parse.quote(email)}&exact=true", token=tok)
    if not existing:
        status, _, headers = kc_api("POST", f"/admin/realms/{REALM}/users", token=tok, data={
            "username": email, "email": email, "enabled": True, "emailVerified": True,
        })
        assert status == 201, f"user creation failed: {status}"
        user_id = headers["Location"].rsplit("/", 1)[-1]
    else:
        user_id = existing[0]["id"]

    password = secrets.token_urlsafe(16)
    status, _, _ = kc_api("PUT", f"/admin/realms/{REALM}/users/{user_id}/reset-password", token=tok, data={
        "type": "password", "value": password, "temporary": False,
    })
    # A rerun within the same realm session may hit the same
    # passwordHistory(3) conflict setup-job.yaml's own fix works around --
    # harmless here (a random password every run has nothing to sync back
    # to), so treat it as "password already usable" rather than a failure.
    if status == 400:
        password = None  # caller falls back to a fresh reset only if login fails
    else:
        assert status == 204, f"password reset failed: {status}"

    status, groups, _ = kc_api("GET", f"/admin/realms/{REALM}/groups?search={group_path.rsplit('/', 1)[-1]}", token=tok)
    # search only matches top-level groups by name; walk to find the real subgroup id
    parent_name, child_name = group_path.strip("/").split("/")
    status, top, _ = kc_api("GET", f"/admin/realms/{REALM}/groups", token=tok)
    parent = next(g for g in top if g["name"] == parent_name)
    status, children, _ = kc_api("GET", f"/admin/realms/{REALM}/groups/{parent['id']}/children", token=tok)
    child = next(g for g in children if g["name"] == child_name)
    status, _, _ = kc_api("PUT", f"/admin/realms/{REALM}/users/{user_id}/groups/{child['id']}", token=tok)
    assert status == 204, f"group join failed: {status}"

    return user_id, password


def oauth_login(username, password):
    """Full authorization-code flow against the real argo-workflows client.
    Returns the 'authorization' session cookie Argo issues on success."""
    # 1. Hit Argo's login initiation endpoint, capture the state cookie and
    #    the Keycloak authorize URL it redirects to.
    c = conn(ARGO_HOST)
    c.request("GET", "/oauth2/redirect")
    r = c.getresponse()
    assert r.status == 302, f"expected redirect from /oauth2/redirect, got {r.status}"
    authorize_url = r.getheader("Location")
    state_cookie = r.getheader("Set-Cookie")
    c.close()
    state_name, state_value = state_cookie.split(";")[0].split("=", 1)

    # 2. Follow it to Keycloak's login page and scrape the login form.
    parsed = urllib.parse.urlparse(authorize_url)
    c = conn(KC_HOST)
    c.request("GET", parsed.path + "?" + parsed.query)
    r = c.getresponse()
    assert r.status == 200, f"expected Keycloak login page, got {r.status}"
    html = r.read().decode()
    kc_session_cookies = [h.split(";")[0] for k, h in r.getheaders() if k.lower() == "set-cookie"]
    c.close()
    m = re.search(r'action="([^"]+)"', html)
    assert m, "could not find login form action in Keycloak's login page"
    form_action = m.group(1).replace("&amp;", "&")

    # 3. Submit credentials to that exact form action.
    parsed_form = urllib.parse.urlparse(form_action)
    c = conn(KC_HOST)
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    c.request("POST", parsed_form.path + "?" + parsed_form.query, body=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": "; ".join(kc_session_cookies),
    })
    r = c.getresponse()
    if r.status != 302:
        c.close()
        return None, f"login form submission returned {r.status} (bad credentials or Keycloak layout changed)"
    callback_url = r.getheader("Location")
    c.close()

    # 4. Follow the redirect back to Argo's own callback -- this is the
    #    server-to-server step (Argo exchanges the code with Keycloak's
    #    token endpoint) that #693's hostAliases fix made possible at all.
    parsed_cb = urllib.parse.urlparse(callback_url)
    c = conn(ARGO_HOST)
    c.request("GET", parsed_cb.path + "?" + parsed_cb.query, headers={"Cookie": f"{state_name}={state_value}"})
    r = c.getresponse()
    body = r.read()
    auth_cookie = None
    for k, v in r.getheaders():
        if k.lower() == "set-cookie" and v.startswith("authorization="):
            auth_cookie = v.split(";")[0]
    c.close()
    if r.status not in (302, 200) or not auth_cookie:
        return None, f"callback returned {r.status}, no authorization cookie (body: {body[:300]!r})"
    return auth_cookie, None


def argo_api(method, path, auth_cookie, data=None):
    c = conn(ARGO_HOST)
    headers = {"Cookie": auth_cookie}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw[:300]


def report(label, status, expect_ok):
    ok = (status < 400) if expect_ok else (status in (401, 403))
    mark = "PASS" if ok else "FAIL"
    want = "allowed" if expect_ok else "denied"
    print(f"  [{mark}] {label}: HTTP {status} (expected {want})")
    return ok


POLICY = "argo-operator-workflow-scope"


def report_policy_denied(label, status, body):
    """Refused by Kyverno's operator-scope policy, not merely by RBAC: the
    request reached admission, and admission named the policy."""
    ok = status in (400, 403) and POLICY in json.dumps(body)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: HTTP {status} (expected refusal by {POLICY})")
    if not ok:
        print(f"         body: {str(body)[:300]}")
    return ok


def recovery_point(application="immich"):
    return {"workflowTemplateRef": {"name": "recovery-point"},
            "arguments": {"parameters": [{"name": "application", "value": application}]}}


def create_workflow(auth_cookie, spec):
    """A real create. Argo's own createOptions.dryRun is no use here: the API
    server answers a dry run itself, after its own validation, without ever
    sending the create to Kubernetes -- so neither RBAC nor admission runs and
    even a viewer gets a 200 (found live). Every case that expects a refusal
    is chosen to be harmless if it were wrongly admitted."""
    return argo_api("POST", "/api/v1/workflows/backup-system", auth_cookie, data={
        "workflow": {"metadata": {"generateName": "rbac-test-"}, "spec": spec},
    })


def submit_template(auth_cookie, template, parameters, entry_point=None):
    """What the UI's Submit button calls -- the server builds the Workflow
    itself from the template and the submit options, so this exercises a
    different Workflow shape than a hand-written create does."""
    options = {"parameters": parameters, "labels": "submit-from-ui=true"}
    if entry_point:
        options["entryPoint"] = entry_point
    return argo_api("POST", "/api/v1/workflows/backup-system/submit", auth_cookie, data={
        "resourceKind": "WorkflowTemplate", "resourceName": template, "submitOptions": options,
    })


def main():
    admin_tok = get_admin_token()
    print("Authenticated to Keycloak as", ADMIN_USER)

    all_ok = True
    for group_path, email, label in TIERS:
        print(f"\n=== {label} ({email}, group {group_path}) ===")
        user_id, password = ensure_test_user(admin_tok, email, group_path)
        if password is None:
            print("  password reset hit passwordHistory(3) on a rerun; retrying with a fresh one")
            _, password2 = ensure_test_user(admin_tok, email, group_path)
            password = password2
        print(f"  user ensured (id={user_id}), attempting login")

        auth_cookie, err = oauth_login(email, password)
        if err:
            print(f"  [FAIL] login: {err}")
            all_ok = False
            continue
        print("  login succeeded, session cookie acquired")

        is_viewer = group_path.endswith("viewer")
        is_operator = group_path.endswith("app-operator")

        status, _ = argo_api("GET", "/api/v1/workflows/backup-system", auth_cookie)
        all_ok &= report("list workflows", status, expect_ok=True)

        status, _ = argo_api("GET", "/api/v1/workflow-templates/backup-system", auth_cookie)
        all_ok &= report("list workflow templates", status, expect_ok=True)

        # A structurally valid spec, not just any body: Argo validates the
        # entrypoint reference (400 "template name 'x' undefined") BEFORE
        # the K8s RBAC check runs, so an invalid spec produces the same 400
        # regardless of who's asking and can't tell "RBAC blocked this" from
        # "the payload was bad" apart -- confirmed live against the viewer
        # tier: {"entrypoint": "none"} with no matching template 400'd even
        # though (per the fix above) RBAC evaluation never got a chance to
        # deny it; swapping in this self-consistent spec turned that same
        # call into a genuine K8s RBAC 403 ("workflowtemplates.argoproj.io
        # is forbidden: User ... cannot create resource").
        valid_spec = {
            "entrypoint": "main",
            "templates": [{"name": "main", "container": {"image": "docker.io/library/alpine:3.24", "command": ["true"]}}],
        }

        status, body = create_workflow(auth_cookie, valid_spec)
        if is_viewer:
            # Viewer lacks create on workflows -> 403 from RBAC.
            all_ok &= report("submit a raw workflow (RBAC should block)", status, expect_ok=False)
        else:
            # Operator has create, so RBAC lets it through -- and the operator
            # scope policy is what refuses anything that is not a recovery
            # point (an inline workflow here; Strict template referencing
            # alone only rejects it later, at run time, which is too late).
            all_ok &= report_policy_denied("submit a raw workflow (operator-scope policy should refuse)", status, body)

        if is_operator:
            # A dry-run retention: were the policy wrong, this plans and stops.
            status, body = create_workflow(auth_cookie, {
                "workflowTemplateRef": {"name": "retention"},
                "arguments": {"parameters": [{"name": "repository", "value": "local"},
                                             {"name": "dry-run", "value": "true"}]},
            })
            all_ok &= report_policy_denied("start retention (operator-scope policy should refuse)", status, body)

            status, body = create_workflow(auth_cookie, recovery_point('keycloak"; true; "'))
            all_ok &= report_policy_denied("inject shell via the application argument (should refuse)", status, body)

            # The UI's entrypoint dropdown offers every template in the
            # WorkflowTemplate, including ones never meant as entry points --
            # remove-clones and purge-restore-archives among them, which need
            # no inputs and so pass Argo's own validation. `plan` is the
            # harmless one to try: read-only if the policy were ever wrong.
            # (The dangerous ones are in test-argo-operator-scope.py, which
            # only dry-runs.)
            status, body = submit_template(auth_cookie, "recovery-point", ["application=keycloak"],
                                           entry_point="plan")
            all_ok &= report_policy_denied("submit with another entrypoint (should refuse)", status, body)

            status, body = submit_template(auth_cookie, "retention", ["repository=local", "dry-run=true"])
            all_ok &= report_policy_denied("submit retention (should refuse)", status, body)

            # The one thing an operator may do -- and it really runs: a normal
            # recovery point of the smallest application, which cleans up
            # after itself like every nightly one. Left to finish rather than
            # deleted: deleting a running Workflow skips its exit handler and
            # orphans its snapshot clones.
            if os.environ.get("SKIP_START_RECOVERY_POINT") == "1":
                print("  [skip] submit a recovery point for keycloak (SKIP_START_RECOVERY_POINT=1)")
            else:
                status, body = submit_template(auth_cookie, "recovery-point", ["application=keycloak"])
                all_ok &= report("submit a recovery point for keycloak, as the UI does (really runs)", status, expect_ok=True)
                if status < 400 and isinstance(body, dict):
                    print(f"         started {body.get('metadata', {}).get('name')}; it finishes and cleans up on its own")

        status, body = argo_api("POST", "/api/v1/workflow-templates/backup-system", auth_cookie, data={
            "template": {"metadata": {"generateName": "rbac-test-"}, "spec": valid_spec}
        })
        # Neither viewer nor operator may create a WorkflowTemplate -- that's
        # reserved for platform-admin (sso-rbac.yaml's own stated design).
        all_ok &= report("create a workflow template (RBAC should block for both non-admin tiers)", status, expect_ok=False)
        if status in (200, 201) and isinstance(body, dict) and "metadata" in body:
            # Shouldn't happen given the Role, but clean up if it ever does.
            name = body["metadata"]["name"]
            argo_api("DELETE", f"/api/v1/workflow-templates/backup-system/{name}", auth_cookie)

    print("\n" + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
