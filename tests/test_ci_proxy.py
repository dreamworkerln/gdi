"""CI keeps one private proxy address while connection routes change."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from gdi.ci_proxy import CiProxy, read_exact
from gdi.git import GdiError
from gdi.network import NetworkRoutes


class CiProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for context in (patch.dict(os.environ, {}, clear=True),
                        patch('gdi.network.discover_proxies', return_value=[])):
            context.start()
            self.addCleanup(context.stop)
        self.network = NetworkRoutes(state_path=self.root / 'route.json')
        self.proxy = CiProxy(self.network)
        self.addCleanup(self.proxy.close)
        self.endpoint = urlsplit(self.proxy.url)
        self.authorization = 'Basic ' + base64.b64encode(
            (self.endpoint.username + ':' + self.endpoint.password).encode()).decode()

    def server(self, server_type, handler):
        server = server_type(('127.0.0.1', 0), handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def connection(self):
        connection = socket.create_connection((self.endpoint.hostname, self.endpoint.port), timeout=2)
        self.addCleanup(connection.close)
        return connection

    @staticmethod
    def headers(connection):
        data = bytearray()
        while not data.endswith(b'\r\n\r\n'):
            data.extend(read_exact(connection, 1))
        return bytes(data)

    def test_connect_tunnels_bytes_and_rejects_unauthenticated_requests(self):
        class Echo(BaseRequestHandler):
            def handle(self):
                self.request.sendall(read_exact(self.request, 4))
        echo = self.server(ThreadingTCPServer, Echo)
        connection = self.connection()
        connection.sendall(('CONNECT 127.0.0.1:' + str(echo.server_address[1]) + ' HTTP/1.1\r\n'
                            'Proxy-Authorization: ' + self.authorization + '\r\n\r\n').encode())
        self.assertIn(b'200', self.headers(connection).split(b'\r\n')[0])
        connection.sendall(b'ping')
        self.assertEqual(read_exact(connection, 4), b'ping')
        connection = self.connection()
        connection.sendall(b'CONNECT example.invalid:443 HTTP/1.1\r\n\r\n')
        self.assertIn(b'407', self.headers(connection).split(b'\r\n')[0])

    def test_http_post_forwards_body_without_forwarding_proxy_credentials(self):
        seen = []
        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                seen.append((body, self.headers.get('Proxy-Authorization')))
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        origin = self.server(ThreadingHTTPServer, Origin)
        connection = self.connection()
        connection.sendall(('POST http://127.0.0.1:' + str(origin.server_port) + '/body HTTP/1.1\r\n'
                            'Host: 127.0.0.1\r\nContent-Length: 4\r\n'
                            'Proxy-Authorization: ' + self.authorization + '\r\n\r\nping').encode())
        self.assertIn(b'200', self.headers(connection).split(b'\r\n')[0])
        self.assertEqual(read_exact(connection, 4), b'ping')
        self.assertEqual(seen, [(b'ping', None)])

    def test_each_new_connection_can_switch_without_changing_ci_environment(self):
        route = {'https_proxy': 'http://127.0.0.1:10809'}
        self.network.discovered = [route]
        self.network.discovered_at = float('inf')
        calls = []
        connections = []
        def connect(candidate, host, port, *, secure):
            calls.append(candidate)
            if candidate:
                raise GdiError('dial tcp: connection refused')
            left, right = socket.socketpair()
            connections.extend((left, right))
            return left
        before = self.proxy.environment()['HTTPS_PROXY']
        with patch('gdi.ci_proxy.connect', side_effect=connect), \
                patch.object(NetworkRoutes, 'available', return_value=True):
            connection = self.connection()
            connection.sendall(('CONNECT example.invalid:443 HTTP/1.1\r\nProxy-Authorization: ' +
                                self.authorization + '\r\n\r\n').encode())
            self.assertIn(b'200', self.headers(connection).split(b'\r\n')[0])
        for connection in connections:
            connection.close()
        self.assertEqual(calls, [route, {}])
        self.assertEqual(self.network.selected, {})
        self.assertEqual(self.proxy.environment()['HTTPS_PROXY'], before)

    def test_plain_http_uses_upstream_forwarding_without_connect(self):
        seen = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                seen.append((self.path, self.headers.get('Proxy-Authorization')))
                self.send_response(200)
                self.send_header('Content-Length', '4')
                self.end_headers()
                self.wfile.write(b'pong')
        upstream = self.server(ThreadingHTTPServer, Upstream)
        route = {'http_proxy': 'http://owner:password@127.0.0.1:' + str(upstream.server_port)}
        self.network.discovered = [route]
        self.network.discovered_at = float('inf')
        connection = self.connection()
        connection.sendall(('GET http://example.invalid/path HTTP/1.1\r\nHost: example.invalid\r\n'
                            'Proxy-Authorization: ' + self.authorization + '\r\n\r\n').encode())
        self.assertIn(b'200', self.headers(connection).split(b'\r\n')[0])
        self.assertEqual(read_exact(connection, 4), b'pong')
        self.assertEqual(seen, [('http://example.invalid/path',
                                'Basic ' + base64.b64encode(b'owner:password').decode())])
