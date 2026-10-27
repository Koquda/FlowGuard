import json
import time
import urllib.request
import uuid


def call(path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request('http://127.0.0.1:8080' + path, data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


payment = dict(idempotency_key='container-' + uuid.uuid4().hex,
               amount_minor=2900, currency='EUR', scenario='timeout_after_charge')
end = time.monotonic() + 45
while True:
    try:
        accepted = call('/api/payments', payment)
        break
    except OSError:
        if time.monotonic() >= end:
            raise
        time.sleep(.25)
repeated = call('/api/payments', payment)
assert repeated['replayed'] is True
assert repeated['payment']['id'] == accepted['payment']['id']
while time.monotonic() < end:
    overview = call('/api/overview')
    item = next(p for p in overview['payments'] if p['id'] == accepted['payment']['id'])
    if item['status'] == 'succeeded':
        assert item['attempts'] >= 2
        assert item['processor_charge_id']
        assert overview['metrics']['duplicate_charges'] == 0
        print('VERIFIED: running API, durable worker, and processor recover a lost response.')
        break
    time.sleep(.25)
else:
    raise AssertionError('Running services did not complete the payment')
