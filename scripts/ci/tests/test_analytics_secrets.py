import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import yaml

spec = importlib.util.spec_from_file_location('apply_config_analytics', Path(__file__).resolve().parents[3] / 'bootstrap/scripts/apply-config.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class AnalyticsSecrets(unittest.TestCase):
    def test_disabled_does_not_generate_credentials(self):
        cfg = {}
        self.assertEqual(bootstrap.analytics_secret_manifests(cfg, 'synthetic', 'synthetic'), {})
        self.assertEqual(cfg, {})

    def test_password_change_updates_only_its_hash(self):
        original = bootstrap.trino_password_file({'dbt': 'first', 'bi': 'stable'})
        self.assertEqual(bootstrap.trino_password_file({'dbt': 'first', 'bi': 'stable'}, original), original)
        changed = bootstrap.trino_password_file({'dbt': 'rotated', 'bi': 'stable'}, original)
        self.assertNotEqual(changed.splitlines()[0], original.splitlines()[0])
        self.assertEqual(changed.splitlines()[1], original.splitlines()[1])

    @patch.object(bootstrap, 'existing_secret_value', return_value='REPLACE_WITH_ANALYTICS_CREDENTIAL')
    def test_templates_are_replaced_and_credentials_are_stable(self, existing):
        cfg = {'analytics': {'enabled': True}}
        first = bootstrap.analytics_secret_manifests(cfg, 'existing-admin', 'existing-secret')
        self.assertEqual(first, bootstrap.analytics_secret_manifests(cfg, 'existing-admin', 'existing-secret'))
        docs = {yaml.safe_load(v)['metadata']['name']: yaml.safe_load(v) for v in first.values()}
        self.assertEqual(docs['analytics-s3']['stringData']['admin_access_key_id'], 'existing-admin')
        self.assertEqual(docs['analytics-dagster-db']['type'], 'kubernetes.io/basic-auth')
        self.assertNotIn('REPLACE_WITH_', '\n'.join(first.values()))
        self.assertNotIn('analytics-run-env', docs)


if __name__ == '__main__':
    unittest.main()
