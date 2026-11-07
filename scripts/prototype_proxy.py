#!/usr/bin/env python3
"""Temporary HTTPS-tunnel bridge; expose only chat, never REST records or MCP.

Development-only stdlib proxy. TLS terminates at the tunnel provider. Vercel
forwards the application bearer key; the API authenticates it on every chat call.
"""

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass  # Never log headers, questions, credentials or client addresses.

    def reject(self, status, detail):
        data = json.dumps({'detail': detail}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        if (self.command, self.path) not in {('GET', '/api/chat/status'), ('POST', '/api/chat')}:
            return self.reject(404, 'Route unavailable')
        authorization = self.headers.get('Authorization', '')
        if self.command == 'POST' and not authorization.startswith('Bearer '):
            return self.reject(401, 'Access key required')
        if self.headers.get('Transfer-Encoding'):
            return self.reject(400, 'A bounded request body is required')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.reject(400, 'Invalid request length')
        if not 0 <= length <= 32768:
            return self.reject(413, 'Request is too large')
        self.connection.settimeout(10)
        connection = HTTPConnection('127.0.0.1', 8000, timeout=130)
        started = False
        try:
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                return self.reject(400, 'Incomplete request')
            connection.request(
                self.command,
                self.path,
                body=body,
                headers={'Content-Type': 'application/json', 'Authorization': authorization},
            )
            upstream = connection.getresponse()
            self.send_response(upstream.status)
            self.send_header('Content-Type', upstream.getheader('Content-Type', 'application/json'))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            started = True
            self.connection.settimeout(130)
            while chunk := upstream.read1(8192):
                self.wfile.write(f'{len(chunk):X}\r\n'.encode() + chunk + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except OSError, TimeoutError:
            if not started:
                self.reject(502, 'The prototype API is unavailable')
            self.close_connection = True
        finally:
            connection.close()


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 8081), Handler).serve_forever()
