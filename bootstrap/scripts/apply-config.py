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

    email     = get(cfg, "cluster", "letsencrypt_email")
    domain    = get(cfg, "cluster", "domain")
    subdomain = get(cfg, "cluster", "subdomain", required=False)
    # If a subdomain is set, all service hostnames live under <subdomain>.<domain>.
    # The wildcard cert covers *.<subdomain>.<domain>.
    effective_domain = f"{subdomain}.{domain}" if subdomain else domain
    subnet  = get(cfg, "node", "subnet")
    cf      = get(cfg, "cloudflare", "api_token")
    ts_id   = get(cfg, "tailscale", "oauth_client_id")
    ts_sec  = get(cfg, "tailscale", "oauth_client_secret")
    grafana = get(cfg, "grafana", "admin_password")

    # SeaweedFS: auto-generate if not set and save back to config.json
    sw_key = get(cfg, "seaweedfs", "admin_access_key_id", required=False)
    sw_sec = get(cfg, "seaweedfs", "admin_secret_access_key", required=False)
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
        if patch_subnet(mc, subnet):
            changed.append(str(mc.relative_to(REPO_ROOT)))
            print(f"  ✓ etcd advertisedSubnets in {mc.parent.parent.name}")

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

    # Grafana admin password
    path = cluster / "base/infrastructure/04-grafana/admin-secret.yaml"
    if replace_in_file(path, {"REPLACE_WITH_SECURE_PASSWORD": grafana}):
        changed.append(str(path.relative_to(REPO_ROOT)))
        print(f"  ✓ Grafana admin password")

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
