"""Tests for the gitops-lint checks. Run: python3 -m unittest discover -s scripts/ci/tests

Each case is a failure the check exists to catch, or the false pass that let an
earlier version of it do nothing -- the comments in the modules say which.
"""
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flux_references  # noqa: E402
import no_latest_tags  # noqa: E402
import overlay_component_parity  # noqa: E402
import recovery_policy_cronworkflows  # noqa: E402
import sops_enforcement  # noqa: E402
import unreplaced_sentinels  # noqa: E402


def write(root, relative, content):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


class SopsEnforcement(unittest.TestCase):
    def check(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in files.items():
                write(Path(tmp) / "cluster", name, content)
            return sops_enforcement.unencrypted_secrets(Path(tmp) / "cluster")

    def test_plaintext_secret_with_data_is_flagged(self):
        found = self.check({"a/s.yaml": "kind: Secret\nmetadata: {name: s}\nstringData: {k: v}\n"})
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith("cluster/a/s.yaml"))

    def test_data_key_is_flagged_too(self):
        self.assertEqual(len(self.check({"s.yaml": "kind: Secret\nmetadata: {name: s}\ndata: {k: dg==}\n"})), 1)

    def test_encrypted_secret_passes(self):
        self.assertEqual(self.check({"s.yaml": "kind: Secret\nmetadata: {name: s}\nstringData: {k: ENC}\nsops: {version: 3}\n"}), [])

    def test_secret_without_data_passes(self):
        self.assertEqual(self.check({"s.yaml": "kind: Secret\nmetadata: {name: s}\ntype: Opaque\n"}), [])

    def test_skip_annotation_passes(self):
        content = ("kind: Secret\nmetadata:\n  name: s\n  annotations:\n    gitops.homelab/sops-skip: 'true'\n"
                   "stringData: {k: v}\n")
        self.assertEqual(self.check({"s.yaml": content}), [])

    def test_skip_annotation_must_be_the_string_true(self):
        content = ("kind: Secret\nmetadata:\n  name: s\n  annotations:\n    gitops.homelab/sops-skip: 'false'\n"
                   "stringData: {k: v}\n")
        self.assertEqual(len(self.check({"s.yaml": content})), 1)

    def test_only_secrets_are_considered(self):
        self.assertEqual(self.check({"c.yaml": "kind: ConfigMap\nmetadata: {name: c}\ndata: {k: v}\n"}), [])

    def test_every_offending_document_in_a_multi_document_file_counts(self):
        content = ("kind: Secret\nmetadata: {name: a}\nstringData: {k: v}\n---\n"
                   "kind: Secret\nmetadata: {name: b}\nstringData: {k: v}\n")
        self.assertEqual(len(self.check({"s.yaml": content})), 2)


class NoLatestTags(unittest.TestCase):
    def test_list_item_latest_is_caught(self):
        # The `-?` is load-bearing: the first version required whitespace right
        # before `image:`, so the leading dash of a list item broke every match.
        self.assertEqual(no_latest_tags.latest_lines("      - image: foo/bar:latest\n"), ["      - image: foo/bar:latest"])

    def test_plain_key_latest_is_caught(self):
        self.assertEqual(len(no_latest_tags.latest_lines("        image: foo:latest\n")), 1)

    def test_a_pinned_tag_is_not_latest(self):
        self.assertEqual(no_latest_tags.latest_lines("      - image: foo:1.2.3\n"), [])

    def test_a_tag_merely_starting_with_latest_is_not_latest(self):
        self.assertEqual(no_latest_tags.latest_lines("      - image: foo:latest-2\n"), [])

    def test_bare_image_is_untagged(self):
        # local-path-provisioner's helper pod carried a bare `busybox`.
        self.assertEqual(no_latest_tags.untagged_images("    image: busybox\n"), ["busybox"])

    def test_untagged_image_in_a_list_item_and_quoted(self):
        text = '  - image: "docker.io/library/busybox"\n'
        self.assertEqual(no_latest_tags.untagged_images(text), ["docker.io/library/busybox"])

    def test_a_registry_port_is_not_a_tag(self):
        self.assertEqual(no_latest_tags.untagged_images("  image: registry.local:5000/team/app\n"),
                         ["registry.local:5000/team/app"])

    def test_tagged_image_is_fine(self):
        self.assertEqual(no_latest_tags.untagged_images("  image: registry.local:5000/team/app:1.0\n"), [])

    def test_digest_pin_is_fine(self):
        self.assertEqual(no_latest_tags.untagged_images("  image: foo/bar@sha256:" + "a" * 64 + "\n"), [])

    def test_kyverno_wildcard_match_key_is_not_a_pullable_image(self):
        self.assertEqual(no_latest_tags.untagged_images("      - image: docker.io/*\n"), [])

    def test_lines_that_are_not_image_keys_are_ignored(self):
        self.assertEqual(no_latest_tags.untagged_images("  name: busybox\n  imagePullPolicy: Always\n"), [])

    def test_machine_config_latest_is_caught_with_its_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "cluster/overlays/x/talos-machineconfigs/controlplane.yaml",
                         "a: 1\ninlineManifests:\n  - contents: |\n      image: cilium-cli-ci:latest\n")
            with unittest.mock.patch.object(no_latest_tags, "ROOT", Path(tmp)):
                hits = no_latest_tags.machine_config_hits([path])
        self.assertEqual(len(hits), 1)
        self.assertIn(":4:", hits[0])

    def test_machine_config_pinned_tag_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "cluster/overlays/x/talos-machineconfigs/c.yaml", "image: cilium-cli:v0.16\n")
            with unittest.mock.patch.object(no_latest_tags, "ROOT", Path(tmp)):
                self.assertEqual(no_latest_tags.machine_config_hits([path]), [])


