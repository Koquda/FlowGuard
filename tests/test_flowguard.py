import concurrent.futures
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from flowguard.processor import ProcessorStore
from flowguard.server import make_server
from flowguard.store import Conflict, Store
from flowguard.worker import Worker


class PaymentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path/'payments.db')
        self.processor = ProcessorStore(self.path/'processor.db')
        self.server = make_server('127.0.0.1',0,processor=self.processor)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)
        self.worker = Worker(self.store,self.url,base_delay=0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def submit(self,scenario='normal',key='order-1'):
        return self.store.submit(dict(idempotency_key=key,amount_minor=2500,currency='EUR',scenario=scenario))[0]

    def payment(self):
        return self.store.overview()['payments'][0]

    def test_concurrent_duplicate_requests_charge_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            rows = list(pool.map(lambda _: self.submit(),range(30)))
        self.assertEqual(len({row['id'] for row in rows}),1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.worker.once(),range(8)))
        self.assertEqual(self.processor.stats()['charges'],1)
        self.assertEqual(self.payment()['status'],'succeeded')
        self.assertEqual(self.store.overview()['metrics']['duplicate_requests'],29)

    def test_same_key_different_payload_conflicts(self):
        self.submit()
        with self.assertRaises(Conflict):
            self.store.submit(dict(idempotency_key='order-1',amount_minor=2501,currency='EUR',scenario='normal'))

    def test_charge_persisted_before_lost_response_recovers_after_restart(self):
        self.submit('timeout_after_charge')
        self.worker.once()
        self.assertEqual(self.payment()['status'],'retrying')
        self.assertEqual(self.processor.stats()['charges'],1)
        restarted_store = Store(self.path/'payments.db')
        restarted_worker = Worker(restarted_store,self.url,base_delay=0)
        restarted_worker.once()
        self.assertEqual(self.payment()['status'],'succeeded')
        self.assertEqual(self.processor.stats()['charges'],1)
        self.assertEqual(self.payment()['attempts'],2)

    def test_expired_lease_fences_old_worker(self):
        self.submit()
        old = self.store.claim(lease_seconds=-1)
        current = self.store.claim()
        self.assertNotEqual(old['lease_token'],current['lease_token'])
        self.assertFalse(self.store.finish(old,'declined','stale completion'))
        self.assertTrue(self.store.finish(current,'succeeded',charge_id='ch_test'))
        self.assertEqual(self.payment()['status'],'succeeded')

    def test_crash_after_charge_before_completion_does_not_charge_twice(self):
        self.submit()
        old = self.store.claim(lease_seconds=-1)
        self.processor.charge(old['id'],old)
        self.worker.once()
        self.assertEqual(self.payment()['status'],'succeeded')
        self.assertEqual(self.processor.stats()['charges'],1)
        self.assertFalse(self.store.finish(old,'retrying','stale retry'))

    def test_transient_retries_and_decline(self):
        self.submit('transient')
        for _ in range(3):
            self.worker.once()
        self.assertEqual(self.payment()['status'],'succeeded')
        self.assertEqual(self.payment()['attempts'],3)
        declined = self.submit('declined','order-2')
        self.worker.once()
        self.assertEqual(next(p for p in self.store.overview()['payments'] if p['id']==declined['id'])['status'],'declined')
        self.assertEqual(self.processor.stats()['charges'],1)

    def test_dead_letter_replay_preserves_identity(self):
        payment = self.submit('outage')
        for _ in range(4):
            self.worker.once()
        self.assertEqual(self.payment()['status'],'dead_letter')
        replayed = self.store.replay(payment['id'])
        self.assertEqual(replayed['id'],payment['id'])
        self.assertEqual(replayed['idempotency_key'],payment['idempotency_key'])
        self.assertEqual(replayed['status'],'pending')
        for _ in range(4):
            self.worker.once()
        self.assertEqual(self.payment()['status'],'dead_letter')
        self.assertEqual(self.payment()['attempts'],8)
        self.assertEqual(self.processor.stats()['charges'],0)

    def test_many_crashed_claims_do_not_overflow_retry_delay(self):
        self.submit()
        job = self.store.claim()
        job['budget_attempts'] = 10000
        self.worker.retry(job,'Processor unavailable after repeated crashes')
        self.assertEqual(self.payment()['status'],'dead_letter')

    def test_success_cannot_be_replayed(self):
        payment = self.submit()
        self.worker.once()
        with self.assertRaises(Conflict):
            self.store.replay(payment['id'])

    def test_http_validation_overview_and_metrics(self):
        api = make_server('127.0.0.1',0,store=self.store,processor_url=self.url)
        thread = threading.Thread(target=api.serve_forever,daemon=True)
        thread.start()
        base = 'http://127.0.0.1:'+str(api.server_port)
        try:
            payload = dict(idempotency_key='http-1',amount_minor=1200,currency='EUR',scenario='normal')
            req = urllib.request.Request(base+'/api/payments',json.dumps(payload).encode(),{'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status,202)
            self.worker.once()
            with urllib.request.urlopen(base+'/api/overview') as response:
                data = json.load(response)
            self.assertEqual(data['metrics']['succeeded'],1)
            self.assertEqual(data['metrics']['processor_charges'],1)
            with urllib.request.urlopen(base+'/metrics') as response:
                self.assertIn('flowguard_succeeded 1',response.read().decode())
            for value in ([],{},True,None):
                invalid = {**payload,'currency':value}
                req = urllib.request.Request(base+'/api/payments',json.dumps(invalid).encode(),{'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code,400)
                error.exception.close()
        finally:
            api.shutdown()
            api.server_close()
            thread.join()

    def test_processor_rejects_changed_payload(self):
        payment = self.submit()
        self.processor.charge(payment['id'],payment)
        with self.assertRaises(Conflict):
            self.processor.charge(payment['id'],{**payment,'amount_minor':9999})


if __name__ == '__main__':
    unittest.main()
