"""Regressions for the source inventory/native Renovate coverage boundary."""
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import renovate_coverage as coverage  # noqa: E402


class RenovateCoverage(unittest.TestCase):
    def setUp(self):
        self.file = "cluster/base/test.yaml"
        self.text = "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n    - image: nginx:1.2.3\n"
        self.files = {self.file: self.text}
        self.pins = coverage.inventory(self.files)
        self.dep = {"manager": "kubernetes", "datasource": "docker", "depName": "nginx",
                    "currentValue": "1.2.3"}

    def errors(self, deps=None, config=None, exceptions=None):
        extracted = {self.file: [self.dep]} if deps is None else deps
        return coverage.coverage_errors(self.pins, extracted, self.files, config or {}, exceptions or [])

    def exception(self, **extra):
        pin = self.pins[0]
        return {"file": pin.file, "field": pin.field, "value": pin.value,
                "reason": "Reviewed manual maintenance", "maintenance": "manual", **extra}

    def test_builtin_image_needs_no_comment(self):
        self.assertEqual(self.errors(), [])

    def test_manager_no_longer_matching_file_fails(self):
        self.assertTrue(self.errors({"cluster/old/path.yaml": [self.dep]}))

    def test_missing_dependency_and_wrong_version_fail(self):
        self.assertTrue(self.errors({}))
        self.dep["currentValue"] = "1.2.4"
        self.assertTrue(self.errors())

    def test_skipped_dependency_is_not_coverage(self):
        for reason in ("unknown-registry", "invalid-value", "unsupported-version"):
            with self.subTest(reason=reason):
                self.dep["skipReason"] = reason
                self.assertTrue(self.errors())

    def test_unconditional_disable_fails(self):
        config = {"packageRules": [{"matchManagers": ["kubernetes"],
                                   "matchPackageNames": ["nginx"], "enabled": False}]}
        self.assertTrue(self.errors(config=config))

    def test_major_only_disable_still_has_maintenance(self):
        config = {"packageRules": [{"matchPackageNames": ["nginx"],
                                   "matchUpdateTypes": ["major"], "enabled": False}]}
        self.assertEqual(self.errors(config=config), [])

    def test_file_disable_and_later_enable_follow_rule_order(self):
        rule = {"matchFileNames": ["cluster/base/*.yaml"], "enabled": False}
        self.assertTrue(self.errors(config={"packageRules": [rule]}))
        self.assertEqual(self.errors(config={"packageRules": [rule, {"enabled": True}]}), [])

    def test_unknown_disable_selector_fails_closed(self):
        with self.assertRaises(ValueError):
            self.errors(config={"packageRules": [{"matchJsonata": ["true"], "enabled": False}]})

    def test_docker_registry_port_and_digest(self):
        self.files = {self.file: "kind: Pod\nspec: {containers: [{image: 'localhost:5000/app:1.2@sha256:abc'}]}"}
        self.pins = coverage.inventory(self.files)
        self.dep.update(depName="localhost:5000/app", currentValue="1.2", currentDigest="sha256:abc")
        self.assertEqual(self.errors(), [])
        self.assertEqual(coverage.image_parts("localhost:5000/app"), ("localhost:5000/app", "", ""))

    def test_unpinned_image_fails(self):
        self.files = {self.file: "kind: Pod\nspec: {containers: [{image: nginx}]}"}
        self.pins = coverage.inventory(self.files)
        self.dep.pop("currentValue")
        self.assertTrue(self.errors())

    def test_init_containers_lists_and_multi_document_images_are_inventoried(self):
        text = "kind: Pod\nspec: {initContainers: [{image: 'busybox:1.2'}]}\n---\nkind: Job\nspec: {image: 'nginx:1.3'}"
        pins = coverage.inventory({self.file: text})
        self.assertEqual([p.value for p in pins], ["busybox:1.2", "nginx:1.3"])
        self.assertEqual([p.line for p in pins], [2, 5])

    def test_chart_version_and_split_image_values_keep_string_spelling(self):
        text = "kind: HelmRelease\nspec:\n  chart:\n    spec: {chart: app, version: 16.10}\n  values:\n    engine: {registry: quay.io, repository: example/engine, tag: 16.10}\n"
        pins = coverage.inventory({self.file: text})
        self.assertEqual([p.value for p in pins], ["16.10", "16.10"])
        deps = [{"manager": "flux", "datasource": "helm", "depName": "app", "currentValue": "16.10"},
                {"manager": "flux", "datasource": "docker", "depName": "quay.io/example/engine", "currentValue": "16.10"}]
        self.assertEqual(coverage.coverage_errors(pins, {self.file: deps}, {self.file: text}, {}, []), [])

    def test_unknown_chart_registry_fails(self):
        text = "kind: HelmRelease\nspec: {chart: {spec: {chart: app, version: 1.2.3}}}"
        pins = coverage.inventory({self.file: text})
        dep = {"manager": "flux", "datasource": "helm", "depName": "app", "currentValue": "1.2.3",
               "skipReason": "unknown-registry"}
        self.assertTrue(coverage.coverage_errors(pins, {self.file: [dep]}, {self.file: text}, {}, []))

    def test_oci_helm_chart_is_covered_by_flux_docker_datasource(self):
        pin = coverage.Pin(self.file, "spec.chart.spec.version", 1, "1.2.3", "app", "helm")
        dep = {"manager": "flux", "datasource": "docker", "depName": "ghcr.io/example/charts/app",
               "currentValue": "1.2.3"}
        self.assertTrue(coverage.covers(pin, dep, "", {}))

    def test_custom_pin_needs_native_replacement_span(self):
        text = "kind: Cluster\nspec:\n  imageName: ghcr.io/example/postgres:16.10\n"
        pins = coverage.inventory({self.file: text})
        dep = {"manager": "regex", "datasource": "docker", "depName": "ghcr.io/example/postgres",
               "currentValue": "16.10", "replaceString": "imageName: ghcr.io/example/postgres:16.10"}
        self.assertEqual(coverage.coverage_errors(pins, {self.file: [dep]}, {self.file: text}, {}, []), [])
        dep["replaceString"] = "other: 16.10"
        self.assertTrue(coverage.coverage_errors(pins, {self.file: [dep]}, {self.file: text}, {}, []))

    def test_custom_comment_alone_does_not_prove_coverage(self):
        text = "# renovate: datasource=docker depName=example/app\nkind: HelmRelease\nspec: {values: {image: {tag: v1.2.3}}}\n"
        pins = coverage.inventory({self.file: text})
        self.assertTrue(coverage.coverage_errors(pins, {}, {self.file: text}, {}, []))

    def test_same_version_in_another_custom_field_is_not_coverage(self):
        text = "first_version: 1.2.3\nsecond_version: 1.2.3\n"
        pins = coverage.inventory({self.file: text})
        dep = {"manager": "regex", "datasource": "github-releases", "depName": "example/first",
               "currentValue": "1.2.3", "replaceString": "first_version: 1.2.3"}
        errors = coverage.coverage_errors(pins, {self.file: [dep]}, {self.file: text}, {}, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("second_version", errors[0])

    def test_embedded_talos_manifest_image_and_cli_version(self):
        text = textwrap.dedent("""\
            kind: MachineConfig
            contents: |
              apiVersion: batch/v1
              kind: Job
              spec:
                image: quay.io/cilium/cilium-cli:v1.2.3
                args: [--version=v1.2.4]
            """)
        pins = coverage.inventory({self.file: text})
        self.assertEqual([p.value for p in pins], ["quay.io/cilium/cilium-cli:v1.2.3", "v1.2.4"])
        self.assertEqual([p.line for p in pins], [6, 7])

    def test_flux_generated_bundle_tracks_release_instead_of_individual_images(self):
        text = "# Flux Version: v2.9.6\n# Components: source-controller\nkind: Deployment\nspec: {image: 'ghcr.io/fluxcd/source-controller:v1.9.6'}"
        pins = coverage.inventory({self.file: text})
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].name, "fluxcd/flux2")

    def test_crd_schema_and_metadata_are_not_dependency_pins(self):
        text = "kind: CustomResourceDefinition\nspec: {version: v1, image: {type: string}, tag: {type: string}}\n---\nkind: ConfigMap\nmetadata: {tag: config}\n"
        self.assertEqual(coverage.inventory({self.file: text}), [])

    def test_workflow_actions_sha_tools_urls_and_pip_installs(self):
        file = ".github/workflows/test.yml"
        text = "jobs:\n  test:\n    steps:\n      - uses: example/action@abc123\n      - run: |\n          VERSION=v1.2.3\n          VERSION=v2.3.4\n          curl https://get.helm.sh/helm-v4.3.0-linux-amd64.tar.gz\n          pip install --quiet pyyaml==6.0.3\n"
        pins = coverage.inventory({file: text})
        self.assertEqual([p.value for p in pins], ["abc123", "v1.2.3", "v2.3.4", "v4.3.0", "6.0.3"])
        self.assertEqual(len({p.field for p in pins}), len(pins))

    def test_versions_env_and_ansible_tools(self):
        pins = coverage.inventory({"cluster/config/versions.env": "TALOS_VERSION=v1.2.3\nKUBERNETES_VERSION=v2.3.4\n",
                                   "bootstrap/ansible/all.yml": 'tool_version: "1.2.3"\n'})
        self.assertEqual([p.value for p in pins], ["1.2.3", "v1.2.3", "v2.3.4"])

    def test_native_extraction_log_is_required(self):
        for log in ("", '{"msg":"Repository finished","level":30}', "not json"):
            with self.subTest(log=log), self.assertRaises(ValueError):
                coverage.extracted_packages(log)

    def test_native_errors_or_empty_extraction_fail(self):
        for payload in ({"msg": "failure", "level": 50}, {"msg": "Extracted dependencies", "packageFiles": {}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                coverage.extracted_packages(json.dumps(payload))

    def test_native_log_maps_manager_and_file(self):
        payload = {"msg": "Extracted dependencies", "level": 30,
                   "packageFiles": {"kubernetes": [{"packageFile": self.file, "deps": [self.dep]}]}}
        self.assertEqual(coverage.extracted_packages(json.dumps(payload)), {self.file: [self.dep]})

    def test_exception_must_be_exact_current_and_reasoned(self):
        self.assertEqual(self.errors({}, exceptions=[self.exception()]), [])
        for extra in ({"value": "nginx:old"}, {"reason": ""}, {"field": "deleted.field"}, {"maintenance": ""}):
            with self.subTest(extra=extra):
                self.assertTrue(self.errors({}, exceptions=[self.exception(**extra)]))

    def test_derived_exception_needs_covered_source(self):
        exception = self.exception(source={"file": self.file, "field": "missing_version"})
        self.assertTrue(self.errors({}, exceptions=[exception]))

    def test_duplicate_exceptions_fail(self):
        self.assertTrue(self.errors({}, exceptions=[self.exception(), self.exception()]))

    def test_parse_failure_is_not_silently_skipped(self):
        with self.assertRaises(Exception):
            coverage.inventory({self.file: "kind: [broken"})

    def test_scope_includes_new_application_paths_and_yml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cluster/base/applications/new/deferred/workload.yml"
            path.parent.mkdir(parents=True)
            path.write_text(self.text, encoding="utf-8")
            self.assertEqual(len(coverage.inventory(coverage.source_files(root))), 1)


if __name__ == "__main__":
    unittest.main()