class UnreplacedSentinels(unittest.TestCase):
    def test_surviving_sentinel_is_reported_with_its_line(self):
        text = "a: 1\nversion: REPLACED-BY-KUSTOMIZE\n"
        self.assertEqual(unreplaced_sentinels.sentinel_lines(text), ["2:version: REPLACED-BY-KUSTOMIZE"])

    def test_image_tag_sentinel_is_caught_too(self):
        self.assertEqual(len(unreplaced_sentinels.sentinel_lines("image: ghcr.io/x/talosctl:REPLACED-BY-KUSTOMIZE\n")), 1)

    def test_a_real_version_is_fine(self):
        self.assertEqual(unreplaced_sentinels.sentinel_lines("version: v1.14.1\n"), [])

    def test_only_config_overlays_are_rendered(self):
        # Checking a base overlay would render nothing containing a sentinel and
        # pass unconditionally, which is what the first draft of this job did.
        self.assertEqual(unreplaced_sentinels.OVERLAYS, ("1-node-config", "3-node-config"))


def helm_release(name, source=None, depends=()):
    spec = {"chart": {"spec": {"sourceRef": {"kind": "HelmRepository", "name": source}}}} if source else {"chart": {"spec": {}}}
    if depends:
        spec["dependsOn"] = [{"name": d} for d in depends]
    return {"kind": "HelmRelease", "metadata": {"name": name}, "spec": spec}


def helm_repo(name):
    return {"kind": "HelmRepository", "metadata": {"name": name}}


class FluxReferences(unittest.TestCase):
    def test_resolving_references_pass(self):
        docs = [helm_repo("r"), helm_release("a", "r"), helm_release("b", "r", depends=["a"])]
        self.assertEqual(flux_references.reference_errors("o", docs), [])

    def test_missing_chart_source_is_reported(self):
        errors = flux_references.reference_errors("o", [helm_release("a", "gone")])
        self.assertEqual(len(errors), 1)
        self.assertIn("HelmRepository/gone not found", errors[0])

    def test_missing_dependency_is_reported(self):
        errors = flux_references.reference_errors("o", [helm_repo("r"), helm_release("a", "r", depends=["ghost"])])
        self.assertEqual(len(errors), 1)
        self.assertIn("HelmRelease/ghost not found", errors[0])

    def test_non_helmrepository_sources_are_not_judged(self):
        doc = {"kind": "HelmRelease", "metadata": {"name": "a"},
               "spec": {"chart": {"spec": {"sourceRef": {"kind": "OCIRepository", "name": "x"}}}}}
        self.assertEqual(flux_references.reference_errors("o", [doc]), [])

    def test_references_are_per_overlay(self):
        # A chart source present in ANOTHER overlay does not help this one.
        self.assertEqual(len(flux_references.reference_errors("3", [helm_release("a", "only-in-1")])), 1)


def cron(app, namespace="backup-system"):
    return {"kind": "CronWorkflow", "metadata": {"name": f"recovery-point-{app}", "namespace": namespace}}


def policy(table):
    return {"kind": "ConfigMap", "metadata": {"name": "recovery-policy"}, "data": {"applications": table}}


