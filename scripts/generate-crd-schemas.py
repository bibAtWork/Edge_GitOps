#!/usr/bin/env python3
"""Build strict kubeconform JSON schemas from Kubernetes CRDs.

The input is a JSON array of Kubernetes documents.  Keeping YAML parsing out
of this script lets CI use its existing yq binary and keeps the generator on
Python's standard library only.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any


def strict_schema(value: Any) -> Any:
    """Convert a CRD's OpenAPI v3 fragment into a strict JSON schema."""
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    result = {key: strict_schema(item) for key, item in value.items()}

    # Kubernetes reports unknown fields for structural objects unless the CRD
    # explicitly makes the object free-form or defines a map value schema.
    if (
        result.get("type") == "object"
        and "properties" in result
        and "additionalProperties" not in result
        and not result.get("x-kubernetes-preserve-unknown-fields", False)
    ):
        result["additionalProperties"] = False

    # These OpenAPI extensions express unions that JSON Schema validators do
    # not otherwise understand.
    if result.get("x-kubernetes-int-or-string") is True:
        result.setdefault("anyOf", [{"type": "integer"}, {"type": "string"}])

    if result.pop("nullable", False):
        schema_type = result.get("type")
        if isinstance(schema_type, str):
            result["type"] = [schema_type, "null"]
        elif isinstance(schema_type, list) and "null" not in schema_type:
            result["type"] = [*schema_type, "null"]

    return result


def generate(documents: list[Any], group: str, output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for document in documents:
        if not isinstance(document, dict):
            continue
        if document.get("kind") != "CustomResourceDefinition":
            continue

        spec = document.get("spec", {})
        if spec.get("group") != group:
            continue

        kind = spec.get("names", {}).get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"CRD in {group} has no spec.names.kind")

        for version in spec.get("versions", []):
            if not version.get("served", True):
                continue
            version_name = version.get("name")
            schema = version.get("schema", {}).get("openAPIV3Schema")
            if not isinstance(version_name, str) or not isinstance(schema, dict):
                raise ValueError(f"{kind} has a served version without an OpenAPI schema")

            destination = output / group / f"{kind.lower()}_{version_name}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            converted = strict_schema(copy.deepcopy(schema))
            destination.write_text(
                json.dumps(converted, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            written.append(destination)

    if not written:
        raise ValueError(f"input contains no served CRD schemas for {group}")
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        documents = json.load(sys.stdin)
        if not isinstance(documents, list):
            raise ValueError("input must be a JSON array of Kubernetes documents")
        written = generate(documents, args.group, args.output)
    except (json.JSONDecodeError, OSError, ValueError) as error:
        print(f"generate-crd-schemas: {error}", file=sys.stderr)
        return 1

    for path in sorted(written):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
