#!/usr/bin/env python3
"""Focused tests for generate-crd-schemas.py."""

from __future__ import annotations

import importlib.util
import json
import shutil
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("generate-crd-schemas.py")
SPEC = importlib.util.spec_from_file_location("generate_crd_schemas", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GenerateCrdSchemasTest(unittest.TestCase):
    def setUp(self) -> None:
        # Keep this path short enough for local Windows checkouts.
        self.output = Path(".test-crd-schemas")
        if self.output.exists():
            shutil.rmtree(self.output)
        self.output.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.output)

    def test_generates_strict_schema_for_requested_group(self) -> None:
        documents = [
            {
                "kind": "CustomResourceDefinition",
                "spec": {
                    "group": "argoproj.io",
                    "names": {"kind": "WorkflowTemplate"},
                    "versions": [
                        {
                            "name": "v1alpha1",
                            "served": True,
                            "schema": {
                                "openAPIV3Schema": {
                                    "type": "object",
                                    "properties": {
                                        "spec": {
                                            "type": "object",
                                            "properties": {
                                                "count": {
                                                    "type": "integer",
                                                    "nullable": True,
                                                },
                                                "value": {
                                                    "x-kubernetes-int-or-string": True
                                                },
                                                "labels": {
                                                    "type": "object",
                                                    "additionalProperties": {
                                                        "type": "string"
                                                    },
                                                },
                                                "freeForm": {
                                                    "type": "object",
                                                    "properties": {},
                                                    "x-kubernetes-preserve-unknown-fields": True,
                                                },
                                            },
                                        }
                                    },
                                }
                            },
                        }
                    ],
                },
            },
            {
                "kind": "CustomResourceDefinition",
                "spec": {
                    "group": "example.com",
                    "names": {"kind": "Ignored"},
                    "versions": [],
                },
            },
        ]

        paths = MODULE.generate(documents, "argoproj.io", self.output)
        self.assertEqual(
            paths,
            [self.output / "argoproj.io/workflowtemplate_v1alpha1.json"],
        )
        schema = json.loads(paths[0].read_text(encoding="utf-8"))

        self.assertFalse(schema["additionalProperties"])
        spec = schema["properties"]["spec"]
        self.assertFalse(spec["additionalProperties"])
        self.assertEqual(spec["properties"]["count"]["type"], ["integer", "null"])
        self.assertEqual(
            spec["properties"]["value"]["anyOf"],
            [{"type": "integer"}, {"type": "string"}],
        )
        self.assertNotIn("additionalProperties", spec["properties"]["freeForm"])
        self.assertEqual(
            spec["properties"]["labels"]["additionalProperties"],
            {"type": "string"},
        )

    def test_rejects_missing_requested_group(self) -> None:
        with self.assertRaisesRegex(ValueError, "no served CRD schemas"):
            MODULE.generate([], "argoproj.io", self.output)


if __name__ == "__main__":
    unittest.main()