class RecoveryPolicyCronWorkflows(unittest.TestCase):
    TABLE = "# application criticality profile\nimmich critical critical\nkeycloak critical critical  # comment\n"

    def test_matching_sets_pass(self):
        errors, checked = recovery_policy_cronworkflows.consistency_errors(
            "o", [policy(self.TABLE), cron("immich"), cron("keycloak")])
        self.assertEqual((errors, checked), ([], True))

    def test_application_without_a_cronworkflow_is_reported(self):
        # The pre-upgrade gate would never have proven it.
        errors, _ = recovery_policy_cronworkflows.consistency_errors("o", [policy(self.TABLE), cron("immich")])
        self.assertEqual(len(errors), 1)
        self.assertIn('"keycloak" but there is no recovery-point-keycloak', errors[0])

    def test_cronworkflow_without_a_policy_entry_is_reported(self):
        errors, _ = recovery_policy_cronworkflows.consistency_errors(
            "o", [policy(self.TABLE), cron("immich"), cron("keycloak"), cron("stray")])
        self.assertEqual(len(errors), 1)
        self.assertIn("recovery-point-stray CronWorkflow exists", errors[0])

    def test_cronworkflows_elsewhere_do_not_count(self):
        errors, _ = recovery_policy_cronworkflows.consistency_errors(
            "o", [policy("immich c c\n"), cron("immich", namespace="other")])
        self.assertEqual(len(errors), 1)

    def test_other_cronworkflow_names_are_ignored(self):
        docs = [policy("immich c c\n"), cron("immich"),
                {"kind": "CronWorkflow", "metadata": {"name": "reconcile", "namespace": "backup-system"}}]
        self.assertEqual(recovery_policy_cronworkflows.consistency_errors("o", docs), ([], True))

    def test_empty_table_cannot_compare_anything(self):
        errors, checked = recovery_policy_cronworkflows.consistency_errors("o", [policy("# only a comment\n")])
        self.assertTrue(checked)
        self.assertIn("table is empty", errors[0])

    def test_overlay_without_a_policy_is_skipped_not_passed(self):
        self.assertEqual(recovery_policy_cronworkflows.consistency_errors("o", [cron("immich")]), ([], False))


class OverlayComponentParity(unittest.TestCase):
    def build(self, layout):
        """layout: {profile+suffix: [component dirs it references]}"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for overlay, comps in layout.items():
            for comp in comps:
                write(root, f"cluster/base/infrastructure/{comp}/kustomization.yaml", "resources: []\n")
            resources = "".join(f"  - ../../base/infrastructure/{c}\n" for c in comps)
            write(root, f"cluster/overlays/{overlay}/kustomization.yaml", f"resources:\n{resources}" if comps else "resources: []\n")
        return root

    def test_same_components_pass(self):
        root = self.build({"1-node": ["01-a", "02-b"], "1-node-config": [], "3-node": ["01-a"], "3-node-config": ["02-b"]})
        one = overlay_component_parity.components("1-node", root)
        three = overlay_component_parity.components("3-node", root)
        self.assertIn("02-b", three)  # counted through the -config half
        self.assertEqual(overlay_component_parity.divergences(one, three), [])

    def test_component_missing_from_one_profile_is_reported(self):
        root = self.build({"1-node": ["01-a", "02-b"], "1-node-config": [], "3-node": ["01-a"], "3-node-config": []})
        errors = overlay_component_parity.divergences(
            overlay_component_parity.components("1-node", root), overlay_component_parity.components("3-node", root))
        self.assertEqual(errors, ["02-b is in 1-node/1-node-config but not 3-node/3-node-config"])

    def test_component_missing_from_the_other_direction_is_reported(self):
        errors = overlay_component_parity.divergences({"01-a"}, {"01-a", "02-b"})
        self.assertEqual(errors, ["02-b is in 3-node/3-node-config but not 1-node/1-node-config"])

    def test_intentional_difference_is_allowed(self):
        allowed = {"1-node": {"02-b"}, "3-node": set()}
        self.assertEqual(overlay_component_parity.divergences({"01-a", "02-b"}, {"01-a"}, allowed), [])

    def test_stale_allowlist_entry_is_reported(self):
        allowed = {"1-node": {"02-b"}, "3-node": set()}
        errors = overlay_component_parity.divergences({"01-a", "02-b"}, {"01-a", "02-b"}, allowed)
        self.assertEqual(len(errors), 1)
        self.assertIn("remove the allowlist entry", errors[0])

    def test_non_component_directories_are_not_counted(self):
        root = self.build({"1-node": ["01-a"], "1-node-config": [], "3-node": [], "3-node-config": []})
        write(root, "cluster/base/other/kustomization.yaml", "resources: []\n")
        self.assertEqual(overlay_component_parity.components("1-node", root) & {"other"}, set())


if __name__ == "__main__":
    unittest.main()
