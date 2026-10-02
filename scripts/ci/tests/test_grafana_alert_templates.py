"""Grafana's Helm chart evaluates alerting values with Go's tpl function."""

import unittest
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[3]


class GrafanaAlertTemplateSafety(unittest.TestCase):
    def test_alert_rules_contain_no_unescaped_go_templates(self):
        release = yaml.safe_load(
            (ROOT / "cluster/base/infrastructure/04-grafana/helmrelease.yaml")
            .read_text(encoding="utf-8")
        )
        groups = release["spec"]["values"]["alerting"]["rules.yaml"]["groups"]

        # The chart calls tpl(toYaml(rules)) before Grafana sees the file.
        # A Grafana annotation like {{ $labels.instance }} is therefore parsed
        # by Helm first and fails the whole release upgrade. Existing Grafana
        # templates use the Helm-escaped form {{ "{{" }} $labels... instead.
        unescaped = re.compile(r"\{\{\s+\$(?:labels|values|value)\b")
        offenders = [
            f"{group['name']}/{rule['uid']}/{field}"
            for group in groups
            for rule in group["rules"]
            for field, value in rule.get("annotations", {}).items()
            if unescaped.search(value)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
