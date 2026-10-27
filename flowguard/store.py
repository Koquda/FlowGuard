import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

SCENARIOS = {'normal', 'transient', 'timeout_after_charge', 'declined', 'outage'}


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object')
    key = data.get('idempotency_key')
    amount = data.get('amount_minor')
    currency = data.get('currency', 'EUR')
    scenario = data.get('scenario', 'normal')
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or not key.isascii() or not all(c.isalnum() or c in '-_.' for c in key):
        raise ValueError('Use an idempotency key of 1–128 letters, digits, dots, underscores or hyphens')
    if type(amount) is not int or not 1 <= amount <= 100000000:
        raise ValueError('amount_minor must be an integer between 1 and 100000000')
    if not isinstance(currency, str) or currency not in {'EUR', 'USD', 'GBP'}:
        raise ValueError('currency must be EUR, USD or GBP')
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError('Unknown scenario')
    return {'idempotency_key': key, 'amount_minor': amount, 'currency': currency, 'scenario': scenario}


def fingerprint(data):
    return hashlib.sha256(json.dumps({k: data[k] for k in ('amount_minor', 'currency', 'scenario')}, sort_keys=True).encode()).hexdigest()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            for attempt in range(50):
                try:
                    db.execute('PRAGMA journal_mode=WAL')
                    break
                except sqlite3.OperationalError as error:
                    if 'locked' not in str(error) or attempt == 49:
                        raise
                    time.sleep(.1)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL, amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL, scenario TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','processing','retrying','succeeded','declined','dead_letter')),
                    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
                    last_error TEXT, processor_charge_id TEXT, lease_token TEXT,
                    lease_until REAL, budget_attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, payment_id TEXT NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, payment_id, kind, message):
        db.execute('INSERT INTO events(payment_id,kind,message,created_at) VALUES(?,?,?,?)', (payment_id, kind, message, time.time()))

    def count(self, db, name):
        db.execute('INSERT INTO counters VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1', (name,))

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('id','idempotency_key','amount_minor','currency','scenario','status','attempts','created_at','updated_at','next_attempt_at','last_error','processor_charge_id')}

    def submit(self, data):
        data = validate(data)
        signature = fingerprint(data)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM payments WHERE idempotency_key=?', (data['idempotency_key'],)).fetchone()
            if row:
                if row['fingerprint'] != signature:
                    raise Conflict('This idempotency key belongs to a different payment')
                self.count(db, 'duplicate_requests')
                self.event(db, row['id'], 'duplicate', 'Duplicate request returned the original payment')
                return self.public(row), True
            payment_id = uuid.uuid4().hex
            now = time.time()
            db.execute('INSERT INTO payments(id,idempotency_key,fingerprint,amount_minor,currency,scenario,status,created_at,updated_at,next_attempt_at) VALUES(?,?,?,?,?,?,\'pending\',?,?,?)', (payment_id,data['idempotency_key'],signature,data['amount_minor'],data['currency'],data['scenario'],now,now,now))
            self.event(db, payment_id, 'queued', 'Payment durably queued')
            return self.public(db.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()), False

    def claim(self, lease_seconds=15):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM payments WHERE (status IN ('pending','retrying') AND next_attempt_at<=?) OR (status='processing' AND lease_until<=?) ORDER BY next_attempt_at LIMIT 1", (now,now)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE payments SET status='processing',attempts=attempts+1,budget_attempts=budget_attempts+1,updated_at=?,lease_token=?,lease_until=? WHERE id=?", (now,token,now+lease_seconds,row['id']))
            self.event(db,row['id'],'attempt','Worker claimed payment' if row['status'] != 'processing' else 'Expired worker lease recovered')
            return dict(db.execute('SELECT * FROM payments WHERE id=?',(row['id'],)).fetchone())

    def finish(self, job, status, error=None, charge_id=None, delay=0):
        with self.connect() as db:
            changed = db.execute('UPDATE payments SET status=?,last_error=?,processor_charge_id=COALESCE(?,processor_charge_id),updated_at=?,next_attempt_at=?,lease_token=NULL,lease_until=NULL WHERE id=? AND status=\'processing\' AND lease_token=?', (status,error,charge_id,time.time(),time.time()+delay,job['id'],job['lease_token'])).rowcount
            if changed:
                self.event(db,job['id'],status,error or 'Processor confirmed one charge')
                if status == 'retrying':
                    self.count(db,'retries_total')
            return bool(changed)

    def replay(self, payment_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM payments WHERE id=?',(payment_id,)).fetchone()
            if not row:
                raise KeyError(payment_id)
            if row['status'] != 'dead_letter':
                raise Conflict('Only dead-letter payments can be replayed')
            db.execute("UPDATE payments SET status='pending',budget_attempts=0,next_attempt_at=?,updated_at=?,last_error=NULL WHERE id=?",(time.time(),time.time(),payment_id))
            self.event(db,payment_id,'replayed','Payment requeued with its original processor idempotency key')
            return self.public(db.execute('SELECT * FROM payments WHERE id=?',(payment_id,)).fetchone())

    def overview(self):
        with self.connect() as db:
            payments = [self.public(row) for row in db.execute('SELECT * FROM payments ORDER BY created_at DESC LIMIT 200')]
            events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
            counts = {row['status']:row['n'] for row in db.execute('SELECT status,count(*) n FROM payments GROUP BY status')}
            metrics = {state:counts.get(state,0) for state in ('pending','processing','retrying','succeeded','declined','dead_letter')}
            metrics['payments_total'] = sum(counts.values())
            metrics.update({row['name']:row['value'] for row in db.execute('SELECT * FROM counters')})
            for name in ('duplicate_requests','retries_total'):
                metrics.setdefault(name,0)
            metrics['protected_requests'] = metrics['duplicate_requests']
            oldest = db.execute("SELECT min(created_at) FROM payments WHERE status IN ('pending','processing','retrying')").fetchone()[0]
            metrics['oldest_pending_age_seconds'] = max(0,time.time()-oldest) if oldest is not None else 0
            return {'payments':payments,'events':events,'metrics':metrics}
