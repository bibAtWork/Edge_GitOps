"""Tests for the Talos / Kubernetes version checks. Run: python3 -m unittest discover -s scripts/ci/tests

Each case is a failure the check exists to catch, a false pass an earlier version
of it had, or a behaviour found by running it against the real service.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
import kubelet_node_ip  # noqa: E402
import kustomize_replacements  # noqa: E402
import talos_pins  # noqa: E402
import upgrade_window_collision as uwc  # noqa: E402
import version_pins  # noqa: E402


SID = "613e1592b2da41ae5e265e8789429f22e121aab91cb4deb6bc3c0b6262961245"


class Agreement(unittest.TestCase):
    def test_all_pins_agree(self):
        lines, error = talos_pins.agreement("v1.14.1", ["v1.14.1", "v1.14.1"])
        self.assertIsNone(error)
        self.assertEqual(lines[0], "Pins found: 3")
        self.assertEqual(lines[-1], "All pins agree on v1.14.1")

    def test_a_machine_config_behind_versions_env_is_an_error(self):
        # A rebuilt node would install something other than the upgrade target.
        _, error = talos_pins.agreement("v1.14.1", ["v1.14.1", "v1.14.0"])
        self.assertIn("2 distinct values", error)

    def test_versions_env_behind_a_machine_config_is_an_error(self):
        _, error = talos_pins.agreement("v1.14.0", ["v1.14.1"])
        self.assertIsNotNone(error)

    def test_no_machine_config_pin_at_all_proves_nothing(self):
        # A moved or typo'd path must not read as "everything agrees".
        _, error = talos_pins.agreement("v1.14.1", [])
        self.assertIn("cannot verify anything", error)

    def test_only_the_install_image_key_counts_as_a_pin(self):
        text = "# was v1.13.9 once\nmachine:\n  install:\n    image: factory.talos.dev/installer/" + SID + ":v1.14.1\n"
        self.assertEqual(talos_pins.INSTALL_IMAGE.findall(text), ["v1.14.1"])

    def test_a_version_in_prose_is_not_a_pin(self):
        self.assertEqual(talos_pins.INSTALL_IMAGE.findall("# bumped from v1.13.9 to v1.14.1\n"), [])


def fake_fetch(*answers):
    """A fetch that returns each (status, body) in turn and records its calls."""
    calls = []
    remaining = list(answers)

    def fetch(url, data=None, headers=None, method=None, timeout=30):
        calls.append((url, method))
        return remaining.pop(0) if remaining else remaining_default

    remaining_default = answers[-1]
    fetch.calls = calls
    return fetch


class FactorySchematic(unittest.TestCase):
    def test_201_created_is_success(self):
        # The Image Factory answers a schematic POST with 201, not 200; curl -f
        # accepted any 2xx and the first Python port did not (found against the
        # real service).
        fetch = fake_fetch((201, json.dumps({"id": SID}).encode()))
        self.assertEqual(talos_pins.factory_schematic_id(b"x", fetch), (SID, None))

    def test_200_is_success(self):
        self.assertEqual(talos_pins.factory_schematic_id(b"x", fake_fetch((200, json.dumps({"id": SID}).encode())))[0], SID)

    def test_a_client_error_is_not_retried(self):
        fetch = fake_fetch((400, b"bad schematic"))
        self.assertEqual(talos_pins.factory_schematic_id(b"x", fetch), (None, "HTTP 400"))
        self.assertEqual(len(fetch.calls), 1)

    def test_a_server_error_is_retried_and_then_succeeds(self):
        fetch = fake_fetch((503, b""), (201, json.dumps({"id": SID}).encode()))
        self.assertEqual(talos_pins.factory_schematic_id(b"x", fetch)[0], SID)
        self.assertEqual(len(fetch.calls), 2)

    def test_no_answer_at_all(self):
        self.assertEqual(talos_pins.factory_schematic_id(b"x", fake_fetch((None, b"timed out"))), (None, "no answer"))

    def test_a_body_without_an_id_is_an_error(self):
        _, why = talos_pins.factory_schematic_id(b"x", fake_fetch((201, b"{}")))
        self.assertIn("without an id", why)

    def test_a_stale_pinned_id_fails(self):
        stale = "a" * 64
        fetch = fake_fetch((201, json.dumps({"id": SID}).encode()))
        lines, failed = talos_pins.schematic_report("s.yaml", b"x", {SID, stale}, fetch)
        self.assertTrue(failed)
        self.assertTrue(any(f"Stale schematic ID {stale}" in line for line in lines))
        self.assertIn(f"  ok      {SID}", lines)

    def test_matching_ids_pass(self):
        lines, failed = talos_pins.schematic_report("s.yaml", b"x", {SID}, fake_fetch((201, json.dumps({"id": SID}).encode())))
        self.assertFalse(failed)

    def test_an_unanswered_factory_is_not_reported_as_stale(self):
        lines, failed = talos_pins.schematic_report("s.yaml", b"x", {"a" * 64}, fake_fetch((None, b"")))
        self.assertTrue(failed)
        self.assertFalse(any("Stale schematic" in line for line in lines))
        self.assertTrue(any("says nothing about whether" in line for line in lines))


class InstallerImages(unittest.TestCase):
    def test_pairs_come_from_the_repo_and_from_versions_env_sorted_and_unique(self):
        texts = [f"image: factory.talos.dev/installer/{SID}:v1.14.1\n", f"image: factory.talos.dev/installer/{SID}:v1.14.1\n"]
        pairs = talos_pins.installer_pairs(texts, {"TALOS_VERSION": "v1.14.2", "SCHEMATIC_ID": SID})
        self.assertEqual(pairs, [f"{SID}:v1.14.1", f"{SID}:v1.14.2"])

    def test_versions_env_without_both_values_adds_nothing(self):
        self.assertEqual(talos_pins.installer_pairs([], {"TALOS_VERSION": "v1.14.1"}), [])

    def probe_returning(self, verdict, status):
        return lambda url, **kwargs: (verdict, status)

    def test_a_built_installer_passes(self):
        lines, failed = talos_pins.image_report([f"{SID}:v1.14.1"], self.probe_returning("exists", 200))
        self.assertFalse(failed)
        self.assertIn("ok    v1.14.1", lines[0])

    def test_a_missing_installer_fails_with_the_cordon_warning(self):
        lines, failed = talos_pins.image_report([f"{SID}:v1.14.0"], self.probe_returning("unknown", 404))
        self.assertTrue(failed)
        self.assertIn("leave the node cordoned", lines[0])

    def test_a_rate_limited_answer_is_not_reported_as_a_missing_installer(self):
        lines, failed = talos_pins.image_report([f"{SID}:v1.14.1"], self.probe_returning("unknown", 429))
        self.assertTrue(failed)
        self.assertIn("says nothing about whether the installer exists", lines[0])

    def test_the_request_asks_for_the_oci_index(self):
        seen = {}

        def probe(url, definitely_missing=(), headers=None):
            seen.update(url=url, headers=headers)
            return "exists", 200

        talos_pins.image_report([f"{SID}:v1.14.1"], probe)
        self.assertTrue(seen["url"].endswith(f"/v2/installer/{SID}/manifests/v1.14.1"))
        self.assertEqual(seen["headers"], {"Accept": "application/vnd.oci.image.index.v1+json"})


class Probe(unittest.TestCase):
    def test_404_is_missing_and_not_retried(self):
        fetch = fake_fetch((404, b""))
        self.assertEqual(common.probe("u", fetch_fn=fetch, delay=0), ("missing", 404))
        self.assertEqual(len(fetch.calls), 1)

    def test_403_is_never_a_missing_tag(self):
        # An unauthenticated call from a shared runner was rate limited (403) and
        # reported "no tag v1.37.0" for a tag that exists.
        fetch = fake_fetch((403, b""))
        verdict, status = common.probe("u", fetch_fn=fetch, delay=0)
        self.assertEqual((verdict, status), ("unknown", 403))
        self.assertEqual(len(fetch.calls), 3)

    def test_a_transient_failure_then_success_is_success(self):
        self.assertEqual(common.probe("u", fetch_fn=fake_fetch((503, b""), (200, b"")), delay=0), ("exists", 200))

    def test_no_answer_is_unknown(self):
        self.assertEqual(common.probe("u", fetch_fn=fake_fetch((None, b"")), delay=0), ("unknown", None))

    def test_other_client_errors_are_not_retried(self):
        fetch = fake_fetch((401, b""))
        self.assertEqual(common.probe("u", fetch_fn=fetch, delay=0), ("unknown", 401))
        self.assertEqual(len(fetch.calls), 1)


class KubernetesRelease(unittest.TestCase):
    def report(self, version, verdict="exists", status=200):
        return version_pins.release_report(version, lambda url, **kw: (verdict, status))

    def test_a_real_release_passes(self):
        self.assertEqual(self.report("v1.37.0")[1], True)

    def test_a_prerelease_pin_is_refused_before_any_lookup(self):
        # An -rc pin resolves to a real tag and would pass the lookup.
        for version in ("v1.37.0-rc.0", "v1.37.0-alpha.1", "1.37.0", "v1.37", ""):
            lines, ok = version_pins.release_report(version, mock.Mock(side_effect=AssertionError("looked up")))
            self.assertFalse(ok, version)
            self.assertIn("not a plain vX.Y.Z", lines[0])

    def test_a_missing_tag_says_so(self):
        lines, ok = self.report("v1.99.0", "missing", 404)
        self.assertFalse(ok)
        self.assertIn("has no tag v1.99.0", lines[0])

    def test_an_unverifiable_tag_does_not_claim_it_is_missing(self):
        lines, ok = self.report("v1.37.0", "unknown", 403)
        self.assertFalse(ok)
        self.assertIn("not a missing tag", lines[0])
        self.assertNotIn("has no tag", lines[0])

    def test_the_exact_match_ref_endpoint_is_used_not_the_prefix_one(self):
        seen = []
        version_pins.release_report("v1.37.0", lambda url, **kw: seen.append(url) or ("exists", 200))
        self.assertTrue(seen[0].endswith("/git/ref/tags/v1.37.0"))
        self.assertNotIn("matching-refs", seen[0])


class MinorStep(unittest.TestCase):
    def test_same_minor_and_next_minor_pass(self):
        self.assertTrue(version_pins.minor_step("v1.36", "v1.36")[0])
        self.assertTrue(version_pins.minor_step("v1.36", "v1.37")[0])

    def test_skipping_a_minor_fails_and_names_the_intermediate_one(self):
        ok, message = version_pins.minor_step("v1.36", "v1.38")
        self.assertFalse(ok)
        self.assertIn("land v1.37 first", message)

    def test_minors_compare_as_numbers_not_strings(self):
        self.assertTrue(version_pins.minor_step("v1.9", "v1.10")[0])
        self.assertTrue(version_pins.minor_step("v1.99", "v1.100")[0])

    def test_a_major_change_needs_a_human(self):
        self.assertFalse(version_pins.minor_step("v1.36", "v2.0")[0])

    def test_a_decrease_is_not_this_guards_business(self):
        self.assertTrue(version_pins.minor_step("v1.37", "v1.36")[0])

    def test_no_prior_pin_is_a_no_op(self):
        self.assertEqual(version_pins.minor_step("", "v1.37"), (True, None))

    def test_only_the_key_line_is_read(self):
        text = "# was KUBERNETES_VERSION=v1.30.1 once\nKUBERNETES_VERSION=v1.37.0\n"
        self.assertEqual(version_pins.minor_of(text), "v1.37")

    def test_absent_key_and_absent_file_read_as_no_prior_pin(self):
        self.assertEqual(version_pins.minor_of("TALOS_VERSION=v1.14.1\n"), "")
        self.assertEqual(version_pins.minor_of(None), "")


class Downgrade(unittest.TestCase):
    OLD = {"TALOS_VERSION": "v1.14.1", "KUBERNETES_VERSION": "v1.37.0"}

    def test_a_forward_bump_is_not_a_downgrade(self):
        self.assertEqual(version_pins.downgraded_keys(self.OLD, {**self.OLD, "TALOS_VERSION": "v1.14.2"}), [])

    def test_a_revert_is_flagged(self):
        self.assertEqual(version_pins.downgraded_keys(self.OLD, {**self.OLD, "TALOS_VERSION": "v1.14.0"}), ["TALOS_VERSION"])

    def test_both_pins_can_be_flagged(self):
        new = {"TALOS_VERSION": "v1.13.9", "KUBERNETES_VERSION": "v1.36.4"}
        self.assertEqual(version_pins.downgraded_keys(self.OLD, new), ["TALOS_VERSION", "KUBERNETES_VERSION"])

    def test_versions_compare_numerically(self):
        self.assertTrue(version_pins.is_downgrade("v1.10.0", "v1.9.9"))
        self.assertFalse(version_pins.is_downgrade("v1.9.9", "v1.10.0"))

    def test_an_absent_side_is_not_a_decrease(self):
        self.assertFalse(version_pins.is_downgrade("", "v1.1.0"))
        self.assertFalse(version_pins.is_downgrade("v1.1.0", ""))
        self.assertFalse(version_pins.is_downgrade("v1.1.0", "v1.1.0"))

    def test_a_decrease_without_the_label_fails(self):
        lines, ok = version_pins.downgrade_verdict(self.OLD, {**self.OLD, "TALOS_VERSION": "v1.14.0"}, [])
        self.assertFalse(ok)
        self.assertIn("Add the 'confirmed-downgrade' label", lines[-1])

    def test_the_label_is_the_documented_escape_hatch(self):
        _, ok = version_pins.downgrade_verdict(self.OLD, {**self.OLD, "TALOS_VERSION": "v1.14.0"}, ["confirmed-downgrade"])
        self.assertTrue(ok)

    def test_a_label_that_merely_contains_the_words_does_not_count(self):
        # PR labels arrive as JSON; the old check was a substring grep on the text.
        _, ok = version_pins.downgrade_verdict(self.OLD, {**self.OLD, "TALOS_VERSION": "v1.14.0"}, ["not-a-confirmed-downgrade-really"])
        self.assertFalse(ok)

    def test_no_decrease_passes_without_a_label(self):
        self.assertEqual(version_pins.downgrade_verdict(self.OLD, self.OLD, []), (["No version decrease in this PR."], True))

    def test_env_parsing_reads_only_key_lines(self):
        self.assertEqual(version_pins.parse_env("# TALOS_VERSION=v0\nTALOS_VERSION=v1.14.1\n\nOTHER=x\n"),
                         {"TALOS_VERSION": "v1.14.1", "OTHER": "x"})


CRON = "35 5,11,17,23 * * *"


def window(name="talos-controlplane", days="su", start="12:00", end="14:00", tz="UTC"):
    return (name, days, start, end, tz)


class CronFires(unittest.TestCase):
    def test_lists_ranges_steps_and_names(self):
        self.assertEqual(uwc.expand_field("5,11,17,23", 0, 23), [5, 11, 17, 23])
        self.assertEqual(uwc.expand_field("1-3", 0, 59), [1, 2, 3])
        self.assertEqual(uwc.expand_field("*/20", 0, 59), [0, 20, 40])
        self.assertEqual(uwc.expand_field("10-30/10", 0, 59), [10, 20, 30])
        self.assertEqual(uwc.expand_field("Sun", 0, 7, names=uwc.DAY_TOKENS), [0])

    def test_sunday_is_both_0_and_7_and_sun_is_not_mangled_by_su(self):
        self.assertEqual(uwc.expand_field("sun", 0, 7, names=uwc.DAY_TOKENS), [0])
        self.assertEqual(set(uwc.cron_fires("0 1 * * 7")), {0})

    def test_fire_times_are_minutes_of_day(self):
        fires = uwc.cron_fires(CRON)
        self.assertEqual(set(fires), set(range(7)))
        self.assertEqual(fires[0], [5 * 60 + 35, 11 * 60 + 35, 17 * 60 + 35, 23 * 60 + 35])

    def test_unparseable_and_unsupported_schedules_refuse_rather_than_pass(self):
        for bad in ("35 5 * *", "35 5 1 * *", "x 5 * * *", "35 30 * * *"):
            with self.assertRaises(uwc.Refused, msg=bad):
                uwc.cron_fires(bad)


class Collisions(unittest.TestCase):
    def test_the_schedule_that_paged_critical_on_every_upgrade(self):
        # PR #386 round 4: 12:35 UTC landed inside the 12:00-14:00 control-plane window.
        errors, _ = uwc.collisions("35 12 * * *", "", [window()])
        self.assertEqual(len(errors), 1)
        self.assertIn("12:35 UTC on Sunday, inside talos-controlplane's window", errors[0])

    def test_a_clear_schedule_passes_and_counts_what_it_compared(self):
        errors, checked = uwc.collisions(CRON, "", [window(), window("talos-worker", "su", "14:00", "16:00"),
                                                    window("talos-kubernetes", "su", "16:00", "17:00")])
        self.assertEqual((errors, checked), ([], 3))

    def test_window_boundaries_are_inclusive(self):
        self.assertEqual(len(uwc.collisions("0 12 * * 0", "", [window()])[0]), 1)
        self.assertEqual(len(uwc.collisions("0 14 * * 0", "", [window()])[0]), 1)
        self.assertEqual(uwc.collisions("1 14 * * 0", "", [window()])[0], [])

    def test_a_window_on_a_day_the_cron_skips_is_still_compared(self):
        errors, checked = uwc.collisions("0 12 * * 1", "", [window()])
        self.assertEqual((errors, checked), ([], 1))

    def test_day_names_and_lists_are_understood(self):
        # A window moved to another day, or spelled out, was once silently never checked.
        self.assertEqual(len(uwc.collisions("30 12 * * 6", "", [window(days="Sunday,Sat")])[0]), 1)

    def test_no_windowed_plan_refuses(self):
        # A renamed Plan must not shrink what gets compared to nothing.
        with self.assertRaises(uwc.Refused):
            uwc.collisions(CRON, "", [])

    def test_no_schedule_refuses(self):
        with self.assertRaises(uwc.Refused):
            uwc.collisions("", "", [window()])

    def test_empty_days_refuses_instead_of_matching_nothing(self):
        # ''.split(',') == [''], which used to skip the Plan and still print "ok".
        # Asserted on the message so the refusal is for the right reason.
        with self.assertRaisesRegex(uwc.Refused, "missing days/startTime/endTime"):
            uwc.collisions(CRON, "", [window(days="")])

    def test_a_missing_start_or_end_names_the_missing_field(self):
        for bad in (window(start=""), window(end="")):
            with self.assertRaisesRegex(uwc.Refused, "missing days/startTime/endTime"):
                uwc.collisions(CRON, "", [bad])

    def test_unknown_day_wrap_past_midnight_and_bad_times_refuse(self):
        for bad in (window(days="funday"), window(start="22:00", end="02:00"), window(start="noon")):
            with self.assertRaises(uwc.Refused, msg=str(bad)):
                uwc.collisions(CRON, "", [bad])

    def test_a_non_utc_cronjob_refuses(self):
        with self.assertRaises(uwc.Refused):
            uwc.collisions(CRON, "Europe/Berlin", [window()])

    def test_an_explicit_utc_cronjob_is_fine(self):
        self.assertEqual(uwc.collisions(CRON, "UTC", [window()])[0], [])

    def test_a_window_time_zone_must_say_utc(self):
        for tz in ("", "Europe/Berlin"):
            with self.assertRaises(uwc.Refused, msg=tz):
                uwc.collisions(CRON, "", [window(tz=tz)])

    def extract_docs(self):
        return [
            {"kind": "CronJob", "metadata": {"name": "talos-fleet-health"}, "spec": {"schedule": CRON}},
            {"kind": "Plan", "metadata": {"name": "talos-controlplane"},
             "spec": {"window": {"days": ["su"], "startTime": "12:00", "endTime": "14:00", "timeZone": "UTC"}}},
            {"kind": "Plan", "metadata": {"name": "unwindowed"}, "spec": {}},
            {"kind": "CronJob", "metadata": {"name": "other"}, "spec": {"schedule": "0 0 * * *"}},
        ]

    def test_extraction_reads_only_the_fleet_health_cronjob_and_windowed_plans(self):
        cron, cron_tz, windows = uwc.extract(self.extract_docs())
        self.assertEqual((cron, cron_tz), (CRON, ""))
        self.assertEqual(windows, [("talos-controlplane", "su", "12:00", "14:00", "UTC")])

    def test_a_renamed_cronjob_extracts_as_no_schedule_and_then_refuses(self):
        docs = self.extract_docs()
        docs[0]["metadata"]["name"] = "renamed"
        cron, cron_tz, windows = uwc.extract(docs)
        with self.assertRaises(uwc.Refused):
            uwc.collisions(cron, cron_tz, windows)


CONFIG_WITH_SUBNET = """
machine:
  kubelet:
    nodeIP:
      validSubnets:
        - 192.168.178.0/24
