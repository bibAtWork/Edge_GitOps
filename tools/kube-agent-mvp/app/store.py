"""Single-replica durable queue. No third-party runtime dependencies."""
import hashlib
import json
import sqlite3
import time
import uuid


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path, lease_seconds=1200, auto_propose=False):
        self.path = path
        self.lease_seconds = lease_seconds
        self.auto_propose = auto_propose
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, dedup TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                claim TEXT, lease_until REAL, report TEXT, proposal TEXT,
                patch TEXT, error TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS dedup_index ON incidents(dedup)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result

    def enqueue(self, payload, dedup=None):
        now = time.time()
        dedup = dedup or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Suppress repeats for an hour, including completed investigations.
            previous = db.execute('SELECT * FROM incidents WHERE dedup=? AND created>? ORDER BY created DESC LIMIT 1',
                                  (dedup, now - 3600)).fetchone()
            if previous:
                return self.decode(previous), False
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO incidents(id,dedup,created,updated,status,payload) VALUES(?,?,?,?,?,?)',
                       (identifier, dedup, now, now, 'queued_investigation', json.dumps(payload)))
            return self.decode(db.execute('SELECT * FROM incidents WHERE id=?', (identifier,)).fetchone()), True

    def claim(self, stage):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Expired work is failed rather than replayed while an old worker might still be alive.
            db.execute("UPDATE incidents SET status='failed', error='Worker lease expired; inspect worker before retrying', claim=NULL, updated=? WHERE status LIKE 'running_%' AND lease_until<?", (now, now))
            if db.execute("SELECT 1 FROM incidents WHERE status LIKE 'running_%'").fetchone():
                return None
            row = db.execute('SELECT * FROM incidents WHERE status=? ORDER BY created LIMIT 1',
                             ('queued_' + stage,)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute('UPDATE incidents SET status=?, claim=?, lease_until=?, updated=? WHERE id=?',
                       ('running_' + stage, token, now + self.lease_seconds, now, row['id']))
            return self.decode(db.execute('SELECT * FROM incidents WHERE id=?', (row['id'],)).fetchone())

    def finish(self, identifier, token, stage, result, error=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM incidents WHERE id=?', (identifier,)).fetchone()
            if not row or row['claim'] != token or row['status'] != 'running_' + stage or row['lease_until'] < time.time():
                raise Conflict('Claim is missing, expired, or no longer current')
            if error:
                status = 'failed'
            elif stage == 'investigation':
                status = 'queued_proposal' if self.auto_propose else 'awaiting_review'
            else:
                status = 'complete'
            db.execute('UPDATE incidents SET status=?, updated=?, claim=NULL, lease_until=NULL, report=?, proposal=?, patch=?, error=? WHERE id=?',
                       (status, time.time(), result.get('report', row['report']), result.get('proposal', row['proposal']),
                        result.get('patch', row['patch']), error, identifier))

    def propose(self, identifier):
        with self.connect() as db:
            changed = db.execute("UPDATE incidents SET status='queued_proposal', updated=? WHERE id=? AND status='awaiting_review'", (time.time(), identifier)).rowcount
            if not changed:
                raise Conflict('Incident must be awaiting_review')

    def get(self, identifier):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM incidents WHERE id=?', (identifier,)).fetchone())

    def listing(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT id,created,updated,status,error FROM incidents ORDER BY created DESC LIMIT 100')]
