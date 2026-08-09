#!/usr/bin/env python3
"""
Apply bootstrap/config.json values to all cluster placeholder files.

Fills every REPLACE_WITH_* token, patches non-secret config (email, subnet),
then runs encrypt-secrets.sh so the result is ready to commit and push.

Called by bootstrap-1node.sh at multiple points as generated values become
available (age key after Phase 1, talosconfig after Phase 3, Velero IAM
credentials after Phase 5). Can also be run standalone to refresh a single
secret without re-running the full bootstrap.

Usage:
  python3 bootstrap/scripts/apply-config.py
  python3 bootstrap/scripts/apply-config.py --age-public-key age1xyz...
  python3 bootstrap/scripts/apply-config.py --talosconfig /path/to/talosconfig
  python3 bootstrap/scripts/apply-config.py \\
      --velero-access-key AKIAIOSFODNN7EXAMPLE \\
      --velero-secret-key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
  python3 bootstrap/scripts/apply-config.py --no-encrypt
"""

import argparse
import base64
import json
import os
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = REPO_ROOT / "bootstrap" / "config.json"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(
            f"ERROR: {CONFIG_FILE} not found.\n"
            f"Copy bootstrap/config.json.template to bootstrap/config.json and fill in values."
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
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


def replace_in_file(path: Path, replacements: dict) -> bool:
    """Replace all keys with their values in file. Returns True if any change was made."""
    if not path.exists():
        return False
    content = path.read_text()
    new_content = content
    for old, new in replacements.items():
        new_content = new_content.replace(old, new)
    if new_content != content:
        path.write_text(new_content)
        return True
    return False


def patch_subnet(path: Path, subnet: str) -> bool:
    """Replace the etcd advertisedSubnets value, whatever it currently is."""
    if not path.exists():
        return False
    content = path.read_text()
    new_content = re.sub(
        r"(advertisedSubnets:\s*\n\s*-\s*)[\d\.\/]+",
        lambda m: m.group(1) + subnet,
        content,
    )
    if new_content != content:
        path.write_text(new_content)
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--age-public-key", metavar="KEY",
                        help="talos-backup age public key (from .talos-backup-age.key)")
    parser.add_argument("--talosconfig", metavar="PATH",
                        help="Path to generated talosconfig file (for system-upgrade-controller)")
    parser.add_argument("--velero-access-key", metavar="KEY",
                        help="Velero IAM access key ID (from terraform output)")
    parser.add_argument("--velero-secret-key", metavar="SECRET",
                        help="Velero IAM secret access key (from terraform output)")
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
    modified_cfg = False
    if not sw_key:
        sw_key = random_credential(20)
        cfg.setdefault("seaweedfs", {})["admin_access_key_id"] = sw_key
        modified_cfg = True
        print("  auto-generated seaweedfs.admin_access_key_id")
    if not sw_sec:
        sw_sec = random_credential(40)
        cfg.setdefault("seaweedfs", {})["admin_secret_access_key"] = sw_sec
        modified_cfg = True
        print("  auto-generated seaweedfs.admin_secret_access_key")
    if not zot_pw:
        zot_pw = random_credential(24)
        cfg.setdefault("zot", {})["admin_password"] = zot_pw
        modified_cfg = True
        print("  auto-generated zot.admin_password")

    etcd_enc_key = get(cfg, "etcd", "encryption_key", required=False)
    if not etcd_enc_key:
        # 32 random bytes → base64 gives a 44-char string; used as the AES-CBC key
        etcd_enc_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        cfg.setdefault("etcd", {})["encryption_key"] = etcd_enc_key
        modified_cfg = True
        print("  auto-generated etcd.encryption_key (AES-256-CBC, stored in config.json)")

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

    # Cilium LB IPAM + Tailscale subnet router — both use the gateway LAN IP
    if gateway_ip:
        for path in [
            cluster / "overlays/1-node-config/lb-ipam.yaml",
            cluster / "base/infrastructure/14-tailscale-operator/config/subnet-router.yaml",
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
        patched |= replace_in_file(mc, {"REPLACE_WITH_ETCD_ENCRYPTION_KEY": etcd_enc_key})
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

    # Velero SeaweedFS credentials — for the local backup storage location
    path = cluster / "base/infrastructure/07-velero/seaweedfs-secret.yaml"
    if replace_in_file(path, {
        "REPLACE_WITH_ADMIN_KEY":    sw_key,
        "REPLACE_WITH_ADMIN_SECRET": sw_sec,
    }):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ Velero SeaweedFS credentials")

    # Grafana admin password
    path = cluster / "base/infrastructure/04-grafana/admin-secret.yaml"
    if replace_in_file(path, {"REPLACE_WITH_SECURE_PASSWORD": grafana}):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ Grafana admin password")

    # Flux GitHub status notifications token (repo:status scope only)
    flux_gh_token = get(cfg, "flux_notifications", "github_status_token", required=False)
    if flux_gh_token:
        path = cluster / "base/infrastructure/21-flux-notifications/github-token.yaml"
        if replace_in_file(path, {"REPLACE_WITH_GITHUB_STATUS_TOKEN": flux_gh_token}):
            changed.append(str(path.relative_to(REPO_ROOT)))
            print(f"  ✓ Flux GitHub status token")

    # Zot htpasswd — generate bcrypt hash, write the unencrypted secret file.
    # encrypt-secrets.sh will SOPS-encrypt it afterward.
    # bcrypt is used instead of apr1 (MD5) for stronger hashing.
    # Password is passed in-process (not via argv) to avoid /proc exposure.
    def _bcrypt_hash(password: str) -> str:
        try:
            import bcrypt as _bcrypt  # pip install bcrypt
            hashed = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12))
            return hashed.decode("utf-8")
        except ImportError:
            pass
        import shutil, subprocess
        if shutil.which("htpasswd"):
            # -B bcrypt, -n stdout, -i read password from stdin (avoids /proc exposure)
            result = subprocess.run(
                ["htpasswd", "-B", "-n", "-i", "admin"],
                input=password,
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip().split(":", 1)[1]
        raise RuntimeError(
            "Cannot generate bcrypt hash: install 'bcrypt' Python package "
            "(pip install bcrypt) or ensure 'htpasswd' is on PATH"
        )

    path = cluster / "base/infrastructure/12-zot/operator/htpasswd-secret.yaml"
    htpasswd_line = f"admin:{_bcrypt_hash(zot_pw)}"
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
    existing = path.read_text() if path.exists() else ""
    if new_secret != existing:
        path.write_text(new_secret)
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ Zot htpasswd")

    # talos-backup: SeaweedFS credentials (same key/secret as above)
    path = cluster / "base/00-bootstrap/talos-backup/secret.yaml"
    replacements = {
        "REPLACE_WITH_SEAWEEDFS_ACCESS_KEY": sw_key,
        "REPLACE_WITH_SEAWEEDFS_SECRET_KEY": sw_sec,
    }
    if args.age_public_key:
        replacements["REPLACE_WITH_AGE_PUBLIC_KEY"] = args.age_public_key
    if replace_in_file(path, replacements):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ talos-backup credentials")

    # ── Generated values (only when flags are passed) ─────────────────────────

    # system-upgrade-controller talosconfig
    if args.talosconfig:
        tc_path = Path(args.talosconfig)
        if not tc_path.exists():
            sys.exit(f"ERROR: talosconfig file not found: {tc_path}")
        tc_content = tc_path.read_text()
        # Indent the content to fit under the 'talosconfig: |' key (4 spaces)
        indented = "\n".join("    " + line for line in tc_content.splitlines())
        path = cluster / "base/infrastructure/15-system-upgrade-controller/operator/talos-credentials-secret.yaml"
        content = path.read_text()
        # Replace the placeholder line (including any indentation before REPLACE_WITH_)
        new_content = re.sub(
            r"[ \t]*REPLACE_WITH_BASE64_ENCODED_TALOSCONFIG\n?",
            indented + "\n",
            content,
        )
        if new_content != content:
            path.write_text(new_content)
            changed.append(str(path.relative_to(REPO_ROOT)))
            print(f"  ✓ system-upgrade-controller talosconfig")

    # Velero AWS IAM credentials (from Terraform output)
    if args.velero_access_key and args.velero_secret_key:
        path = cluster / "base/infrastructure/07-velero/aws-secret.yaml"
        if replace_in_file(path, {
            "REPLACE_WITH_ACCESS_KEY": args.velero_access_key,
            "REPLACE_WITH_SECRET_KEY": args.velero_secret_key,
        }):
            changed.append(str(path.relative_to(REPO_ROOT)))
            print(f"  ✓ Velero AWS IAM credentials")

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
