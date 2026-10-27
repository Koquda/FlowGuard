import sqlite3
from contextlib import closing
import time
import uuid
from .store import Conflict, fingerprint, validate


class ProcessorStore:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS charges(payment_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, charge_id TEXT NOT NULL UNIQUE, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(payment_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL);
            ''')

    def charge(self, payment_id, data):
        data = validate({**data,'idempotency_key':payment_id})
        signature = fingerprint(data)
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM charges WHERE payment_id=?',(payment_id,)).fetchone()
            if existing:
                if existing['fingerprint'] != signature:
                    raise Conflict('Processor key payload conflict')
                return 200, {'charge_id':existing['charge_id'],'replayed':True}, False
            db.execute('INSERT INTO calls VALUES(?,1) ON CONFLICT(payment_id) DO UPDATE SET attempts=attempts+1',(payment_id,))
            attempts = db.execute('SELECT attempts FROM calls WHERE payment_id=?',(payment_id,)).fetchone()[0]
            scenario = data['scenario']
            if scenario == 'declined':
                return 402, {'error':'Card declined by simulated processor'}, False
            if scenario == 'outage' or scenario == 'transient' and attempts <= 2:
                return 503, {'error':'Simulated processor unavailable'}, False
            charge_id = 'ch_' + uuid.uuid4().hex[:20]
            db.execute('INSERT INTO charges VALUES(?,?,?,?,?,?)',(payment_id,signature,charge_id,data['amount_minor'],data['currency'],time.time()))
        return 200, {'charge_id':charge_id,'replayed':False}, scenario == 'timeout_after_charge'

    def stats(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return {'charges':db.execute('SELECT count(*) FROM charges').fetchone()[0], 'duplicate_charges':0}
