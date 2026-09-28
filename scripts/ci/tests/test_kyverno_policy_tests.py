from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kyverno_policy_tests import check_coverage, judge_exclusion, judge_mutation, judge_report  # noqa: E402


def report(result="pass", process="admission review"):
    return {"results": [{"policy": "fixture", "result": result, "properties": {"process": process}}]}


class KyvernoPolicyTests(unittest.TestCase):
    def test_expected_results(self):
        for result in ("pass", "fail", "skip"):
            self.assertIsNone(judge_report(report(result), "fixture", result))

    def test_skip_does_not_satisfy_denied_or_allowed_case(self):
        for expected in ("pass", "fail"):
            self.assertIsNotNone(judge_report(report("skip"), "fixture", expected))

    def test_compilation_error_is_not_expected_denial(self):
        self.assertIsNotNone(judge_report(report("error"), "fixture", "fail"))

    def test_empty_or_wrong_policy_result_cannot_pass(self):
        self.assertIsNotNone(judge_report({"results": []}, "fixture", "pass"))
        self.assertIsNotNone(judge_report(report(), "another", "pass"))

    def test_identity_policies_must_retain_background_disabled_setting(self):
        self.assertIsNotNone(judge_report(report(process="background scan"), "fixture", "pass"))

    def test_empty_output_requires_clean_exit_and_expected_exclusion(self):
        self.assertIsNone(judge_exclusion(0, "", "", "skip"))
        self.assertIsNotNone(judge_exclusion(0, "", "", "pass"))
        self.assertIsNotNone(judge_exclusion(0, "", "", "fail"))
        self.assertIsNotNone(judge_exclusion(1, "", "", "skip"))
        self.assertIsNotNone(judge_exclusion(0, "", "compile error", "skip"))

    def test_every_new_cel_policy_needs_cases(self):
        policies = [{"kind": "ValidatingPolicy", "metadata": {"name": "fixture"}},
                    {"kind": "MutatingPolicy", "metadata": {"name": "new"}}]
        self.assertTrue(check_coverage(policies, [{"policy": "fixture", "expected": "pass"}]))
        self.assertFalse(check_coverage(policies, [{"policy": "fixture", "expected": "pass"},
                                                 {"policy": "fixture", "expected": "fail"},
                                                 {"policy": "new", "expected": {}}]))

    def test_exclusion_only_suite_cannot_pass(self):
        policies = [{"kind": "ValidatingPolicy", "metadata": {"name": "fixture"}}]
        self.assertTrue(check_coverage(policies, [{"policy": "fixture", "expected": "skip"}]))

    def test_whole_mutated_resource_is_checked(self):
        expected = {"metadata": {"name": "fixture"}, "spec": {"revisionHistoryLimit": 3, "replicas": 1}}
        self.assertIsNone(judge_mutation(expected, deepcopy(expected)))
        changed = deepcopy(expected)
        changed["spec"]["replicas"] = 0
        self.assertIsNotNone(judge_mutation(changed, expected))
