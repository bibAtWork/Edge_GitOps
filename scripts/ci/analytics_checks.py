"""Exercise upstream runtimes and the deployed ACL through Trino's SQL API."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / 'cluster/base/applications/analytics/deferred'


def run(*args):
    subprocess.run(args, check=True, cwd=ROOT)


def runtime():
    docs = list(yaml.safe_load_all((BASE / 'dagster.yaml').read_text()))
    image = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']['containers'][0]['image']
    command = ['docker', 'run', '--rm', '--user', '1000:1000', '--read-only',
               '--tmpfs', '/opt/runtime:uid=1000,gid=1000,mode=0750,size=2147483648',
               '--tmpfs', '/tmp:uid=1000,gid=1000,mode=1777,size=2147483648',
               '-v', f'{ROOT / "analytics"}:/opt/analytics:ro',
               '-v', f'{BASE / "config"}:/checks:ro',
               '-e', 'HOME=/tmp', '-e', 'PYTHONPATH=/opt/analytics',
               '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'DBT_TARGET_PATH=/tmp/dbt/target',
               '-e', 'DBT_LOG_PATH=/tmp/dbt/logs',
               '-e', 'PATH=/opt/runtime/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin']
    run(*command, '-e', 'REQUIREMENTS_FILE=/opt/analytics/requirements-control.lock', image,
        'sh', '-ec', 'sh /opt/analytics/install-runtime.sh; python -c "import dagster_webserver, dagster_k8s, dagster_postgres, definitions"')
    run(*command, image, 'sh', '-ec',
        'sh /opt/analytics/install-runtime.sh; python /opt/analytics/validate_runtime.py')


def assert_permission(result, denied):
    """A syntax/connection error is never evidence of denied access."""
    error = result.get('error')
    if denied:
        assert error and error.get('errorName') == 'PERMISSION_DENIED', result
    else:
        assert not error, result


def query(sql, user, port):
    headers = {'X-Trino-User': user, 'Content-Type': 'text/plain'}
    req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/statement', data=sql.encode(), headers=headers)
    rows = []
    while True:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
        rows.extend(result.get('data', []))
        if 'error' in result or not result.get('nextUri'):
            result['data'] = rows
            return result
        req = urllib.request.Request(result['nextUri'], headers=headers)


def permissions():
    # Memory storage isolates authorization from Keycloak, SeaweedFS and Lakekeeper.
    # HTTP identities and fixture rules exist ONLY in this disposable CI container.
    docs = list(yaml.safe_load_all((BASE / 'trino.yaml').read_text()))
    image = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']['containers'][0]['image']
    name, port = f'analytics-permission-check-{os.getpid()}', 18080
    with tempfile.TemporaryDirectory() as temp:
        config = Path(temp)
        config.chmod(0o755)
        (config / 'catalog').mkdir()
        (config / 'catalog/iceberg.properties').write_text('connector.name=memory\n')
        (config / 'config.properties').write_text('coordinator=true\nnode-scheduler.include-coordinator=true\nhttp-server.http.port=8080\ndiscovery.uri=http://localhost:8080\n')
        (config / 'node.properties').write_text('node.environment=ci\nnode.data-dir=/tmp/trino\n')
        (config / 'jvm.config').write_text('-server\n-Xmx1G\n-XX:+UseG1GC\n-XX:+ExitOnOutOfMemoryError\n')
        (config / 'access-control.properties').write_text('access-control.name=file\nsecurity.config-file=/etc/trino/rules.json\nsecurity.refresh-period=1s\n')
        fixture_rules = {'catalogs': [{'user': 'ci-fixture', 'catalog': 'iceberg', 'allow': 'all'}],
                         'schemas': [{'user': 'ci-fixture', 'owner': True}],
                         'tables': [{'user': 'ci-fixture', 'privileges': ['SELECT', 'INSERT', 'OWNERSHIP']}],
                         'queries': [{'user': 'ci-fixture', 'allow': ['execute']}]}
        (config / 'rules.json').write_text(json.dumps(fixture_rules))
        try:
            run('docker', 'run', '-d', '--name', name, '--memory', '2g', '-p', f'127.0.0.1:{port}:8080', '-v', f'{config}:/etc/trino:ro', image)
            deadline = time.monotonic() + 180
            while True:
                try:
                    assert_permission(query('SELECT 1', 'ci-fixture', port), False)
                    break
                except (urllib.error.URLError, AssertionError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(2)
            for sql in ['CREATE SCHEMA iceberg.raw', 'CREATE SCHEMA iceberg.marts', 'CREATE SCHEMA iceberg.staging',
                        'CREATE TABLE iceberg.raw.demo_events AS SELECT 1 AS id', 'CREATE TABLE iceberg.marts.summary AS SELECT 1 AS id']:
                assert_permission(query(sql, 'ci-fixture', port), False)
            (config / 'rules.json').write_text((BASE / 'config/trino-rules.json').read_text())
            deadline = time.monotonic() + 30
            while True:
                result = query('SELECT 1', 'ci-fixture', port)
                if result.get('error', {}).get('errorName') == 'PERMISSION_DENIED':
                    break
                if time.monotonic() > deadline:
                    raise AssertionError('Deployed ACL did not replace fixture permissions')
                time.sleep(1)
            checks = [
                ('analytics-bi', 'SELECT * FROM iceberg.marts.summary', False),
                ('analytics-bi', 'SELECT * FROM iceberg.raw.demo_events', True),
                ('analytics-bi', 'CREATE TABLE iceberg.marts.forbidden AS SELECT 1 AS id', True),
                ('analytics-dbt', 'SELECT * FROM iceberg.raw.demo_events', False),
                ('analytics-dbt', 'CREATE TABLE iceberg.staging.allowed AS SELECT 1 AS id', False),
                ('analytics-dbt', 'CREATE TABLE iceberg.raw.forbidden AS SELECT 1 AS id', True),
                ('unknown-user', 'SELECT * FROM iceberg.marts.summary', True),
            ]
            for user, sql, denied in checks:
                assert_permission(query(sql, user, port), denied)
                print(f'PASS {user}: {sql} ({"denied" if denied else "allowed"})')
        except BaseException:
            subprocess.run(['docker', 'logs', '--tail', '100', name], check=False)
            raise
        finally:
            subprocess.run(['docker', 'rm', '-f', name], check=False)


def manifests():
    with tempfile.TemporaryDirectory() as temp:
        paths = []
        for directory in [BASE.parent, BASE]:
            output = subprocess.check_output(['kubectl', 'kustomize', '--load-restrictor', 'LoadRestrictionsNone', str(directory)], text=True)
            docs = [d for d in yaml.safe_load_all(output) if d and 'sops' not in d]
            path = Path(temp) / (directory.name + '.yaml')
            path.write_text(yaml.safe_dump_all(docs))
            paths.append(str(path))
        run('kubeconform', '-strict', '-summary', '-ignore-missing-schemas', '-schema-location', 'default', '-schema-location',
            'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json',
            *paths, str(ROOT / 'analytics/bootstrap-job.yaml'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('check', choices=['runtime', 'permissions', 'manifests'])
    globals()[parser.parse_args().check]()
