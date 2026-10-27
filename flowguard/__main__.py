import argparse
import logging
import os
import signal
import threading
from pathlib import Path
from .processor import ProcessorStore
from .server import make_server
from .store import Store
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description='FlowGuard local payment demo')
    parser.add_argument('service',choices=('api','worker','processor'))
    parser.add_argument('--host',default=os.environ.get('HOST',os.environ.get('FLOWGUARD_HOST','127.0.0.1')))
    parser.add_argument('--port',type=int,default=int(os.environ['PORT']) if 'PORT' in os.environ else None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    db_path = os.environ.get('PROCESSOR_DB','data/processor.db') if args.service == 'processor' else os.environ.get('FLOWGUARD_DB','data/flowguard.db')
    Path(db_path).parent.mkdir(parents=True,exist_ok=True)
    processor_url = os.environ.get('PROCESSOR_URL','http://127.0.0.1:8081')
    if args.service == 'worker':
        stop = threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stop.set())
        signal.signal(signal.SIGINT,lambda *_:stop.set())
        worker = Worker(Store(db_path),processor_url)
        while not stop.is_set():
            if not worker.once():
                stop.wait(.25)
        return
    server = make_server(args.host,args.port or (8081 if args.service == 'processor' else 8080),processor=ProcessorStore(db_path) if args.service == 'processor' else None,store=Store(db_path) if args.service == 'api' else None,processor_url=processor_url)
    logging.info('%s listening on %s:%s',args.service,*server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
