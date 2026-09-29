"""Keep scheduled drill evidence runnable under Argo templateRef expansion."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "check_recovery_drills", ROOT / "scripts/check-recovery-drills.py")
drills = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drills)


class EvidenceVolume(unittest.TestCase):
    def template(self, volumes):
        return {"kind": "WorkflowTemplate", "metadata": {"name": "drill-pitr"},
                "spec": {"volumes": volumes, "templates": [{"name": "main", "steps": [[
                    {"name": "evidence", "templateRef": {"name": "drill-evidence", "template": "report"}}]]}]}}

    def test_missing_caller_volume_is_rejected(self):
        self.assertEqual(drills.evidence_volume_errors([self.template([])]), [
            "drill-pitr references drill-evidence without the recovery-policy volume"])

    def test_wrong_configmap_is_rejected(self):
        self.assertEqual(len(drills.evidence_volume_errors([self.template([
            {"name": "policy", "configMap": {"name": "another-policy"}}])])), 1)

    def test_policy_volume_passes(self):
        self.assertEqual(drills.evidence_volume_errors([self.template([
            {"name": "policy", "configMap": {"name": "recovery-policy"}}])]), [])


if __name__ == "__main__":
    unittest.main()
