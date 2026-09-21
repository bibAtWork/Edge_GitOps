#!/usr/bin/env python3
"""Fail when a SOPS Secret has no reproducible bootstrap owner."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPLY_CONFIG = ROOT / "bootstrap/scripts/apply-config.py"
RECOVERY_GENERATED = {
    "cluster/base/infrastructure/37-backup-system/recovery-aws-promoter.yaml",
    "cluster/base/infrastructure/37-backup-system/recovery-aws-retention.yaml",
}


def assigned_literal(path: Path, name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                value = ast.literal_eval(node.value)
                if not isinstance(value, set) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f"{name} must be a literal set of paths")
                return value
    raise ValueError(f"{name} is not declared in {path}")


def main() -> int:
    apply_source = APPLY_CONFIG.read_text(encoding="utf-8")
    encrypted = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "cluster").rglob("*.yaml")
        if any(line == "sops:" for line in path.read_text(encoding="utf-8").splitlines())
    }
    owned = assigned_literal(APPLY_CONFIG, "APPLY_CONFIG_SECRET_FILES") | RECOVERY_GENERATED
    errors = []
    for path in sorted(encrypted - owned):
        errors.append(f"{path}: encrypted Secret has no bootstrap generator")
    for path in sorted(owned - encrypted):
        errors.append(f"{path}: bootstrap inventory is stale or file is not SOPS encrypted")
    for path in sorted(assigned_literal(APPLY_CONFIG, "APPLY_CONFIG_SECRET_FILES")):
        # One occurrence declares inventory; another is the implementation's
        # path. This prevents satisfying coverage by listing an ungenerated
        # Secret in the set alone.
        relative = path.removeprefix("cluster/")
        if apply_source.count(relative) < 2:
            errors.append(f"{path}: inventoried but not referenced by generator implementation")
    if errors:
        print("Secret bootstrap coverage errors:")
        for error in errors:
            print(f"  ERROR: {error}")
        return 1
    print(f"All {len(encrypted)} SOPS manifests have a bootstrap generator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
