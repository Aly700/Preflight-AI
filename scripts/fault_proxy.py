#!/usr/bin/env python3
"""Local eval-only MCP HTTP fault proxy. Never use on the hosted demo.

Forwards HTTP messages and streams (including chunk framing and trailers) without
interpreting response bodies. Only selected JSON-RPC tools/call requests are
intercepted. Logs contain method, path, tool name and decision, never headers or
request bodies. Run on the private local stack network, without publishing a port.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import suppress
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import re
import sys
from typing import BinaryIO
from urllib.parse import urlsplit


def chunks(stream: BinaryIO | io.BufferedIOBase) -> Iterator[tuple[bytes, bytes]]:
    """Yield original HTTP chunk framing alongside decoded bytes for request inspection."""
    while True:
        line = stream.readline()
        if not line:
            raise EOFError('Truncated chunk header')
        size = int(line.split(b';', 1)[0], 16)
        if size < 0:
            raise ValueError('Negative chunk size')
        yield line, b''
        if size == 0:
            while True:
                trailer = stream.readline()
                if not trailer:
                    raise EOFError('Truncated trailers')
                yield trailer, b''
                if trailer == b'\r\n':
                    return
        while size:
            # read() can wait for the whole HTTP chunk while an SSE event is already available.
            data = getattr(stream, 'read1', stream.read)(min(size, 65536))
            if not data:
                raise EOFError('Truncated chunk')
            size -= len(data)
            yield data, data
        ending = stream.read(2)
        if ending != b'\r\n':
            raise ValueError('Invalid chunk ending')
        yield ending, b''


def create_server(address: tuple[str, int], upstream: str, fail: set[str], mode: str) -> ThreadingHTTPServer:
    """Construct a server without starting it; tests bind both peers to loopback."""
    target = urlsplit(upstream)
    if (
        target.scheme != 'http'
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or target.path not in ('', '/')
        or target.query
        or target.fragment
    ):
        raise ValueError('--upstream must be an http://host:port origin without credentials or a path')
    port = target.port or 80
    if mode not in ('tool-error', 'http-503'):
        raise ValueError('Unknown fault mode')
    hostname = target.hostname
    host_header = f'[{hostname}]:{port}' if ':' in hostname else f'{hostname}:{port}'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format: str, *args: object) -> None:
            # BaseHTTPRequestHandler's request/error logs are deliberately disabled.
            pass

        def __getattr__(self, name: str) -> Callable[[], None]:
            if name.startswith('do_'):
                return self.forward
            raise AttributeError(name)

        def log_decision(self, tool: str | None, decision: str) -> None:
            print(
                json.dumps({'method': self.command, 'path': self.path, 'tool': tool, 'decision': decision}),
                file=sys.stderr,
                flush=True,
            )

        def reply(self, status: int, body: bytes = b'', content_type: str | None = None) -> None:
            self.send_response_only(status)
            if content_type:
                self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def forward(self) -> None:
            connection = HTTPConnection(hostname, port, timeout=60)
            started = False
            tool = None
            try:
                if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
                    wire, decoded = io.BytesIO(), io.BytesIO()
                    for raw, data in chunks(self.rfile):
                        wire.write(raw)
                        decoded.write(data)
                    body, inspect = wire.getvalue(), decoded.getvalue()
                else:
                    length = int(self.headers.get('Content-Length', '0'))
                    if length < 0:
                        raise ValueError('Negative body length')
                    body = inspect = self.rfile.read(length)
                    if len(body) != length:
                        raise EOFError('Truncated request')
                try:
                    request = json.loads(inspect)
                except ValueError, UnicodeError:
                    request = None
                if isinstance(request, dict) and request.get('method') == 'tools/call':
                    params = request.get('params')
                    name = params.get('name') if isinstance(params, dict) else None
                    if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name):
                        tool = name
                if tool in fail:
                    self.log_decision(tool, mode)
                    if mode == 'http-503':
                        self.reply(503)
                    else:
                        fault_result = {
                            'jsonrpc': '2.0',
                            'id': request.get('id'),
                            'result': {
                                'content': [{'type': 'text', 'text': 'Tool unavailable (simulated outage)'}],
                                'isError': True,
                            },
                        }
                        self.reply(200, json.dumps(fault_result).encode(), 'application/json')
                    return
                self.log_decision(tool, 'forward')
                connection.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for key, value in self.headers.items():
                    if key.lower() != 'host':
                        connection.putheader(key, value)
                connection.putheader('Host', host_header)
                connection.endheaders(body)
                with connection.getresponse() as response:
                    self.send_response_only(response.status, response.reason)
                    for key, value in response.getheaders():
                        self.send_header(key, value)
                    self.end_headers()
                    started = True
                    self.close_connection = self.close_connection or response.will_close
                    if self.command == 'HEAD' or response.status in (204, 304) or response.status < 200:
                        return
                    assert response.fp is not None
                    if response.chunked:
                        # Work on the wire bytes so extensions and trailers survive unchanged.
                        for raw, _ in chunks(response.fp):
                            self.wfile.write(raw)
                            self.wfile.flush()
                    else:
                        remaining = response.length
                        while remaining is None or remaining > 0:
                            data = response.fp.read1(65536 if remaining is None else min(65536, remaining))
                            if not data:
                                if remaining:
                                    raise EOFError('Truncated upstream response')
                                break
                            self.wfile.write(data)
                            self.wfile.flush()
                            if remaining is not None:
                                remaining -= len(data)
            except OSError, HTTPException, ValueError, EOFError:
                self.log_decision(tool, 'transport-error')
                self.close_connection = True
                if not started:
                    with suppress(OSError):
                        self.reply(502)
            finally:
                connection.close()

    return ThreadingHTTPServer(address, Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen-port', type=int, default=8002)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--fail', required=True, help='Comma-separated tool names')
    parser.add_argument('--mode', choices=['tool-error', 'http-503'], default='tool-error')
    args = parser.parse_args()
    if not 1 <= args.listen_port <= 65535:
        parser.error('--listen-port must be between 1 and 65535')
    try:
        server = create_server(('0.0.0.0', args.listen_port), args.upstream, set(args.fail.split(',')), args.mode)
    except ValueError, OSError:
        parser.error('Invalid upstream origin or listen address')
    with server, suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == '__main__':
    main()
