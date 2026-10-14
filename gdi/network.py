"""Discover host proxies without executing shell startup files; retry network routes."""

import ast
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
from urllib.parse import urlsplit

from .git import GdiError


LOG = logging.getLogger('gdi.network')
PROXY_NAMES = frozenset(name for key in ('http_proxy', 'https_proxy', 'all_proxy',
                                       'ftp_proxy', 'no_proxy') for name in (key, key.upper()))


def proxy_settings(values):
    """Normalize case and ALL_PROXY for rclone's Go HTTP transport."""
    result = {}
    for key in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        value = values.get(key, values.get(key.upper(), ''))
        if key != 'no_proxy' and value:
            try:
                parsed = urlsplit(value)
                if parsed.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed.hostname:
                    continue
                parsed.port
            except ValueError:
                continue
        if value:
            result[key] = value
    for key in ('http_proxy', 'https_proxy'):
        if key not in result and 'all_proxy' in result:
            result[key] = result['all_proxy']
    return result


def file_settings(path):
    """Only literal KEY=value declarations; never source a shell file."""
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return {}
    values = {}
    for line in lines:
        match = re.fullmatch(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)', line)
        if not match or match[1] not in PROXY_NAMES or any(char in match[2] for char in ('$', '`')):
            continue
        try:
            tokens = shlex.split(match[2], comments=True)
        except ValueError:
            continue
        if len(tokens) <= 1:
            values[match[1]] = tokens[0] if tokens else ''
    return proxy_settings(values)


def command_output(args):
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True,
                                       timeout=.5).strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ''


def desktop_settings():
    """GNOME manual proxy settings; PAC scripts are not evaluated."""
    def setting(schema, key):
        raw = command_output(['gsettings', 'get', schema, key])
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
    if setting('org.gnome.system.proxy', 'mode') != 'manual':
        return {}
    values = {}
    for protocol in ('http', 'https', 'socks'):
        schema = 'org.gnome.system.proxy.' + protocol
        host, port = setting(schema, 'host'), setting(schema, 'port')
        if isinstance(host, str) and host and type(port) is int and 0 < port < 65536:
            host = '[' + host + ']' if ':' in host and not host.startswith('[') else host
            key = 'all_proxy' if protocol == 'socks' else protocol + '_proxy'
            values[key] = ('socks5://' if protocol == 'socks' else 'http://') + host + ':' + str(port)
    return proxy_settings(values)


def discover_proxies():
    """Read settings afresh so a running worker can follow host changes."""
    manager = {}
    for line in command_output(['systemctl', '--user', 'show-environment']).splitlines():
        key, separator, value = line.partition('=')
        if separator and key in PROXY_NAMES:
            manager[key] = value
    yield proxy_settings(manager)
    home = Path.home()
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(home / '.config')))
    environment = file_settings(Path('/etc/environment'))
    for directory in (Path('/usr/lib/environment.d'), Path('/etc/environment.d'), config / 'environment.d'):
        for path in sorted(directory.glob('*.conf')):
            environment.update(file_settings(path))
    yield environment
    for name in ('.profile', '.bash_profile', '.bashrc', '.zshenv', '.zprofile', '.zshrc'):
        yield file_settings(home / name)
    yield desktop_settings()


def network_error(error):
    """Auth, missing files and protocol errors must never trigger a route retry."""
    if not isinstance(error, GdiError):
        return False
    message = str(error).lower()
    return any(token in message for token in (
        'dial tcp:', 'dial tcp ', 'dial udp:', 'proxyconnect tcp:', 'socks connect',
        'no such host', 'server misbehaving', 'network is unreachable',
        'connection refused', 'connection reset by peer', 'i/o timeout',
        'tls handshake timeout', 'context deadline exceeded',
        'client.timeout exceeded while awaiting headers', 'temporary failure in name resolution'))


class NetworkRoutes:
    def __init__(self, *, state_path=None):
        self.selected = None
        self.discovered = []
        self.discovered_at = None
        cache = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache')))
        self.state_path = Path(state_path) if state_path is not None else cache / 'gdi/network-route.json'
        self.preferred = None
        try:
            self.preferred = json.loads(self.state_path.read_text())['route_sha256']
        except (OSError, ValueError, KeyError, TypeError):
            pass

    @staticmethod
    def identity(route):
        return hashlib.sha256(json.dumps({key: value for key, value in route.items() if key != 'no_proxy'},
                                         sort_keys=True).encode()).hexdigest()

    @staticmethod
    def available(route):
        # A dead loopback proxy is detectable without waiting for a remote timeout.
        proxies = set(value for key, value in route.items() if key in ('http_proxy', 'https_proxy'))
        for url in proxies:
            parsed = urlsplit(url)
            if parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
                return True
            try:
                with socket.create_connection((parsed.hostname, parsed.port or
                                               (443 if parsed.scheme == 'https' else 1080 if parsed.scheme.startswith('socks') else 80)),
                                              timeout=.2):
                    pass
                return True
            except OSError:
                pass
        return not proxies

    def routes(self):
        inherited = proxy_settings(os.environ)
        direct = {key: value for key, value in inherited.items() if key == 'no_proxy'}
        first = inherited if any(key != 'no_proxy' for key in inherited) else None
        routes = []
        for value in (self.selected, first, *self.alternatives(), direct):
            if value is not None:
                value = {**direct, **value}
            if value is not None and value not in routes:
                routes.append(value)
        if self.preferred is not None:
            routes.sort(key=lambda value: self.identity(value) != self.preferred)
        return routes

    def alternatives(self):
        if self.discovered_at is None or time.monotonic() - self.discovered_at >= 5:
            self.discovered = [route for route in discover_proxies()
                               if any(key != 'no_proxy' for key in route)]
            self.discovered_at = time.monotonic()
        return self.discovered

    def environment(self, route):
        environment = {key: value for key, value in os.environ.items() if key not in PROXY_NAMES}
        bypass = proxy_settings(os.environ).get('no_proxy')
        settings = {**({'no_proxy': bypass} if bypass else {}), **route}
        for key, value in settings.items():
            environment[key] = environment[key.upper()] = value
        return environment

    def run(self, operation):
        routes = self.routes()
        tried = []
        last_error = None
        while routes:
            route = routes.pop(0)
            if route in tried:
                continue
            tried.append(route)
            if not self.available(route):
                continue
            if last_error is not None:
                # Never log proxy URLs: they can include passwords.
                LOG.warning('Network route failed; retrying connection %s',
                            'through a host proxy' if any(key != 'no_proxy' for key in route) else 'directly')
            try:
                result = operation(route)
            except GdiError as error:
                if not network_error(error):
                    raise
                last_error = error
            else:
                self.selected = route
                identity = self.identity(route)
                if identity != self.preferred:
                    self.preferred = identity
                    try:
                        from .ci_protocol import atomic_write
                        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        atomic_write(self.state_path, json.dumps({'route_sha256': identity}).encode())
                    except OSError:
                        # A read-only home must not prevent network access.
                        pass
                return result
        if last_error is not None:
            raise last_error
        raise GdiError('no available network route')
