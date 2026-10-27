import json
import logging
import random
import urllib.error
import urllib.request


class Worker:
    def __init__(self, store, processor_url, max_attempts=4, base_delay=1, timeout=5):
        self.store = store
        self.processor_url = processor_url.rstrip('/')
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.timeout = timeout

    def once(self):
        job = self.store.claim()
        if not job:
            return False
        data = {key:job[key] for key in ('amount_minor','currency','scenario')}
        data['payment_id'] = job['id']
        request = urllib.request.Request(self.processor_url+'/charge', json.dumps(data).encode(), {'Content-Type':'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
            if not isinstance(result,dict):
                raise ValueError('Processor returned an invalid response')
            charge_id = result.get('charge_id')
            if not isinstance(charge_id,str) or not charge_id:
                raise ValueError('Processor returned no charge identifier')
            self.store.finish(job,'succeeded',charge_id=charge_id)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 402:
                self.store.finish(job,'declined','Card declined by simulated processor')
            elif error.code in (408,425,429) or error.code >= 500:
                self.retry(job, 'Processor unavailable, HTTP '+str(error.code))
            else:
                self.store.finish(job,'dead_letter','Processor rejected the request, HTTP '+str(error.code))
        except (OSError, ValueError) as error:
            self.retry(job,'Processor response uncertain, '+type(error).__name__)
        return True

    def retry(self, job, error):
        status = 'dead_letter' if job['budget_attempts'] >= self.max_attempts else 'retrying'
        delay = min(30,self.base_delay * 2 ** min(job['budget_attempts']-1,5)) * random.uniform(.8,1.2)
        self.store.finish(job,status,error,delay=delay)
        logging.info('payment=%s status=%s attempt=%s',job['id'],status,job['attempts'])
