"""Private host HTTP proxy: each CI connection follows automatic network routes."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import select
import socket
import ssl
import threading
from urllib.parse import unquote, urlsplit
from urllib.request import proxy_bypass_environment

from .git import GdiError


def read_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise OSError('proxy closed connection')
        result.extend(chunk)
    return bytes(result)


def authority(host, port):
    return ('[' + host + ']' if ':' in host else host) + ':' + str(port)


def effective_proxy(route, host, port, *, secure):
    if proxy_bypass_environment(authority(host, port), route):
        return None
    return route.get('https_proxy' if secure else 'http_proxy')


def proxy_authorization(parsed):
    if parsed.username is None:
        return ''
    credentials = unquote(parsed.username) + ':' + unquote(parsed.password or '')
    return 'Proxy-Authorization: Basic ' + base64.b64encode(credentials.encode()).decode() + '\r\n'


def connect(route, host, port, *, secure):
    """Establish a direct, HTTP CONNECT, HTTPS CONNECT or SOCKS5 tunnel."""
    proxy = effective_proxy(route, host, port, secure=secure)
    connection = None
    try:
        if not proxy:
            return socket.create_connection((host, port), timeout=5)
        parsed = urlsplit(proxy)
        proxy_port = parsed.port or (443 if parsed.scheme == 'https' else
                                     1080 if parsed.scheme.startswith('socks') else 80)
        connection = socket.create_connection((parsed.hostname, proxy_port), timeout=5)
        if parsed.scheme == 'https':
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=parsed.hostname)
        if parsed.scheme in ('http', 'https'):
            if not secure:
                # Forward plain HTTP in absolute form. HTTP proxies need not
                # allow CONNECT to port 80, and request bodies must not be replayed.
                return connection
            target = authority(host, port)
            headers = 'CONNECT ' + target + ' HTTP/1.1\r\nHost: ' + target + '\r\n'
            headers += proxy_authorization(parsed)
            connection.sendall((headers + '\r\n').encode('ascii'))
            response = bytearray()
            while not response.endswith(b'\r\n\r\n'):
                if len(response) >= 65536:
                    raise GdiError('invalid upstream proxy response')
                response.extend(read_exact(connection, 1))
            status = bytes(response).split(b'\r\n', 1)[0].split()
            if len(status) < 2 or status[1] != b'200':
                if len(status) >= 2 and status[1] == b'407':
                    raise GdiError('upstream proxy authentication required')
                raise GdiError('proxyconnect tcp: upstream proxy refused connection')
        else:
            authenticated = parsed.username is not None
            connection.sendall(b'\x05\x02\x00\x02' if authenticated else b'\x05\x01\x00')
            version, method = read_exact(connection, 2)
            if version != 5 or method not in (0, 2) or method == 2 and not authenticated:
                raise GdiError('upstream SOCKS proxy authentication unavailable')
            if method == 2:
                username = unquote(parsed.username).encode()
                password = unquote(parsed.password or '').encode()
                if len(username) > 255 or len(password) > 255:
                    raise GdiError('invalid SOCKS credentials')
                connection.sendall(b'\x01' + bytes([len(username)]) + username + bytes([len(password)]) + password)
                if read_exact(connection, 2) != b'\x01\x00':
                    raise GdiError('upstream SOCKS proxy authentication failed')
            hostname = host.encode('idna')
            if len(hostname) > 255:
                raise GdiError('invalid proxy destination')
            connection.sendall(b'\x05\x01\x00\x03' + bytes([len(hostname)]) + hostname + port.to_bytes(2, 'big'))
            version, status, reserved, kind = read_exact(connection, 4)
            if version != 5 or status != 0:
                raise GdiError('socks connect: upstream proxy refused connection')
            size = {1: 4, 4: 16}.get(kind)
            if kind == 3:
                size = read_exact(connection, 1)[0]
            if size is None:
                raise GdiError('invalid SOCKS proxy response')
            read_exact(connection, size + 2)
        return connection
    except (OSError, GdiError) as error:
        if connection is not None:
            connection.close()
        if isinstance(error, GdiError):
            raise
        # Keep URLs and proxy credentials out of worker logs.
        raise GdiError('dial tcp: CI connection failed (' + type(error).__name__ + ')') from error


class CiProxy:
    def __init__(self, network):
        self.network = network
        self.stop = threading.Event()
        token = secrets.token_urlsafe(32)
        authorization = 'Basic ' + base64.b64encode(('gdi:' + token).encode()).decode()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10
            rbufsize = 0  # Do not consume a POST body before tunnelling it.

            def log_message(self, *args):
                pass  # Request URLs and authorization are private.

            def handle_proxy(self, *, tunnel=False):
                if not secrets.compare_digest(self.headers.get('Proxy-Authorization', ''), authorization):
                    self.send_error(407)
                    return
                upstream = None
                established = False
                try:
                    parsed = urlsplit('//' + self.path if tunnel else self.path)
                    if not parsed.hostname or parsed.username is not None or not tunnel and parsed.scheme != 'http':
                        self.send_error(400)
                        return
                    port = parsed.port or (443 if tunnel else 80)
                    upstream, proxy = owner.network.run(lambda route:
                        (connect(route, parsed.hostname, port, secure=tunnel),
                         effective_proxy(route, parsed.hostname, port, secure=tunnel)))
                    upstream.settimeout(None)
                    if tunnel:
                        self.send_response(200, 'Connection established')
                        self.end_headers()
                        self.wfile.flush()
                    else:
                        path = parsed.path or '/'
                        if parsed.query:
                            path += '?' + parsed.query
                        headers = ''.join(key + ': ' + value + '\r\n' for key, value in self.headers.items()
                                          if key.lower() not in ('proxy-authorization', 'proxy-connection', 'connection'))
                        if proxy and urlsplit(proxy).scheme in ('http', 'https'):
                            path = self.path
                            headers += proxy_authorization(urlsplit(proxy))
                        upstream.sendall((self.command + ' ' + path + ' HTTP/1.1\r\n' + headers +
                                          'Connection: close\r\n\r\n').encode('latin-1'))
                    established = True
                    # Stream without replaying application data. A new connection
                    # can choose another route; an interrupted request stays failed.
                    sockets = [self.connection, upstream]
                    while not owner.stop.is_set():
                        readable, _, _ = select.select(sockets, (), (), .5)
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                if source is upstream:
                                    return
                                sockets.remove(source)
                                upstream.shutdown(socket.SHUT_WR)
                                continue
                            destination = upstream if source is self.connection else self.connection
                            destination.sendall(data)
                except (GdiError, OSError, ValueError):
                    if not established:
                        self.send_error(502, 'CI network connection unavailable')
                finally:
                    if upstream is not None:
                        upstream.close()
                    self.close_connection = True

            def do_CONNECT(self):
                self.handle_proxy(tunnel=True)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = handle_proxy

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.url = 'http://gdi:' + token + '@127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='gdi-ci-proxy')
        self.thread.start()

    def environment(self):
        inherited = self.network.environment(self.network.selected or {})
        for key in ('http_proxy', 'https_proxy'):
            inherited[key] = inherited[key.upper()] = self.url
        for key in ('all_proxy', 'ALL_PROXY'):
            inherited.pop(key, None)
        bypass = list(dict.fromkeys(item.strip() for item in inherited.get('no_proxy', '').split(',') if item.strip()))
        for host in ('localhost', '127.0.0.1', '::1'):
            if host not in bypass:
                bypass.append(host)
        inherited['no_proxy'] = inherited['NO_PROXY'] = ','.join(bypass)
        return inherited

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
