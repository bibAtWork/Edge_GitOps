#!/usr/bin/env python3
"""Encrypt private application credentials for Flux without writing plaintext YAML.

Usage: python bootstrap/scripts/provision-private-app-secrets.py \
    --ghcr-token-file PATH --gemini-key-file PATH --ghcr-username USER

The two input files must be outside this repository. A classic GitHub PAT with
read:packages and read access to all four packages is required. SOPS uses the
public age recipient in .sops.yaml; the age private key is not needed here.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "sre-agent-api",
    "sre-agent-holmes",
    "sre-agent-opencode",
    "real-estate-screener",
)
PROVISION_PRIVATE_APP_SECRET_FILES = {
    "cluster/base/applications/sre-agent/agent-auth-secret.yaml",
    "cluster/base/applications/sre-agent/agent-gemini-secret.yaml",
    "cluster/base/applications/private-ghcr/ghcr-pull-secret.yaml",
}


def read_credential(path: Path) -> str:
    resolved = path.resolve(strict=True)
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("credential input files must be outside the repository")
    value = resolved.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"{resolved}: expected exactly one nonempty credential line")
    return value


def read_gemini_key(path: Path) -> str:
    """Accept a bare key or the local api-key/project document."""
    resolved = path.resolve(strict=True)
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("credential input files must be outside the repository")
    content = resolved.read_text(encoding="utf-8-sig").strip()
    if "\n" not in content and "\r" not in content and not content.startswith("api-key:"):
        if not content:
            raise ValueError(f"{resolved}: Gemini API key is empty")
        return content

    fields = {}
    for line in content.splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not separator or name not in {"api-key", "project"} or not value or name in fields:
            raise ValueError(f"{resolved}: expected api-key and optional project fields")
        fields[name] = value
    if "api-key" not in fields:
        raise ValueError(f"{resolved}: missing api-key field")
    return fields["api-key"]


def verify_packages(username: str, token: str) -> None:
    basic = base64.b64encode(f"{username}:{token}".encode()).decode()
    for package in PACKAGES:
        scope = f"repository:bibatwork/{package}:pull"
        url = (
            "https://ghcr.io/token?scope="
            + urllib.parse.quote(scope, safe="")
            + "&service=ghcr.io"
        )
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {basic}"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                bearer = json.load(response)["token"]
            tags = urllib.request.Request(
                f"https://ghcr.io/v2/bibatwork/{package}/tags/list",
                headers={"Authorization": f"Bearer {bearer}"},
            )
            with urllib.request.urlopen(tags, timeout=15) as response:
                if response.status != 200:
                    raise ValueError(f"{package}: GHCR returned HTTP {response.status}")
        except urllib.error.HTTPError as error:
            raise ValueError(f"{package}: GHCR package read failed (HTTP {error.code})") from None


def secret_yaml(name: str, namespace: str | None, values: dict[str, str],
                kind: str = "Opaque") -> str:
    rows = [
        "apiVersion: v1", "kind: Secret", "metadata:", f"  name: {name}",
    ]
    if namespace:
        rows.append(f"  namespace: {namespace}")
    rows.extend((f"type: {kind}", "stringData:"))
    rows.extend(f"  {key}: {json.dumps(value)}" for key, value in values.items())
    return "\n".join(rows) + "\n"


def encrypt(path: Path, plaintext: str) -> str:
    result = subprocess.run(
        ["sops", "encrypt", "--input-type", "yaml", "--output-type", "yaml",
         "--filename-override", str(path.relative_to(ROOT))],
        input=plaintext, capture_output=True, text=True, cwd=ROOT, encoding="utf-8",
    )
    if result.returncode or "\nsops:\n" not in result.stdout:
        raise RuntimeError(f"SOPS encryption failed for {path.relative_to(ROOT)}")
    return result.stdout


def write_encrypted(path: Path, ciphertext: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == ciphertext:
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".sops-", suffix=".tmp", delete=False) as output:
        try:
            output.write(ciphertext)
            temp_path = Path(output.name)
        except BaseException:
            Path(output.name).unlink(missing_ok=True)
            raise
    os.replace(temp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ghcr-token-file", type=Path, required=True)
    parser.add_argument("--gemini-key-file", type=Path, required=True)
    parser.add_argument("--ghcr-username", required=True)
    args = parser.parse_args()

    try:
        token = read_credential(args.ghcr_token_file)
        gemini_key = read_gemini_key(args.gemini_key_file)
        verify_packages(args.ghcr_username, token)
        docker_auth = base64.b64encode(f"{args.ghcr_username}:{token}".encode()).decode()
        docker_config = json.dumps({"auths": {"ghcr.io": {"auth": docker_auth}}},
                                   separators=(",", ":"))

        sre = ROOT / "cluster/base/applications/sre-agent"
        shared = ROOT / "cluster/base/applications/private-ghcr"
        manifests = {
            sre / "agent-gemini-secret.yaml": secret_yaml(
                "agent-gemini", "sre-agent", {"api-key": gemini_key}),
            shared / "ghcr-pull-secret.yaml": secret_yaml(
                "ghcr-pull", None, {".dockerconfigjson": docker_config},
                "kubernetes.io/dockerconfigjson"),
        }
        auth_path = sre / "agent-auth-secret.yaml"
        if auth_path.exists() and "\nsops:\n" not in auth_path.read_text(encoding="utf-8"):
            raise ValueError(f"{auth_path.relative_to(ROOT)} is not SOPS-encrypted")
        if not auth_path.exists():
            manifests[auth_path] = secret_yaml(
                "agent-auth", "sre-agent", {"API_TOKEN": secrets.token_urlsafe(32)})

        # Encrypt every manifest before writing any file. Plaintext is held only
        # in process memory; reruns preserve the existing API token.
        encrypted = {path: encrypt(path, content) for path, content in manifests.items()}
        for path, ciphertext in encrypted.items():
            write_encrypted(path, ciphertext)
        print(f"Encrypted {len(encrypted)} private application Secrets with SOPS/age.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
