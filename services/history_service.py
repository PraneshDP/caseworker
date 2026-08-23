#!/usr/bin/env python3
"""
Calder County — Resident History API (mock)

    python3 history_service.py [--port 8083]

Endpoints
    GET  /residents/<ref>            full history for one resident
    GET  /residents/<ref>/household  household composition only
    GET  /residents/<ref>/events     case events only
    GET  /health

Modest latency, no injected failures. This service is not the hard part of the problem.
"""
import json, os, time, random, argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = json.load(open(os.path.join(HERE, '_history_data.json'), encoding='utf-8'))

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, code, payload):
        b = json.dumps(payload, indent=1).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/health':
            return self._send(200, {'status': 'ok', 'service': 'resident-history',
                                    'records': len(DATA)})

        time.sleep(random.uniform(0.10, 0.35))
        parts = [p for p in u.path.split('/') if p]

        if len(parts) >= 2 and parts[0] == 'residents':
            rec = DATA.get(parts[1])
            if rec is None:
                return self._send(404, {'error': 'not_found', 'resident_ref': parts[1]})
            if len(parts) == 2:
                return self._send(200, rec)
            if parts[2] == 'household':
                return self._send(200, {'resident_ref': rec['resident_ref'],
                                        'household': rec['household']})
            if parts[2] == 'events':
                return self._send(200, {'resident_ref': rec['resident_ref'],
                                        'events': rec['events']})
        return self._send(404, {'error': 'no_such_endpoint', 'path': u.path})

    def log_message(self, fmt, *a):
        print(f'  [history] {fmt % a}')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--port', type=int, default=8083)
    a = ap.parse_args()
    print(f'Resident History API on http://127.0.0.1:{a.port}  ({len(DATA)} residents)')
    ThreadingHTTPServer(('127.0.0.1', a.port), Handler).serve_forever()
