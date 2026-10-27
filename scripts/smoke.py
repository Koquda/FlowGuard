import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(url, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def wait_for(check, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError('Timed out waiting for live service behavior')


def main():
    processes = []
    with tempfile.TemporaryDirectory(prefix='flowguard-smoke-') as temp:
        api_port, processor_port = free_port(), free_port()
        base = f'http://127.0.0.1:{api_port}'
        env = dict(os.environ, FLOWGUARD_DB=f'{temp}/payments.db', PROCESSOR_DB=f'{temp}/processor.db',
                   PROCESSOR_URL=f'http://127.0.0.1:{processor_port}', HOST='127.0.0.1')
        with open(f'{temp}/services.log', 'w+') as log:
            def start(role, port=None):
                role_env = dict(env)
                if port:
                    role_env['PORT'] = str(port)
                process = subprocess.Popen([sys.executable, '-m', 'flowguard', role], cwd=ROOT,
                                           env=role_env, stdout=log, stderr=log)
                processes.append(process)
                return process

            try:
                start('processor', processor_port)
                start('api', api_port)
                worker = start('worker')
                wait_for(lambda: request(base + '/healthz'))
                payment = dict(idempotency_key='smoke-lost-response', amount_minor=4250,
                               currency='EUR', scenario='timeout_after_charge')
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda _: request(base + '/api/payments', payment), range(16)))
                ids = {result['payment']['id'] for result in results}
                assert len(ids) == 1, 'Duplicate requests created different payments'
                wait_for(lambda: next((p for p in request(base + '/api/overview')['payments']
                                       if p['id'] in ids and p['status'] == 'succeeded'), None))
                overview = request(base + '/api/overview')
                assert overview['metrics']['processor_charges'] == 1, overview
                assert overview['metrics']['duplicate_charges'] == 0, overview
                try:
                    request(base + '/api/payments', dict(payment, amount_minor=9999))
                    raise AssertionError('Changed payload accepted under the same key')
                except urllib.error.HTTPError as error:
                    assert error.code == 409, error.code
                worker.terminate()
                worker.wait(timeout=5)
                start('worker')
                request(base + '/api/payments', payment)
                overview = request(base + '/api/overview')
                assert overview['metrics']['processor_charges'] == 1, overview
                print('VERIFIED: 16 concurrent requests, payload conflict, lost-response recovery, worker restart, one processor charge.')
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == '__main__':
    main()
