"""The public prototype bridge must not expose the existing REST/database routes."""

from http.server import ThreadingHTTPServer
import importlib
import threading
import unittest
import urllib.error
import urllib.request


class PrototypeProxy(unittest.TestCase):
    def test_only_chat_routes_are_admitted(self):
        try:
            handler = importlib.import_module('scripts.prototype_proxy').Handler
        except ModuleNotFoundError:
            self.fail('Prototype bridge is not implemented')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, method, expected in [
                ('/api/aircraft', 'GET', 404),
                ('/api/issues', 'GET', 404),
                ('/health', 'GET', 404),
                ('/mcp', 'POST', 404),
                ('/api/chat', 'POST', 401),
            ]:
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        urllib.request.Request('http://127.0.0.1:' + str(server.server_port) + path, method=method),
                        timeout=3,
                    )
                self.assertEqual(caught.exception.code, expected)
                caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
