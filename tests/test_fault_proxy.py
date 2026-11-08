"""Real loopback HTTP transports with an in-process fake MCP upstream; no external network."""

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class FaultProxy(unittest.TestCase):
    def setUp(self):
        script = ROOT / 'scripts/fault_proxy.py'
        self.assertTrue(script.is_file(), 'Fault proxy has not been implemented')
        spec = importlib.util.spec_from_file_location('fault_proxy', script)
        assert spec is not None and spec.loader is not None
        self.proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.proxy)
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.release = threading.Event()
        self.log = io.StringIO()
        context = patch('sys.stderr', self.log)
        context.start()
        self.addCleanup(context.stop)
        requests, release = self.requests, self.release

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                if self.headers.get('Transfer-Encoding') == 'chunked':
                    body = b''
                    while size := int(self.rfile.readline().split(b';')[0], 16):
                        body += self.rfile.read(size)
                        self.rfile.read(2)
                    while self.rfile.readline() != b'\r\n':
                        pass
                else:
                    body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                requests.append((self.command, self.path, dict(self.headers), body))
                self.send_response(201, 'Fictional reply')
                self.send_header('Content-Type', 'application/json')
                self.send_header('mcp-session-id', 'response-session')
                self.send_header('X-Duplicate', 'first')
                self.send_header('X-Duplicate', 'second')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_DELETE = do_POST
            do_PATCH = do_POST
            do_OPTIONS = do_POST

            def do_HEAD(self):
                self.send_response(200)
                self.send_header('Content-Length', '123')
                self.end_headers()

            def do_GET(self):
                requests.append((self.command, self.path, dict(self.headers), b''))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                if self.path == '/unframed':
                    self.send_header('Connection', 'close')
                else:
                    self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                first = b'event: message\ndata: {"part":1}\n\n'
                last = b'data: {"part":2}\n\n'
                if self.path == '/unframed':
                    self.wfile.write(first)
                elif self.path == '/single-chunk':
                    self.wfile.write(f'{len(first) + len(last):x};demo=yes\r\n'.encode() + first)
                else:
                    self.wfile.write(f'{len(first):x}\r\n'.encode() + first + b'\r\n')
                self.wfile.flush()
                release.wait(3)
                if self.path == '/unframed':
                    self.wfile.write(last)
                    self.close_connection = True
                elif self.path == '/single-chunk':
                    self.wfile.write(last + b'\r\n0\r\nX-Trailer: fictional\r\n\r\n')
                else:
                    self.wfile.write(f'{len(last):x}\r\n'.encode() + last + b'\r\n0\r\n\r\n')
                self.wfile.flush()

            def log_message(self, *args):
                pass

        self.upstream = self.start_server(ThreadingHTTPServer(('127.0.0.1', 0), Upstream))
        self.addCleanup(self.release.set)

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        return server

    def connection(self, mode='tool-error'):
        upstream = f'http://127.0.0.1:{self.upstream.server_port}'
        server = self.start_server(
            self.proxy.create_server(('127.0.0.1', 0), upstream, {'get_issue_history', 'compare_aircraft'}, mode)
        )
        client = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        self.addCleanup(client.close)
        return client

    def test_json_methods_headers_body_and_host_pass_through(self):
        for method in ('POST', 'DELETE', 'PATCH', 'OPTIONS'):
            with self.subTest(method=method):
                client = self.connection()
                body = b'{"jsonrpc":"2.0","method":"initialize","id":1,"private":"body-marker"}'
                client.request(
                    method,
                    '/mcp?sample=1',
                    body,
                    {
                        'Authorization': 'Bearer header-marker',
                        'mcp-session-id': 'request-session',
                        'Host': 'wrong.example',
                        'Content-Type': 'application/json',
                        'MCP-Protocol-Version': '2025-06-18',
                    },
                )
                response = client.getresponse()
                self.assertEqual((response.status, response.reason), (201, 'Fictional reply'))
                self.assertEqual(response.read(), body)
                self.assertEqual(response.getheader('mcp-session-id'), 'response-session')
                self.assertEqual(
                    [value for key, value in response.getheaders() if key == 'X-Duplicate'], ['first', 'second']
                )
                command, path, headers, forwarded = self.requests[-1]
                self.assertEqual((command, path, forwarded), (method, '/mcp?sample=1', body))
                self.assertEqual(headers['Authorization'], 'Bearer header-marker')
                self.assertEqual(headers['mcp-session-id'], 'request-session')
                self.assertEqual(headers['Host'], f'127.0.0.1:{self.upstream.server_port}')
                self.assertEqual(headers['MCP-Protocol-Version'], '2025-06-18')
        self.assertNotIn('header-marker', self.log.getvalue())
        self.assertNotIn('body-marker', self.log.getvalue())
        self.assertNotIn('request-session', self.log.getvalue())

    def test_stream_is_delivered_before_upstream_finishes(self):
        client = self.connection()
        client.request('GET', '/mcp')
        response = client.getresponse()
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
        self.assertEqual(response.getheader('Cache-Control'), 'no-cache')
        self.assertEqual(response.getheader('Transfer-Encoding'), 'chunked')
        self.assertEqual(response.readline(), b'event: message\n')
        self.assertEqual(response.readline(), b'data: {"part":1}\n')
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.assertEqual(response.read(), b'\ndata: {"part":2}\n\n')

    def test_large_chunk_and_unframed_stream_do_not_buffer_events(self):
        for path in ('/single-chunk', '/unframed'):
            with self.subTest(path=path):
                self.release.clear()
                client = self.connection()
                client.request('GET', path)
                response = client.getresponse()
                try:
                    self.assertEqual(response.readline(), b'event: message\n')
                    self.assertEqual(response.readline(), b'data: {"part":1}\n')
                finally:
                    self.release.set()
                self.assertEqual(response.read(), b'\ndata: {"part":2}\n\n')

    def test_chunked_calls_are_inspected_and_other_chunked_bodies_forwarded(self):
        for tool in ('get_issue_history', 'count_issues'):
            client = self.connection()
            body = json.dumps(
                {'jsonrpc': '2.0', 'id': 'chunked', 'method': 'tools/call', 'params': {'name': tool}}
            ).encode()
            client.request(
                'POST', '/mcp', iter([body[:20], body[20:]]), {'Content-Type': 'application/json'}, encode_chunked=True
            )
            response = client.getresponse()
            if tool == 'get_issue_history':
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['id'], 'chunked')
                self.assertEqual(self.requests, [])
            else:
                self.assertEqual(response.status, 201)
                self.assertEqual(response.read(), body)
                self.assertEqual(self.requests[-1][-1], body)
                self.assertEqual(self.requests[-1][2]['Transfer-Encoding'], 'chunked')

    def test_head_preserves_length_without_reading_a_body(self):
        client = self.connection()
        client.request('HEAD', '/mcp')
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Length'), '123')
        self.assertEqual(response.read(), b'')

    def test_selected_calls_fail_in_both_modes_with_original_rpc_id(self):
        for mode in ('tool-error', 'http-503'):
            for ident in (41, 'fictional-request', 0):
                for tool in ('get_issue_history', 'compare_aircraft'):
                    with self.subTest(mode=mode, ident=ident, tool=tool):
                        client = self.connection(mode)
                        client.request(
                            'POST',
                            '/mcp',
                            json.dumps(
                                {
                                    'jsonrpc': '2.0',
                                    'id': ident,
                                    'method': 'tools/call',
                                    'params': {'name': tool, 'arguments': {}},
                                }
                            ),
                            {'Content-Type': 'application/json'},
                        )
                        response = client.getresponse()
                        raw = response.read()
                        self.assertEqual(self.requests, [])
                        if mode == 'http-503':
                            self.assertEqual((response.status, raw), (503, b''))
                        else:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.getheader('Content-Type'), 'application/json')
                            self.assertEqual(
                                json.loads(raw),
                                {
                                    'jsonrpc': '2.0',
                                    'id': ident,
                                    'result': {
                                        'content': [{'type': 'text', 'text': 'Tool unavailable (simulated outage)'}],
                                        'isError': True,
                                    },
                                },
                            )

    def test_nonfailing_call_and_noncall_with_same_tool_name_are_forwarded(self):
        for method, name in (('tools/call', 'get_aircraft_summary'), ('tools/list', 'get_issue_history')):
            body = json.dumps({'jsonrpc': '2.0', 'id': 8, 'method': method, 'params': {'name': name}}).encode()
            client = self.connection()
            client.request('POST', '/mcp', body, {'Content-Type': 'application/json'})
            response = client.getresponse()
            self.assertEqual(response.status, 201)
            self.assertEqual(response.read(), body)
            self.assertEqual(self.requests[-1][-1], body)