"""


class KubeletNodeIp(unittest.TestCase):
    def test_a_pinned_subnet_is_found(self):
        self.assertEqual(kubelet_node_ip.pinned_subnet(CONFIG_WITH_SUBNET), "192.168.178.0/24")

    def test_a_subnet_only_in_a_comment_does_not_count(self):
        self.assertIsNone(kubelet_node_ip.pinned_subnet("# nodeIP validSubnets 192.168.178.0/24\nmachine:\n  kubelet: {}\n"))

    def test_a_subnet_in_the_wrong_place_does_not_count(self):
        text = "machine:\n  network:\n    validSubnets:\n      - 192.168.178.0/24\n"
        self.assertIsNone(kubelet_node_ip.pinned_subnet(text))

    def test_the_key_present_but_empty_does_not_count(self):
        self.assertIsNone(kubelet_node_ip.pinned_subnet("machine:\n  kubelet:\n    nodeIP:\n      validSubnets: []\n"))

    def test_a_non_cidr_entry_does_not_count(self):
        self.assertIsNone(kubelet_node_ip.pinned_subnet("machine:\n  kubelet:\n    nodeIP:\n      validSubnets:\n        - tailscale0\n"))

    def test_found_in_a_later_document_of_a_multi_document_file(self):
        self.assertEqual(kubelet_node_ip.pinned_subnet("cluster: {}\n---" + CONFIG_WITH_SUBNET), "192.168.178.0/24")

    def test_reindenting_the_file_does_not_blind_it(self):
        # The awk state machine this replaced walked the file by indentation.
        reindented = "machine:\n    kubelet:\n        nodeIP:\n            validSubnets:\n                - 10.0.0.0/8\n"
        self.assertEqual(kubelet_node_ip.pinned_subnet(reindented), "10.0.0.0/8")


def suc_docs(talos="v1.14.1", kubernetes="v1.37.0"):
    def plan(name, version, **images):
        spec = {"version": version}
        for key, image in images.items():
            spec[key] = {"image": image}
        return {"kind": "Plan", "metadata": {"name": name}, "spec": spec}

    talosctl = f"ghcr.io/siderolabs/talosctl:{talos}"
    env = [{"name": "DESIRED_TALOS_VERSION", "value": talos}, {"name": "DESIRED_KUBERNETES_VERSION", "value": kubernetes}]
    return [
        plan("talos-controlplane", talos, upgrade=talosctl),
        plan("talos-worker", talos, prepare=talosctl, upgrade=talosctl),
        plan("talos-on-demand", talos, upgrade=talosctl),
        plan("talos-kubernetes", kubernetes, upgrade=talosctl),
        {"kind": "CronJob", "metadata": {"name": "talos-fleet-health"},
         "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "check", "env": env}]}}}}}},
    ]


class KustomizeReplacements(unittest.TestCase):
    def errors(self, docs, talos="v1.14.1", kubernetes="v1.37.0"):
        return kustomize_replacements.unresolved(docs, talos, kubernetes)[1]

    def test_everything_resolved_passes_all_eleven_targets(self):
        ok, errors = kustomize_replacements.unresolved(suc_docs(), "v1.14.1", "v1.37.0")
        self.assertEqual((len(ok), errors), (11, []))

    def test_a_stale_literal_that_looks_plausible_fails(self):
        # The placeholder keeps whatever was hand-edited into it when a select matches nothing.
        docs = suc_docs()
        docs[0]["spec"]["version"] = "v1.13.9"
        errors = self.errors(docs)
        self.assertEqual(len(errors), 1)
        self.assertIn("talos-controlplane spec.version is 'v1.13.9'", errors[0])

    def test_a_stale_image_tag_fails(self):
        docs = suc_docs()
        docs[1]["spec"]["prepare"]["image"] = "ghcr.io/siderolabs/talosctl:v1.13.9"
        self.assertIn("talos-worker spec.prepare.image tag", self.errors(docs)[0])

    def test_a_renamed_plan_reads_as_a_target_that_matches_nothing(self):
        docs = suc_docs()
        docs[3]["metadata"]["name"] = "talos-k8s"
        errors = self.errors(docs)
        self.assertTrue(any("talos-kubernetes spec.version is 'null'" in e for e in errors))

    def test_the_last_target_that_once_had_no_check_is_checked(self):
        docs = suc_docs()
        docs[4]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "REPLACED-BY-KUSTOMIZE"
        errors = self.errors(docs)
        self.assertEqual(len(errors), 1)
        self.assertIn("DESIRED_KUBERNETES_VERSION", errors[0])

    def test_two_resources_answering_to_one_name_is_not_a_pass(self):
        docs = suc_docs() + [suc_docs("v1.0.0")[0]]
        self.assertTrue(self.errors(docs))

    def test_the_kubernetes_plan_tracks_kubernetes_but_its_tool_image_tracks_talos(self):
        self.assertEqual(self.errors(suc_docs("v1.14.1", "v1.38.0"), "v1.14.1", "v1.38.0"), [])

    def test_tag_takes_what_follows_the_last_colon(self):
        self.assertEqual(kustomize_replacements.tag("registry:5000/talosctl:v1.14.1"), "v1.14.1")
        self.assertEqual(kustomize_replacements.tag("no-colon"), "no-colon")
        self.assertIsNone(kustomize_replacements.tag(None))


if __name__ == "__main__":
    unittest.main()
