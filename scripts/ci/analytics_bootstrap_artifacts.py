"""Temporary PR bootstrap tooling; outputs only encrypted inactive placeholders."""
import json
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[2]
out = Path('/tmp/analytics-bootstrap-artifacts')
out.mkdir(exist_ok=True)
templates = {
    'analytics-runtime': ('Opaque', ['lakekeeper-encryption-key', 'lightdash-secret', 'trino-internal-secret']),
    'analytics-s3': ('Opaque', ['admin_access_key_id', 'admin_secret_access_key']),
    'analytics-oauth': ('Opaque', ['client-id', 'client-secret']),
    'analytics-trino-client': ('Opaque', ['dbt-password', 'bi-password']),
    'analytics-trino': ('Opaque', ['password.db']),
    'analytics-dagster-db': ('kubernetes.io/basic-auth', ['username', 'password']),
    'analytics-lightdash-db': ('kubernetes.io/basic-auth', ['username', 'password']),
}
for name, (kind, keys) in templates.items():
    path = out / (name + '.yaml')
    manifest = dict(apiVersion='v1', kind='Secret', metadata=dict(name=name, namespace='analytics'),
                    type=kind, stringData={key: 'REPLACE_WITH_ANALYTICS_' + key.upper().replace('-', '_').replace('.', '_') for key in keys})
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    subprocess.run(['sops', '--config', str(ROOT / '.sops.yaml'), '--encrypt', '--in-place', str(path)], check=True)
for source, target in [('requirements.txt','requirements.lock'), ('requirements-control.txt','requirements-control.lock')]:
    subprocess.run(['uv', 'pip', 'compile', '--python-version', '3.12', '--generate-hashes',
                    '--no-header', str(ROOT / 'analytics' / source), '-o', str(out / target)], check=True)
