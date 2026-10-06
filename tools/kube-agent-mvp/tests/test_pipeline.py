import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app.api import make_handler
from app.store import Conflict, Store
from app import worker


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(str(Path(self.directory.name) / 'queue.db'))
        self.token = 'test-token-with-at-least-24-characters'

    def call(self, method, path, body=None, authorized=True):
        # Exercise the real HTTP handler without requiring network permissions.
        handler_type = make_handler(self.store, self.token, 'default')
        handler = object.__new__(handler_type)
        handler.command = method
        handler.path = path
        handler.request_version = 'HTTP/1.1'
        handler.requestline = method + ' ' + path + ' HTTP/1.1'
        handler.connection = unittest.mock.Mock()
        payload = json.dumps(body).encode() if body is not None else b''
        handler.headers = {'Content-Length': str(len(payload))}
        if authorized:
            handler.headers['Authorization'] = 'Bearer ' + self.token
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        handler.route()
        headers, response = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        return int(headers.split()[1]), json.loads(response)

    def test_auth_and_namespace_filter(self):
        self.assertEqual(self.call('POST', '/v1/scan', {}, False)[0], 401)
        self.assertEqual(self.call('GET', '/healthz', authorized=False)[0], 200)
        code, result = self.call('POST', '/v1/alertmanager', {'alerts': [
            {'status': 'firing', 'labels': {'namespace': 'other'}}]})
        self.assertEqual(code, 200)
        self.assertFalse(result['accepted'])
        self.assertEqual(self.store.listing(), [])

    def test_dedup_ignores_timestamps_and_alert_order(self):
        alerts = [{'status': 'firing', 'labels': {'namespace': 'default', 'alertname': name}, 'startsAt': 'old'}
                  for name in ('A', 'B')]
        first = self.call('POST', '/v1/alertmanager', {'alerts': alerts})[1]
        for alert in alerts:
            alert['startsAt'] = 'new'
        second = self.call('POST', '/v1/alertmanager', {'alerts': list(reversed(alerts))})[1]
        self.assertEqual(first['id'], second['id'])
        self.assertFalse(second['created'])

    def test_single_execution_slot_under_concurrent_claims(self):
        self.store.enqueue({'kind': 'scan', 'namespace': 'default'}, 'first')
        self.store.enqueue({'kind': 'scan', 'namespace': 'default'}, 'second')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.store.claim('investigation'), range(8)))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_expired_claim_cannot_overwrite_report(self):
        self.store.enqueue({'kind': 'scan', 'namespace': 'default'})
        claim = self.store.claim('investigation')
        with self.store.connect() as db:
            db.execute('UPDATE incidents SET lease_until=?', (time.time() - 1,))
        with self.assertRaises(Conflict):
            self.store.finish(claim['id'], claim['claim'], 'investigation', {'report': 'stale'})
        self.store.claim('investigation')
        self.assertEqual(self.store.get(claim['id'])['status'], 'failed')

    def test_queue_survives_reopening_database(self):
        incident, _ = self.store.enqueue({'kind': 'scan', 'namespace': 'default'})
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get(incident['id'])['status'], 'queued_investigation')

    def test_empty_result_rejected_and_agent_failure_persisted(self):
        self.call('POST', '/v1/scan', {})
        claim = self.store.claim('investigation')
        body = {'id': claim['id'], 'claim': claim['claim'], 'stage': 'investigation', 'result': {}}
        self.assertEqual(self.call('POST', '/internal/finish', body)[0], 400)
        body['error'] = 'Agent execution failed: RuntimeError'
        self.assertEqual(self.call('POST', '/internal/finish', body)[0], 200)
        self.assertEqual(self.store.get(claim['id'])['status'], 'failed')

    def test_full_pipeline_report_review_and_real_git_patch(self):
        source = Path(self.directory.name) / 'source'
        source.mkdir()
        manifest = source / 'deployment.yaml'
        manifest.write_text('replicas: 1\n')
        subprocess.run(['git', 'init', '-q', '-b', 'ops/talos_linux', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                        'commit', '-qm', 'Initial manifest'], check=True)
        real_run = worker.run

        def run_local_clone(argv, **kwargs):
            if argv[:2] == ['git', 'clone']:
                self.assertIn('ops/talos_linux', argv)
                argv = list(argv)
                argv[-2] = str(source)
            return real_run(argv, **kwargs)

        def fake_transport(path, body):
            code, result = self.call('POST', path, body)
            self.assertEqual(code, 200)
            return result

        env = {
            'WATCH_NAMESPACE': 'default',
            'HOLMES_COMMAND': json.dumps([sys.executable, '-c', 'print("Evidence: rollout unhealthy. Recommend replicas: 2.")']),
            'REPO_URL': 'https://example.com/manifests.git',
            'REPO_REF': 'ops/talos_linux',
            'OPENCODE_COMMAND': json.dumps([sys.executable, '-c',
                'from pathlib import Path; Path("deployment.yaml").write_text("replicas: 2\\n"); '
                'Path("notes.md").write_text("Review rollout\\n"); print("Proposed replica change; validate rollout.")']),
        }
        with patch.dict(os.environ, env), patch.object(worker, 'request', fake_transport), patch.object(worker, 'run', run_local_clone):
            identifier = self.call('POST', '/v1/scan', {})[1]['id']
            claim = self.store.claim('investigation')
            worker.process(claim, 'investigation')
            report = self.store.get(identifier)
            self.assertEqual(report['status'], 'awaiting_review')
            self.assertIn('Evidence:', report['report'])
            self.assertIsNone(self.store.claim('proposal'))
            self.assertEqual(self.call('POST', f'/v1/incidents/{identifier}/propose', {})[0], 202)
            worker.process(self.store.claim('proposal'), 'proposal')
            result = self.store.get(identifier)
            self.assertEqual(result['status'], 'complete')
            self.assertIn('+replicas: 2', result['patch'])
            self.assertIn('notes.md', result['patch'])
            self.assertEqual(manifest.read_text(), 'replicas: 1\n')
            # Persisted artifacts are retrievable, but claim tokens are never exposed here.
            response = self.call('GET', f'/v1/incidents/{identifier}')[1]
            self.assertNotIn('claim', response)

    def test_auto_proposal_and_invalid_review_transition(self):
        self.store.auto_propose = True
        incident, _ = self.store.enqueue({'kind': 'scan', 'namespace': 'default'})
        claim = self.store.claim('investigation')
        self.store.finish(incident['id'], claim['claim'], 'investigation', {'report': 'Evidence'})
        self.assertEqual(self.store.get(incident['id'])['status'], 'queued_proposal')
        with self.assertRaises(Conflict):
            self.store.propose(incident['id'])

    def test_command_preserves_untrusted_shell_characters(self):
        text = '$(touch /tmp/nope); `false` {"x": 1}'
        argv = worker.command('UNUSED_COMMAND', ['echo', '{prompt}'], prompt=text)
        self.assertEqual(argv, ['echo', text])

    def test_holmes_json_result_excludes_cli_logs(self):
        argv = [sys.executable, '-c',
                'import json,sys; from pathlib import Path; '
                'Path(sys.argv[1]).write_text(json.dumps({"result":"Evidence-based final report"})); '
                'print("Initialization and tool logs")', '{output}']
        with patch.dict(os.environ, {'HOLMES_COMMAND': json.dumps(argv), 'WATCH_NAMESPACE': 'default'}):
            result = worker.investigate({'payload': {'kind': 'scan', 'namespace': 'default'}})
        self.assertEqual(result['report'], 'Evidence-based final report')

    def test_timeout_terminates_process(self):
        with self.assertRaisesRegex(RuntimeError, 'runtime limit'):
            worker.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.05)


if __name__ == '__main__':
    unittest.main()
