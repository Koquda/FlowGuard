import json
import mimetypes
import socket
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path
from urllib.parse import urlsplit
from .store import Conflict

WEB = Path(__file__).resolve().parent.parent / 'web'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def respond(self,status,data,content_type='application/json'):
        body = json.dumps(data).encode() if content_type == 'application/json' else data.encode() if isinstance(data,str) else data
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get('Content-Length','0'))
        if not 0 < length <= 8192:
            self.close_connection = True
            raise ValueError('JSON body must be between 1 and 8192 bytes')
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            raise ValueError('Content-Type must be application/json')
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/healthz':
            return self.respond(200,{'status':'ok'})
        if self.server.processor is not None:
            if path in ('/stats','/readyz'):
                return self.respond(200,self.server.processor.stats())
            return self.respond(404,{'error':'Not found'})
        if path == '/readyz':
            try:
                self.server.store.overview()
                return self.respond(200,{'status':'ready'})
            except Exception:
                return self.respond(503,{'error':'Database unavailable'})
        if path in ('/api/overview','/metrics'):
            overview = self.server.store.overview()
            try:
                with urllib.request.urlopen(self.server.processor_url+'/stats',timeout=1) as response:
                    stats = json.load(response)
                overview['processor'] = {'available':True,**stats}
            except (OSError,ValueError) as error:
                if isinstance(error,urllib.error.HTTPError):
                    error.close()
                overview['processor'] = {'available':False,'charges':None,'duplicate_charges':None}
            overview['metrics']['processor_available'] = int(overview['processor']['available'])
            overview['metrics']['processor_charges'] = overview['processor']['charges']
            overview['metrics']['duplicate_charges'] = overview['processor']['duplicate_charges']
            if path == '/metrics':
                body = ''.join(f'# TYPE flowguard_{key} gauge\nflowguard_{key} {value}\n' for key,value in overview['metrics'].items() if value is not None)
                return self.respond(200,body,'text/plain; version=0.0.4')
            return self.respond(200,overview)
        asset = WEB / ('index.html' if path == '/' else path.lstrip('/'))
        if not asset.resolve().is_relative_to(WEB.resolve()) or not asset.is_file():
            return self.respond(404,{'error':'Not found'})
        return self.respond(200,asset.read_bytes(),mimetypes.guess_type(asset)[0] or 'application/octet-stream')

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.respond(403,{'error':'Cross-origin writes are not allowed'})
            data = self.read_json()
            path = urlsplit(self.path).path
            if self.server.processor is not None:
                if path != '/charge' or not isinstance(data,dict):
                    return self.respond(404,{'error':'Not found'})
                payment_id = data.get('payment_id')
                if not isinstance(payment_id,str) or len(payment_id) != 32 or not all(c in '0123456789abcdef' for c in payment_id):
                    raise ValueError('Invalid payment_id')
                status,result,drop = self.server.processor.charge(payment_id,data)
                if drop:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                return self.respond(status,result)
            if path == '/api/payments':
                payment,replayed = self.server.store.submit(data)
                return self.respond(200 if replayed else 202,{'payment':payment,'replayed':replayed})
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api','payments'] and parts[3] == 'replay':
                return self.respond(200,{'payment':self.server.store.replay(parts[2])})
            return self.respond(404,{'error':'Not found'})
        except Conflict as error:
            self.respond(409,{'error':str(error)})
        except KeyError:
            self.respond(404,{'error':'Payment not found'})
        except (ValueError,TypeError) as error:
            self.respond(400,{'error':str(error)})


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def make_server(host, port, store=None, processor=None, processor_url='http://127.0.0.1:8081'):
    server = LocalHTTPServer((host,port),Handler)
    server.daemon_threads = True
    server.store = store
    server.processor = processor
    server.processor_url = processor_url.rstrip('/')
    return server
