#!/usr/bin/env python3
"""
Apply bootstrap/config.json values to all cluster placeholder files.

Fills every REPLACE_WITH_* token, patches non-secret config (email, subnet),
then runs encrypt-secrets.sh so the result is ready to commit and push.

Called by bootstrap-1node.sh at multiple points as generated values become
available (the talosconfig after Phase 3). Can also be run standalone to refresh
a single secret without re-running the full bootstrap.

Usage:
  python3 bootstrap/scripts/apply-config.py
  python3 bootstrap/scripts/apply-config.py --talosconfig /path/to/talosconfig
  python3 bootstrap/scripts/apply-config.py --no-encrypt
"""

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = REPO_ROOT / "bootstrap" / "config.json"

# Every SOPS Secret owned by this script.  check-secret-bootstrap.py compares
# this set with the repository so a newly-added Secret cannot silently become a
# manual bootstrap step.
APPLY_CONFIG_SECRET_FILES = {
    "cluster/base/applications/canary/edge-client-secret.yaml",
    "cluster/base/infrastructure/01-seaweedfs/s3-secret.yaml",
    "cluster/base/infrastructure/04-grafana/admin-secret.yaml",
    "cluster/base/infrastructure/04-grafana/config/telegram-secret.yaml",
    "cluster/base/infrastructure/04-grafana/grafana-oauth-secret.yaml",
    "cluster/base/infrastructure/05-cilium/config/edge-client-secret.yaml",
    "cluster/base/infrastructure/06-cert-manager/operator/cloudflare-secret.yaml",
    "cluster/base/infrastructure/08-external-dns/cloudflare-secret.yaml",
    "cluster/base/infrastructure/12-zot/operator/htpasswd-secret.yaml",
    "cluster/base/infrastructure/12-zot/operator/oidc-credentials-secret.yaml",
    "cluster/base/infrastructure/12-zot/operator/s3-credentials-secret.yaml",
    "cluster/base/infrastructure/14-tailscale-operator/operator/oauth-secret.yaml",
    "cluster/base/infrastructure/15-system-upgrade-controller/operator/talos-credentials-secret.yaml",
    "cluster/base/infrastructure/16-immich/db-secret.yaml",
    "cluster/base/infrastructure/16-immich/oauth-config-secret.yaml",
    "cluster/base/infrastructure/16-immich/pg-owner-secret.yaml",
    "cluster/base/infrastructure/16-immich/recovery-object-store.yaml",
    "cluster/base/infrastructure/17-paperless-ngx/oidc-secret.yaml",
    "cluster/base/infrastructure/17-paperless-ngx/secret.yaml",
    "cluster/base/infrastructure/21-flux-notifications/github-token.yaml",
    "cluster/base/infrastructure/26-keycloak/argo-workflows-client-secret.yaml",
    "cluster/base/infrastructure/26-keycloak/edge-client-secret.yaml",
    "cluster/base/infrastructure/26-keycloak/google-idp-secret.yaml",
    "cluster/base/infrastructure/26-keycloak/immich-client-secret.yaml",
    "cluster/base/infrastructure/26-keycloak/keycloak-admin-user-secret.yaml",
    "cluster/base/infrastructure/26-keycloak/keycloak-secret.yaml",
    "cluster/base/infrastructure/26-keycloak/paperless-client-secret.yaml",
    "cluster/base/infrastructure/26-keycloak/recovery-object-store.yaml",
    "cluster/base/infrastructure/26-keycloak/zot-client-secret.yaml",
    "cluster/base/infrastructure/27-kubeopencode/config/edge-client-secret.yaml",
    "cluster/base/infrastructure/37-backup-system/argo-workflows-sso-secret.yaml",
    "cluster/base/infrastructure/37-backup-system/restic-secret.yaml",
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(
            f"ERROR: {CONFIG_FILE} not found.\n"
            f"Copy bootstrap/config.json.template to bootstrap/config.json and fill in values."
        )
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(CONFIG_FILE, 0o600)


def get(cfg: dict, *keys: str, required: bool = True) -> str:
    value = cfg
    path = ".".join(keys)
    for k in keys:
        if not isinstance(value, dict):
            value = ""
            break
        value = value.get(k, "")
    if not isinstance(value, str):
        value = ""
    value = value.strip()
    if required and not value:
        sys.exit(f"ERROR: config.json field '{path}' is not set.")
    return value


def random_credential(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def bcrypt_hash(password: str) -> str:
    """Hash without putting the password in a process argument."""
    try:
        import bcrypt
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    except ImportError:
        pass
    if shutil.which("htpasswd"):
        result = subprocess.run(
            ["htpasswd", "-B", "-n", "-i", "admin"],
            input=password, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip().split(":", 1)[1]
    raise RuntimeError(
        "Cannot generate bcrypt hash: install the bootstrap prerequisites "
        "(apache2-utils/httpd-tools/httpd) or the Python bcrypt package"
    )


def existing_secret_value(relative: str, key: str) -> str:
    """Read one existing SOPS value for backward-compatible config migration."""
    path = REPO_ROOT / relative
    if not path.exists() or "\nsops:\n" not in path.read_text(encoding="utf-8"):
        return ""
    result = subprocess.run(
        ["sops", "--decrypt", "--extract", f'["stringData"]["{key}"]', str(path)],
        capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else ""


def generated(
    cfg: dict, section: str, key: str, length: int = 43, existing: str = "",
) -> tuple[str, bool]:
    """Return a stable credential, migrating ciphertext before generating."""
    value = get(cfg, section, key, required=False)
    if value:
        return value, False
    value = existing or base64.urlsafe_b64encode(secrets.token_bytes(length)).rstrip(b"=").decode("ascii")
    cfg.setdefault(section, {})[key] = value
    action = "migrated" if existing else "auto-generated"
    print(f"  {action} {section}.{key}")
    return value, True


def secret_yaml(name: str, namespace: str, values: dict[str, str], secret_type: str = "Opaque") -> str:
    """Render a Secret without a YAML dependency; JSON strings are valid YAML scalars."""
    lines = [
        "apiVersion: v1", "kind: Secret", "metadata:", f"  name: {name}",
        f"  namespace: {namespace}", f"type: {secret_type}", "stringData:",
    ]
    for key, value in values.items():
        lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def write_secret(path: Path, content: str, changed: list[str]) -> None:
    """Replace a Secret only when its decrypted content differs."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    comparable = existing
    if "\nsops:\n" in existing:
        decrypted = subprocess.run(
            ["sops", "--decrypt", str(path)], capture_output=True, text=True,
            encoding="utf-8",
        )
        if decrypted.returncode == 0:
            comparable = decrypted.stdout
    if comparable.replace("\r\n", "\n").rstrip() != content.rstrip():
        path.write_text(content, encoding="utf-8")
        changed.append(str(path.relative_to(REPO_ROOT)))


def replace_in_file(path: Path, replacements: dict) -> bool:
    """Replace all keys with their values in file. Returns True if any change was made."""
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    new_content = content
    for old, new in replacements.items():
        new_content = new_content.replace(old, new)
    if new_content != content:
        path.write_text(new_content, encoding="utf-8")
        return True
    return False


def patch_subnet(path: Path, subnet: str) -> bool:
    """Replace the etcd advertisedSubnets value, whatever it currently is."""
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    new_content = re.sub(
        r"(advertisedSubnets:\s*\n\s*-\s*)[\d\.\/]+",
        lambda m: m.group(1) + subnet,
        content,
    )
    if new_content != content:
        path.write_text(new_content, encoding="utf-8")
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--talosconfig", metavar="PATH",
                        help="Path to generated talosconfig file (for system-upgrade-controller)")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="Skip SOPS encryption step (useful when called mid-bootstrap)")
    args = parser.parse_args()

    cfg = load_config()
    cluster = REPO_ROOT / "cluster"
    changed: list[str] = []

    # ── User-provided values ───────────────────────────────────────────────────

    email      = get(cfg, "cluster", "letsencrypt_email")
    domain     = get(cfg, "cluster", "domain")
    subdomain  = get(cfg, "cluster", "subdomain", required=False)
    gateway_ip = get(cfg, "cluster", "gateway_ip", required=False)
    # If a subdomain is set, all service hostnames live under <subdomain>.<domain>.
    # The wildcard cert covers *.<subdomain>.<domain>.
    effective_domain = f"{subdomain}.{domain}" if subdomain else domain
    subnet    = get(cfg, "node", "subnet")
    cf      = get(cfg, "cloudflare", "api_token")
    ts_id   = get(cfg, "tailscale", "oauth_client_id")
    ts_sec  = get(cfg, "tailscale", "oauth_client_secret")
    grafana = get(cfg, "grafana", "admin_password")

    # SeaweedFS + Zot: auto-generate if not set and save back to config.json
    sw_key = get(cfg, "seaweedfs", "admin_access_key_id", required=False)
    sw_sec = get(cfg, "seaweedfs", "admin_secret_access_key", required=False)
    zot_pw = get(cfg, "zot", "admin_password", required=False)
    zot_pw_was_configured = bool(zot_pw)
    modified_cfg = False
    if not sw_key:
        sw_key = existing_secret_value(
            "cluster/base/infrastructure/01-seaweedfs/s3-secret.yaml", "admin_access_key_id",
        ) or random_credential(20)
        cfg.setdefault("seaweedfs", {})["admin_access_key_id"] = sw_key
        modified_cfg = True
        print("  auto-generated seaweedfs.admin_access_key_id")
    if not sw_sec:
        sw_sec = existing_secret_value(
            "cluster/base/infrastructure/01-seaweedfs/s3-secret.yaml", "admin_secret_access_key",
        ) or random_credential(40)
        cfg.setdefault("seaweedfs", {})["admin_secret_access_key"] = sw_sec
        modified_cfg = True
        print("  auto-generated seaweedfs.admin_secret_access_key")
    if not zot_pw:
        zot_pw = random_credential(24)
        cfg.setdefault("zot", {})["admin_password"] = zot_pw
        modified_cfg = True
        print("  auto-generated zot.admin_password")
    zot_password_hash = get(cfg, "zot", "admin_password_hash", required=False)
    if not zot_password_hash:
        existing_htpasswd = existing_secret_value(
            "cluster/base/infrastructure/12-zot/operator/htpasswd-secret.yaml", "htpasswd",
        )
        zot_password_hash = (
            existing_htpasswd.split(":", 1)[1]
            if zot_pw_was_configured and existing_htpasswd.startswith("admin:") else bcrypt_hash(zot_pw)
        )
        cfg.setdefault("zot", {})["admin_password_hash"] = zot_password_hash
        modified_cfg = True
        print("  generated zot.admin_password_hash")

    # Shared secret for Grafana's Keycloak OIDC client — the same value goes
    # into Grafana's oauth Secret and Keycloak's realm-config Secret below.
    #
    # Falls back to the old dex.grafana_client_secret key, which held this
    # same value before Dex was replaced by Keycloak. Without the fallback, a
    # rebuild using an existing config.json (bootstrap-1node.sh runs this
    # script on every run, not just the first) would silently auto-generate a
    # NEW secret under the new key, rewrite grafana-oauth-secret.yaml to
    # match it, and break Grafana sign-in the moment it no longer matches the
    # value already sitting in Keycloak's Secret. Found on review of the
    # original Grafana generator (round 5).
    grafana_oidc_secret = get(cfg, "keycloak", "grafana_client_secret", required=False) \
        or get(cfg, "dex", "grafana_client_secret", required=False) \
        or existing_secret_value(
            "cluster/base/infrastructure/26-keycloak/keycloak-secret.yaml", "grafana-client-secret",
        )
    if not grafana_oidc_secret:
        grafana_oidc_secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        cfg.setdefault("keycloak", {})["grafana_client_secret"] = grafana_oidc_secret
        modified_cfg = True
        print("  auto-generated keycloak.grafana_client_secret")
    elif not get(cfg, "keycloak", "grafana_client_secret", required=False):
        # Carried over from the old dex key on this run; save it under the
        # new key so future runs read it from keycloak.* directly and the
        # old dex section can eventually be deleted from config.json by hand.
        cfg.setdefault("keycloak", {})["grafana_client_secret"] = grafana_oidc_secret
        modified_cfg = True
        print("  migrated dex.grafana_client_secret -> keycloak.grafana_client_secret")

    keycloak_values = {"grafana_client_secret": grafana_oidc_secret}
    for key, length, path, secret_key in [
        ("edge_client_secret", 43, "26-keycloak/edge-client-secret.yaml", "client-secret"),
        ("argo_workflows_client_secret", 43, "26-keycloak/argo-workflows-client-secret.yaml", "argo-workflows-client-secret"),
        ("immich_client_secret", 43, "26-keycloak/immich-client-secret.yaml", "immich-client-secret"),
        ("paperless_client_secret", 32, "26-keycloak/paperless-client-secret.yaml", "paperless-client-secret"),
        ("zot_client_secret", 43, "26-keycloak/zot-client-secret.yaml", "zot-client-secret"),
        ("admin_password", 32, "26-keycloak/keycloak-secret.yaml", "admin-password"),
        ("admin_user_password", 32, "26-keycloak/keycloak-admin-user-secret.yaml", "password"),
    ]:
        old_value = existing_secret_value(f"cluster/base/infrastructure/{path}", secret_key)
        keycloak_values[key], created = generated(cfg, "keycloak", key, length, old_value)
        modified_cfg |= created

    application_credentials = {}
    for section, key, length, path, secret_key in [
        ("immich", "database_password", 24, "16-immich/db-secret.yaml", "db-password"),
        ("immich", "database_admin_password", 24, "16-immich/db-secret.yaml", "db-admin-password"),
        ("paperless", "secret_key", 48, "17-paperless-ngx/secret.yaml", "PAPERLESS_SECRET_KEY"),
        ("paperless", "admin_password", 24, "17-paperless-ngx/secret.yaml", "PAPERLESS_ADMIN_PASSWORD"),
        ("backup", "restic_password", 48, "37-backup-system/restic-secret.yaml", "RESTIC_PASSWORD"),
    ]:
        old_value = existing_secret_value(f"cluster/base/infrastructure/{path}", secret_key)
        application_credentials[(section, key)], created = generated(
            cfg, section, key, length, old_value,
        )
        modified_cfg |= created

    # These values are issued by external services and cannot be generated
    # locally. Empty values leave their optional integrations unavailable while
    # still producing complete Secret objects for Flux.
    telegram_bot_token = get(cfg, "telegram", "bot_token", required=False) or existing_secret_value(
        "cluster/base/infrastructure/04-grafana/config/telegram-secret.yaml", "TELEGRAM_BOT_TOKEN")
    telegram_chat_id = get(cfg, "telegram", "chat_id", required=False) or existing_secret_value(
        "cluster/base/infrastructure/04-grafana/config/telegram-secret.yaml", "TELEGRAM_CHAT_ID")
    google_client_id = get(cfg, "google_oidc", "client_id", required=False) or existing_secret_value(
        "cluster/base/infrastructure/26-keycloak/google-idp-secret.yaml", "GOOGLE_CLIENT_ID")
    google_client_secret = get(cfg, "google_oidc", "client_secret", required=False) or existing_secret_value(
        "cluster/base/infrastructure/26-keycloak/google-idp-secret.yaml", "GOOGLE_CLIENT_SECRET")
    for section, values in {
        "telegram": {"bot_token": telegram_bot_token, "chat_id": telegram_chat_id},
        "google_oidc": {"client_id": google_client_id, "client_secret": google_client_secret},
    }.items():
        for key, value in values.items():
            if value and not get(cfg, section, key, required=False):
                cfg.setdefault(section, {})[key] = value
                modified_cfg = True
                print(f"  migrated {section}.{key}")

    if modified_cfg:
        save_config(cfg)

    print("=== Applying config.json to cluster files ===")

    # Domain substitution — wildcard cert + all HTTPRoutes
    domain_files = [
        cluster / "base/infrastructure/11-ingress-gateway/wildcard-cert.yaml",
        cluster / "base/infrastructure/12-zot/config/httproute.yaml",
        cluster / "base/infrastructure/04-grafana/config/httproute.yaml",
        cluster / "base/infrastructure/05-cilium/config/httproute.yaml",
    ]
    domain_changed = False
    for path in domain_files:
        if replace_in_file(path, {"REPLACE_WITH_DOMAIN": effective_domain}):
            changed.append(str(path.relative_to(REPO_ROOT)))
            domain_changed = True
    if domain_changed:
        print(f"  ✓ Domain ({effective_domain}) applied to wildcard cert + HTTPRoutes")

    # Cilium LB IPAM (one per profile — each overlay owns its own
    # CiliumLoadBalancerIPPool) + Tailscale subnet router (shared base
    # component, one file for both profiles) — all use the gateway LAN IP
    if gateway_ip:
        for path in [
            cluster / "overlays/1-node-config/lb-ipam.yaml",
            cluster / "overlays/3-node-config/lb-ipam.yaml",
            cluster / "base/infrastructure/14-tailscale-operator/config/subnet-router-hostnetwork.yaml",
        ]:
            if replace_in_file(path, {"REPLACE_WITH_GATEWAY_IP": gateway_ip}):
                changed.append(str(path.relative_to(REPO_ROOT)))
                print(f"  ✓ Gateway LAN IP ({gateway_ip}) applied to {path.name}")

    # cert-manager ClusterIssuer — not a secret, not SOPS-encrypted
    path = cluster / "base/infrastructure/06-cert-manager/config/clusterissuer.yaml"
    if replace_in_file(path, {"REPLACE_WITH_YOUR_EMAIL": email}):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ cert-manager ClusterIssuer email")

    # Talos machineconfigs — not secrets
    for mc in [
        cluster / "overlays/1-node/talos-machineconfigs/controlplane.yaml",
        cluster / "overlays/3-node/talos-machineconfigs/controlplane.yaml",
    ]:
        patched = patch_subnet(mc, subnet)
        if patched:
            changed.append(str(mc.relative_to(REPO_ROOT)))
            print(f"  ✓ machineconfig patched in {mc.parent.parent.name}")

    # Cloudflare token (cert-manager + external-dns)
    for path in [
        cluster / "base/infrastructure/06-cert-manager/operator/cloudflare-secret.yaml",
        cluster / "base/infrastructure/08-external-dns/cloudflare-secret.yaml",
    ]:
        if replace_in_file(path, {"REPLACE_WITH_CLOUDFLARE_API_TOKEN": cf}):
            changed.append(str(path.relative_to(REPO_ROOT)))
    print(f"  ✓ Cloudflare API token (cert-manager + external-dns)")

    # Tailscale OAuth
    path = cluster / "base/infrastructure/14-tailscale-operator/operator/oauth-secret.yaml"
    if replace_in_file(path, {
        "REPLACE_WITH_TS_OAUTH_CLIENT_ID":     ts_id,
        "REPLACE_WITH_TS_OAUTH_CLIENT_SECRET": ts_sec,
    }):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ Tailscale OAuth")

    # SeaweedFS S3 credentials (REPLACE_WITH_ADMIN_KEY appears 3× in the file)
    path = cluster / "base/infrastructure/01-seaweedfs/s3-secret.yaml"
    if replace_in_file(path, {
        "REPLACE_WITH_ADMIN_KEY":    sw_key,
        "REPLACE_WITH_ADMIN_SECRET": sw_sec,
    }):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ SeaweedFS S3 credentials")

    # Zot S3 credentials — same admin key/secret, in the zot namespace so the pod can mount them
    path = cluster / "base/infrastructure/12-zot/operator/s3-credentials-secret.yaml"
    if replace_in_file(path, {
        "REPLACE_WITH_ADMIN_KEY":    sw_key,
        "REPLACE_WITH_ADMIN_SECRET": sw_sec,
    }):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ Zot S3 credentials")

    # Grafana admin password
    path = cluster / "base/infrastructure/04-grafana/admin-secret.yaml"
    if replace_in_file(path, {"REPLACE_WITH_SECURE_PASSWORD": grafana}):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ Grafana admin password")

    # Flux GitHub status notifications token (repo:status scope only)
    flux_gh_token = get(cfg, "flux_notifications", "github_status_token", required=False) \
        or existing_secret_value(
            "cluster/base/infrastructure/21-flux-notifications/github-token.yaml", "token")
    if flux_gh_token and not get(cfg, "flux_notifications", "github_status_token", required=False):
        cfg.setdefault("flux_notifications", {})["github_status_token"] = flux_gh_token
        save_config(cfg)
        print("  migrated flux_notifications.github_status_token")
    if flux_gh_token:
        path = cluster / "base/infrastructure/21-flux-notifications/github-token.yaml"
        if replace_in_file(path, {"REPLACE_WITH_GITHUB_STATUS_TOKEN": flux_gh_token}):
            changed.append(str(path.relative_to(REPO_ROOT)))
            print(f"  ✓ Flux GitHub status token")

    # Zot htpasswd — generate bcrypt hash, write the unencrypted secret file.
    # encrypt-secrets.sh will SOPS-encrypt it afterward.
    # bcrypt is used instead of apr1 (MD5) for stronger hashing.
    # Password is passed in-process (not via argv) to avoid /proc exposure.
    path = cluster / "base/infrastructure/12-zot/operator/htpasswd-secret.yaml"
    htpasswd_line = f"admin:{zot_password_hash}"
    new_secret = (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: zot-htpasswd\n"
        "  namespace: zot\n"
        "stringData:\n"
        "  htpasswd: |\n"
        f"    {htpasswd_line}\n"
    )
    write_secret(path, new_secret, changed)
    print("  generated Zot htpasswd")

    # Grafana OIDC secret — write whole-file (like Zot htpasswd) so this works even
    # when the committed file is already SOPS-encrypted from a prior bootstrap run.
    path = cluster / "base/infrastructure/04-grafana/grafana-oauth-secret.yaml"
    new_grafana_oauth_secret = (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: grafana-oauth-secret\n"
        "  namespace: monitoring\n"
        "stringData:\n"
        f'  GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET: "{grafana_oidc_secret}"\n'
    )
    write_secret(path, new_grafana_oauth_secret, changed)
    print("  generated Grafana OAuth client secret")

    # ── Generated application Secrets ─────────────────────────────────────────

    edge_secret = keycloak_values["edge_client_secret"]
    argo_workflows_secret = keycloak_values["argo_workflows_client_secret"]
    immich_secret = keycloak_values["immich_client_secret"]
    paperless_oidc_secret = keycloak_values["paperless_client_secret"]
    zot_oidc_secret = keycloak_values["zot_client_secret"]
    immich_db_password = application_credentials[("immich", "database_password")]
    immich_db_admin_password = application_credentials[("immich", "database_admin_password")]
    restic_password = application_credentials[("backup", "restic_password")]
    seaweed_config = json.dumps({
        "identities": [{"name": "admin", "credentials": [{"accessKey": sw_key, "secretKey": sw_sec}],
                        "actions": ["Admin", "Read", "Write", "DeleteObject", "CreateBucket", "DeleteBucket", "ListBuckets"]}]
    }, separators=(",", ":"))
    immich_config = json.dumps({
        "oauth": {"enabled": True,
                  "issuerUrl": f"https://keycloak.{effective_domain}/realms/homelab/.well-known/openid-configuration",
                  "clientId": "immich", "clientSecret": immich_secret,
                  "scope": "openid email profile groups", "buttonText": "Login with Keycloak",
                  "autoRegister": True, "autoLaunch": False,
                  "mobileOverrideEnabled": False, "mobileRedirectUri": ""},
        "passwordLogin": {"enabled": True},
        "backup": {"database": {"enabled": False, "cronExpression": "0 02 * * *", "keepLastAmount": 1}},
    }, separators=(",", ":"))

    generated_secrets = {
        "base/infrastructure/01-seaweedfs/s3-secret.yaml": secret_yaml("seaweedfs-s3-secret", "seaweedfs", {
            "admin_access_key_id": sw_key, "admin_secret_access_key": sw_sec, "seaweedfs_s3_config": seaweed_config}),
        "base/infrastructure/04-grafana/admin-secret.yaml": secret_yaml("grafana-admin", "monitoring", {
            "admin-user": "admin", "admin-password": grafana}),
        "base/infrastructure/04-grafana/config/telegram-secret.yaml": secret_yaml("telegram-credentials", "monitoring", {
            "TELEGRAM_BOT_TOKEN": telegram_bot_token, "TELEGRAM_CHAT_ID": telegram_chat_id}),
        "base/infrastructure/06-cert-manager/operator/cloudflare-secret.yaml": secret_yaml("cloudflare-api-token", "cert-manager", {"api-token": cf}),
        "base/infrastructure/08-external-dns/cloudflare-secret.yaml": secret_yaml("cloudflare-api-token", "external-dns", {"api-token": cf}),
        "base/infrastructure/12-zot/operator/s3-credentials-secret.yaml": secret_yaml("zot-s3-credentials", "zot", {
            "access_key_id": sw_key, "secret_access_key": sw_sec}),
        "base/infrastructure/12-zot/operator/oidc-credentials-secret.yaml": secret_yaml("zot-oidc-credentials", "zot", {
            "oidc-credentials.json": json.dumps({"clientid": "zot", "clientsecret": zot_oidc_secret}, separators=(",", ":"))}),
        "base/infrastructure/14-tailscale-operator/operator/oauth-secret.yaml": secret_yaml("tailscale-oauth", "tailscale", {
            "client_id": ts_id, "client_secret": ts_sec}),
        "base/infrastructure/16-immich/db-secret.yaml": secret_yaml("immich-db-secret", "immich", {
            "db-password": immich_db_password, "db-admin-password": immich_db_admin_password}),
        "base/infrastructure/16-immich/pg-owner-secret.yaml": secret_yaml("immich-pg-owner", "immich", {
            "username": "immich", "password": immich_db_password}, "kubernetes.io/basic-auth"),
        "base/infrastructure/16-immich/oauth-config-secret.yaml": secret_yaml("immich-oauth-config", "immich", {"config.json": immich_config}),
        "base/infrastructure/16-immich/recovery-object-store.yaml": secret_yaml("recovery-object-store", "immich", {
            "admin_access_key_id": sw_key, "admin_secret_access_key": sw_sec}),
        "base/infrastructure/17-paperless-ngx/oidc-secret.yaml": secret_yaml("paperless-oidc-secret", "paperless", {
            "PAPERLESS_CLIENT_SECRET": paperless_oidc_secret}),
        "base/infrastructure/17-paperless-ngx/secret.yaml": secret_yaml("paperless-secret", "paperless", {
            "PAPERLESS_SECRET_KEY": application_credentials[("paperless", "secret_key")],
            "PAPERLESS_ADMIN_PASSWORD": application_credentials[("paperless", "admin_password")]}),
        "base/infrastructure/21-flux-notifications/github-token.yaml": secret_yaml("flux-github-token", "flux-system", {"token": flux_gh_token}),
        "base/infrastructure/26-keycloak/argo-workflows-client-secret.yaml": secret_yaml("keycloak-argo-workflows-oidc", "keycloak", {
            "argo-workflows-client-secret": argo_workflows_secret}),
        "base/infrastructure/26-keycloak/keycloak-admin-user-secret.yaml": secret_yaml("keycloak-admin-user", "keycloak", {
            "password": keycloak_values["admin_user_password"]}),
        "base/infrastructure/26-keycloak/keycloak-secret.yaml": secret_yaml("keycloak-secrets", "keycloak", {
            "admin-password": keycloak_values["admin_password"], "grafana-client-secret": grafana_oidc_secret}),
        "base/infrastructure/26-keycloak/google-idp-secret.yaml": secret_yaml("keycloak-google-idp", "keycloak", {
            "GOOGLE_CLIENT_ID": google_client_id, "GOOGLE_CLIENT_SECRET": google_client_secret}),
        "base/infrastructure/26-keycloak/immich-client-secret.yaml": secret_yaml("keycloak-immich-oidc", "keycloak", {
            "immich-client-secret": immich_secret}),
        "base/infrastructure/26-keycloak/paperless-client-secret.yaml": secret_yaml("keycloak-paperless-oidc", "keycloak", {
            "paperless-client-secret": paperless_oidc_secret}),
        "base/infrastructure/26-keycloak/zot-client-secret.yaml": secret_yaml("keycloak-zot-oidc", "keycloak", {
            "zot-client-secret": zot_oidc_secret}),
        "base/infrastructure/26-keycloak/recovery-object-store.yaml": secret_yaml("recovery-object-store", "keycloak", {
            "admin_access_key_id": sw_key, "admin_secret_access_key": sw_sec}),
        "base/infrastructure/37-backup-system/argo-workflows-sso-secret.yaml": secret_yaml("argo-workflows-sso", "backup-system", {
            "client-id": "argo-workflows", "client-secret": argo_workflows_secret}),
        "base/infrastructure/37-backup-system/restic-secret.yaml": secret_yaml("restic-local", "backup-system", {
            "RESTIC_REPOSITORY": "s3:http://seaweedfs-s3.seaweedfs.svc:8333/recovery/restic",
            "RESTIC_PASSWORD": restic_password, "AWS_ACCESS_KEY_ID": sw_key, "AWS_SECRET_ACCESS_KEY": sw_sec}),
    }
    for relative, namespace in {
        "base/applications/canary/edge-client-secret.yaml": "platform-canary",
        "base/infrastructure/05-cilium/config/edge-client-secret.yaml": "kube-system",
        "base/infrastructure/26-keycloak/edge-client-secret.yaml": "keycloak",
        "base/infrastructure/27-kubeopencode/config/edge-client-secret.yaml": "kubeopencode-system",
    }.items():
        generated_secrets[relative] = secret_yaml("keycloak-edge-oidc", namespace, {"client-secret": edge_secret})
    for relative, content in generated_secrets.items():
        write_secret(cluster / relative, content, changed)
    print(f"  generated {len(generated_secrets)} application Secrets from config.json")

    # system-upgrade-controller talosconfig
    if args.talosconfig:
        tc_path = Path(args.talosconfig)
        if not tc_path.exists():
            sys.exit(f"ERROR: talosconfig file not found: {tc_path}")
        tc_content = tc_path.read_text(encoding="utf-8")
        path = cluster / "base/infrastructure/15-system-upgrade-controller/operator/talos-credentials-secret.yaml"
        write_secret(path, secret_yaml(
            "talos-credentials", "cattle-system", {"talosconfig": tc_content}), changed)
        print("  generated system-upgrade-controller talosconfig")

    # ── Encrypt ───────────────────────────────────────────────────────────────

    if not changed:
        print("  (no changes — all placeholders already filled)")

    if not args.no_encrypt:
        print()
        print("=== Encrypting secrets with SOPS ===")
        encrypt = REPO_ROOT / "bootstrap/scripts/encrypt-secrets.sh"
        result = subprocess.run([str(encrypt)], cwd=REPO_ROOT)
        if result.returncode != 0:
            sys.exit("ERROR: encrypt-secrets.sh failed.")

    print()
    print("Files changed:")
    for f in changed:
        print(f"  {f}")
    print()
    print("Next: commit and push")
    print("  git add cluster/")
    print("  git commit -m 'chore: apply cluster config'")
    print("  git push")


if __name__ == "__main__":
    main()
