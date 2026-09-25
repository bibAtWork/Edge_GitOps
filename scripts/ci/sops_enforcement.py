#!/usr/bin/env python3
"""Every Secret with data under cluster/ must be SOPS-encrypted.

A Secret opts out with the annotation gitops.homelab/sops-skip: "true" (for
the few that are deliberately plaintext placeholders). A file that does not
parse as YAML is skipped rather than failed: SOPS-encrypted files may not
parse cleanly.
"""
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402

SKIP_ANNOTATION = "gitops.homelab/sops-skip"


def unencrypted_secrets(root):
    """Paths (relative to root's parent) of Secrets that carry data but no sops block."""
    found = []
    for path in sorted(Path(root).rglob("*.yaml")):
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            for doc in yaml.safe_load_all(content):
                if not doc or doc.get("kind") != "Secret":
                    continue
                annotations = (doc.get("metadata") or {}).get("annotations") or {}
                if annotations.get(SKIP_ANNOTATION) == "true":
                    continue
                has_data = bool(doc.get("data") or doc.get("stringData"))
                if has_data and "sops" not in doc:
                    found.append(path.relative_to(Path(root).parent).as_posix())
        except yaml.YAMLError:
            pass
    return found


def main():
    errors = unencrypted_secrets(ROOT / "cluster")
    if errors:
        print("The following Secrets have data but are not SOPS-encrypted:")
        for error in errors:
            print(f"  {error}")
        print()
        print("Encrypt with: sops --encrypt --in-place <file>")
        return 1
    print("All Secrets are SOPS-encrypted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
