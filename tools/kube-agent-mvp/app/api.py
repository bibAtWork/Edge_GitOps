import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .store import Conflict, Store

MAX_BODY = 2 * 1024 * 1024


def make_handler(store, token, namespace):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Do not log payloads, credentials, or report contents.
            logging.info('%s %s', self.command, self.path)

        def reply(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.route()

        def do_POST(self):
            self.route()

        def route(self):
            self.connection.settimeout(10)
            if self.path == '/healthz' and self.command == 'GET':
                return self.reply(200, {'status': 'ok'})
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                return self.reply(401, {'error': 'Unauthorized'})
            try:
                body = {}
                if self.command == 'POST':
                    length = int(self.headers.get('Content-Length', '0'))
                    if length <= 0 or length > MAX_BODY:
                        return self.reply(413, {'error': 'Body must be 1..2097152 bytes'})
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError('Expected JSON object')
                if self.path == '/v1/incidents' and self.command == 'GET':
                    return self.reply(200, store.listing())
                if self.path.startswith('/v1/incidents/'):
                    parts = self.path.strip('/').split('/')
                    identifier = parts[2]
                    if self.command == 'POST' and len(parts) == 4 and parts[3] == 'propose':
                        store.propose(identifier)
                        return self.reply(202, {'id': identifier})
                    if self.command == 'GET' and len(parts) == 3:
                        incident = store.get(identifier)
                        if incident:
                            incident.pop('claim', None)
                        return self.reply(200 if incident else 404, incident or {'error': 'Not found'})
                if self.command == 'POST' and self.path == '/v1/alertmanager':
                    alerts = body.get('alerts', [])
                    if not isinstance(alerts, list) or len(alerts) > 100:
                        raise ValueError('alerts must be a list of at most 100 alerts')
                    selected = []
                    for alert in alerts:
                        if not isinstance(alert, dict) or not isinstance(alert.get('labels', {}), dict):
                            raise ValueError('Invalid alert')
                        if alert.get('status') == 'firing' and alert.get('labels', {}).get('namespace') == namespace:
                            selected.append(alert)
                    if not selected:
                        return self.reply(200, {'accepted': False, 'reason': 'No firing alerts in configured namespace'})
                    # Ignore timestamps and Alertmanager grouping so repeats deduplicate.
                    identity = sorted(json.dumps(a.get('labels', {}), sort_keys=True) for a in selected)
                    incident, created = store.enqueue({'kind': 'alert', 'namespace': namespace, 'alerts': selected}, json.dumps(identity))
                    return self.reply(202, {'id': incident['id'], 'created': created})
                if self.command == 'POST' and self.path == '/v1/scan':
                    incident, created = store.enqueue({'kind': 'scan', 'namespace': namespace})
                    return self.reply(202, {'id': incident['id'], 'created': created})
                if self.command == 'POST' and self.path == '/internal/claim':
                    stage = body.get('stage')
                    if stage not in ('investigation', 'proposal'):
                        raise ValueError('Invalid stage')
                    return self.reply(200, {'incident': store.claim(stage)})
                if self.command == 'POST' and self.path == '/internal/finish':
                    stage = body.get('stage')
                    result = body.get('result', {})
                    if stage not in ('investigation', 'proposal') or not isinstance(result, dict):
                        raise ValueError('Invalid completion')
                    if any(k not in ('report', 'proposal', 'patch') or not isinstance(v, str) or len(v) > 512_000 for k, v in result.items()):
                        raise ValueError('Invalid result fields')
                    error = body.get('error')
                    if error is not None and (not isinstance(error, str) or len(error) > 4000):
                        raise ValueError('Invalid error')
                    required = 'report' if stage == 'investigation' else 'proposal'
                    if not error and not result.get(required):
                        raise ValueError('Empty agent result')
                    store.finish(body['id'], body['claim'], stage, result, error)
                    return self.reply(200, {'saved': True})
                self.reply(404, {'error': 'Not found'})
            except Conflict as exc:
                self.reply(409, {'error': str(exc)})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {'error': 'Invalid request'})
            except Exception:
                logging.exception('Request failed')
                self.reply(500, {'error': 'Internal server error'})
    return Handler


def main():
    token = os.environ['API_TOKEN']
    if len(token) < 24:
        raise SystemExit('API_TOKEN must contain at least 24 characters')
    path = os.environ.get('DATABASE_PATH', '/data/incidents.db')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(path, int(os.environ.get('LEASE_SECONDS', '1200')), os.environ.get('AUTO_PROPOSE', 'false') == 'true')
    logging.basicConfig(level=logging.INFO)
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))),
                        make_handler(store, token, os.environ.get('WATCH_NAMESPACE', 'default'))).serve_forever()


if __name__ == '__main__':
    main()
