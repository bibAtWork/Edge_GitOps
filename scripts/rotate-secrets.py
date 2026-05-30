#!/usr/bin/env python3
"""
rotate-secrets.py — Secret rotation helper for edge-gitops.

Subcommands:
  sops-age      Rotate the SOPS age key (re-encrypt all Git secrets with new key)
  backup-age    Rotate the talos-backup age key (update Kubernetes secret)
  credential    Rotate a named credential inside a SOPS-encrypted secret file

Usage:
  ./scripts/rotate-secrets.py sops-age
  ./scripts/rotate-secrets.py sops-age --phase2
  ./scripts/rotate-secrets.py backup-age [--namespace talos-backup] [--secret talos-backup-age]
  ./scripts/rotate-secrets.py credential --file cluster/base/.../secret.yaml --key MY_KEY
"""

import argparse
import base64
import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SOPS_YAML = REPO_ROOT / ".sops.yaml"


class RotationError(Exception):
    pass


def run(cmd: List[str], cwd: Optional[Path] = None, input: Optional[str] = None) -> str:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        input=input,
    )
    if result.returncode != 0:
        raise RotationError(
            f"Command failed: {' '.join(str(c) for c in cmd)}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout.strip()


def confirm(prompt: str) -> None:
    answer = input(f"\n{prompt} [yes/N]: ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        sys.exit(0)


def check_prereqs(*tools: str) -> None:
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        raise RotationError(f"Missing required tools: {', '.join(missing)}")


def find_encrypted_files() -> List[Path]:
    try:
        output = run(["git", "grep", "-l", "ENC[AES256_GCM", "--", "*.yaml", "*.yml"])
    except RotationError:
        return []
    return [REPO_ROOT / p for p in output.splitlines() if p.strip()]


def parse_public_key_from_keypair(key_file: Path) -> str:
    for line in key_file.read_text().splitlines():
        if line.startswith("# public key:"):
            return line.split("# public key:")[1].strip()
    raise RotationError(f"Could not find public key in {key_file}")


def add_age_key_to_sops_yaml(new_public_key: str) -> None:
    content = SOPS_YAML.read_text()
    if new_public_key in content:
        print(f"  Public key already present in {SOPS_YAML.name}, skipping.")
        return

    # age: field is either a single key or comma-separated list on one line
    match = re.search(r"(age:\s*)(.+)", content)
    if not match:
        raise RotationError("Could not find 'age:' field in .sops.yaml")

    prefix = match.group(1)
    existing_keys = match.group(2).strip().rstrip(",")
    updated_line = f"{prefix}{existing_keys},{new_public_key}"
    content = content[: match.start()] + updated_line + content[match.end() :]
    SOPS_YAML.write_text(content)
    print(f"  Added new public key to {SOPS_YAML.name}")


def remove_age_key_from_sops_yaml(old_public_key: str) -> None:
    content = SOPS_YAML.read_text()
    if old_public_key not in content:
        print(f"  Key not found in {SOPS_YAML.name}, nothing to remove.")
        return

    match = re.search(r"(age:\s*)(.+)", content)
    if not match:
        raise RotationError("Could not find 'age:' field in .sops.yaml")

    prefix = match.group(1)
    keys = [k.strip() for k in match.group(2).split(",")]
    remaining = [k for k in keys if k != old_public_key]
    if not remaining:
        raise RotationError("Cannot remove the only age key from .sops.yaml")

    updated_line = f"{prefix}{','.join(remaining)}"
    content = content[: match.start()] + updated_line + content[match.end() :]
    SOPS_YAML.write_text(content)
    print(f"  Removed old public key from {SOPS_YAML.name}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_sops_age(phase2: bool) -> None:
    check_prereqs("age-keygen", "sops", "git")

    if phase2:
        _sops_age_phase2()
        return

    print("=== SOPS Age Key Rotation — Phase 1 ===")
    print()
    print("Steps:")
    print("  1. Generate a new age keypair")
    print("  2. Add the new public key to .sops.yaml alongside the old one")
    print("  3. Re-encrypt all secrets so both keys can decrypt them")
    print("  4. Print instructions to complete rotation (Phase 2)")
    print()
    print("Requirements:")
    print("  Set SOPS_AGE_KEY_FILE=/path/to/old-sops.key  (or SOPS_AGE_KEY=<private-key>)")
    print("  so SOPS can decrypt existing secrets during re-encryption.")

    if not os.environ.get("SOPS_AGE_KEY_FILE") and not os.environ.get("SOPS_AGE_KEY"):
        raise RotationError(
            "SOPS_AGE_KEY_FILE or SOPS_AGE_KEY must be set to the current private key."
        )

    confirm("Start Phase 1 SOPS age key rotation?")

    # Generate new keypair into a temp file
    fd, tmp = tempfile.mkstemp(suffix=".age.key")
    os.close(fd)
    new_key_file = Path(tmp)
    new_key_file.chmod(0o600)

    print(f"\nGenerating new keypair → {new_key_file}")
    run(["age-keygen", "-o", str(new_key_file)])
    new_public_key = parse_public_key_from_keypair(new_key_file)
    print(f"  New public key: {new_public_key}")

    # Capture old public key from .sops.yaml for Phase 2 instructions
    match = re.search(r"age:\s*(.+)", SOPS_YAML.read_text())
    old_keys_line = match.group(1).strip() if match else "(unknown)"

    # Update .sops.yaml
    add_age_key_to_sops_yaml(new_public_key)

    # Re-encrypt all secrets with both keys
    encrypted_files = find_encrypted_files()
    print(f"\nRe-encrypting {len(encrypted_files)} file(s) with both keys...")
    for f in encrypted_files:
        rel = f.relative_to(REPO_ROOT)
        print(f"  {rel}")
        run(["sops", "updatekeys", "--yes", str(f)])

    print()
    print("=== Phase 1 complete ===")
    print()
    print("NEXT STEPS:")
    print(f"  1. Copy {new_key_file} to OFFLINE secure storage (USB / password manager).")
    print(f"     Then permanently delete it: shred -u {new_key_file}")
    print()
    print("  2. Commit and push:")
    print("       git add .sops.yaml cluster/")
    print("       git commit -m 'chore: add new SOPS age key (rotation phase 1)'")
    print("       git push")
    print()
    print("  3. Wait for Flux to reconcile with the new key in the cluster.")
    print("     Verify: kubectl get kustomizations -A")
    print()
    print("  4. Set SOPS_AGE_KEY_FILE to the NEW private key, then run Phase 2:")
    print("       ./scripts/rotate-secrets.py sops-age --phase2")
    print(f"     Old keys in .sops.yaml (for reference): {old_keys_line}")
    print()
    print("  5. Keep BOTH private keys offline until Phase 2 is committed and pushed.")


def _sops_age_phase2() -> None:
    print("=== SOPS Age Key Rotation — Phase 2 ===")
    print()
    print("This re-encrypts all secrets using ONLY the new key,")
    print("removing the old key's encryption layer.")
    print()
    print("Requirements:")
    print("  1. The OLD age public key has been removed from .sops.yaml")
    print("  2. SOPS_AGE_KEY_FILE or SOPS_AGE_KEY is set to the NEW private key")

    if not os.environ.get("SOPS_AGE_KEY_FILE") and not os.environ.get("SOPS_AGE_KEY"):
        raise RotationError(
            "Set SOPS_AGE_KEY_FILE to the NEW private key before running Phase 2."
        )

    confirm("Run Phase 2 (remove old key encryption from all secrets)?")

    encrypted_files = find_encrypted_files()
    print(f"\nRe-encrypting {len(encrypted_files)} file(s) with new key only...")
    for f in encrypted_files:
        rel = f.relative_to(REPO_ROOT)
        print(f"  {rel}")
        run(["sops", "updatekeys", "--yes", str(f)])

    print()
    print("=== Rotation complete ===")
    print()
    print("NEXT STEPS:")
    print("  1. Commit and push:")
    print("       git add .sops.yaml cluster/")
    print("       git commit -m 'chore: finalize SOPS age key rotation (phase 2)'")
    print("       git push")
    print()
    print("  2. The OLD private key can now be safely destroyed.")


def cmd_backup_age(namespace: str, secret_name: str) -> None:
    check_prereqs("age-keygen", "kubectl")

    print("=== talos-backup Age Key Rotation ===")
    print()
    print("Steps:")
    print("  1. Generate a new age keypair for etcd snapshot encryption")
    print("  2. Update the Kubernetes secret in the cluster")
    print()
    print("WARNING: Existing etcd snapshots remain encrypted with the old key.")
    print("Keep both old and new private keys offline until all old snapshots are deleted.")
    confirm(f"Rotate talos-backup age key (secret: {namespace}/{secret_name})?")

    # Generate new keypair
    fd, tmp = tempfile.mkstemp(suffix=".age.key")
    os.close(fd)
    new_key_file = Path(tmp)
    new_key_file.chmod(0o600)

    print(f"\nGenerating new keypair → {new_key_file}")
    run(["age-keygen", "-o", str(new_key_file)])
    public_key = parse_public_key_from_keypair(new_key_file)
    print(f"  New public key: {public_key}")

    # Encode keypair for Kubernetes secret
    key_content = new_key_file.read_text()
    encoded = base64.b64encode(key_content.encode()).decode()

    # Patch the Kubernetes secret
    patch = f'{{"data": {{"age.key": "{encoded}"}}}}'
    print(f"\nPatching {namespace}/{secret_name}...")
    run(["kubectl", "patch", "secret", secret_name, "-n", namespace, "--patch", patch])

    print()
    print(f"Updated {namespace}/{secret_name} with new public key: {public_key}")
    print()
    print("NEXT STEPS:")
    print(f"  1. Copy {new_key_file} to OFFLINE secure storage.")
    print(f"     Then delete: shred -u {new_key_file}")
    print()
    print("  2. etcd snapshots encrypted with the old key remain valid until expiry.")
    print("     Default retention: 168h (7 days). Verify:")
    print("     aws s3 ls s3://<cluster>-etcd-backups-offsite/ --recursive")
    print()
    print("  3. Keep the OLD talos-backup private key offline until all old snapshots expire.")
    print("     After that, the old key can be destroyed.")


def cmd_credential(secret_file_str: str, key: str, value: Optional[str]) -> None:
    check_prereqs("sops")

    f = Path(secret_file_str)
    if not f.is_absolute():
        f = REPO_ROOT / f
    if not f.exists():
        raise RotationError(f"File not found: {f}")

    rel = f.relative_to(REPO_ROOT)
    print(f"=== Credential Rotation: {key} in {rel} ===")

    if value is None:
        value = getpass.getpass(f"New value for '{key}': ")
        if not value:
            raise RotationError("Value cannot be empty.")

    confirm(f"Update '{key}' in {rel}?")

    # sops --set uses a jq-style path expression
    run(["sops", "--set", f'["stringData"]["{key}"] "{value}"', str(f)])

    print(f"\nUpdated '{key}' in {rel}.")
    print()
    print("Commit the change:")
    print(f"  git add {rel}")
    print(f"  git commit -m 'chore: rotate {key} credential'")
    print("  git push")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate Age keys and credentials for edge-gitops.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p_sops = subs.add_parser("sops-age", help="Rotate the SOPS age key")
    p_sops.add_argument(
        "--phase2",
        action="store_true",
        help="Phase 2: strip old key encryption (run after removing old key from .sops.yaml)",
    )

    p_backup = subs.add_parser("backup-age", help="Rotate the talos-backup age key")
    p_backup.add_argument("--namespace", default="talos-backup")
    p_backup.add_argument("--secret", default="talos-backup-age")

    p_cred = subs.add_parser("credential", help="Rotate a named credential in a SOPS secret")
    p_cred.add_argument("--file", required=True, help="Path to SOPS-encrypted secret YAML")
    p_cred.add_argument(
        "--key", required=True, help="Key name under stringData to update"
    )
    p_cred.add_argument("--value", help="New value (prompted securely if omitted)")

    args = parser.parse_args()

    try:
        if args.command == "sops-age":
            cmd_sops_age(args.phase2)
        elif args.command == "backup-age":
            cmd_backup_age(args.namespace, args.secret)
        elif args.command == "credential":
            cmd_credential(args.file, args.key, getattr(args, "value", None))
    except RotationError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
