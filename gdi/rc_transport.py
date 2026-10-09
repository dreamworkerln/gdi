"""Scoped native rclone RC transport over a private authenticated Unix socket."""

import base64
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

from gdi.branches import directory_ref
from gdi.git import GdiError
from gdi.transport import Rclone
from gdi.diagnostics import rc_call, subprocess_call
from gdi.diagnostics import note


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=180):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            raise


class RcError(GdiError):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(f'RC HTTP {status}: {detail[:2000]}')


class RcServer:
    def __init__(self, *, executable='rclone', timeout=180, trace_path=None, options=None):
        self.executable = executable
        self.timeout = timeout
        self.process = None
        self.temporary = None
        self.errors = None
        self.events = []
        self.closed = False
        self.trace_path = Path(trace_path) if trace_path else None
        self.trace = None
        self.options = options or {}
        self.lock = threading.RLock()
        self.profile_call = None
        self.profile_result = None
        self.password = secrets.token_urlsafe(32)
        self.authorization = 'Basic ' + base64.b64encode(
            ('gdi:' + self.password).encode()).decode()

    def __enter__(self):
        if self.trace_path is not None:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.trace_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self.trace = os.fdopen(descriptor, 'a', encoding='utf-8')
        self.temporary = tempfile.TemporaryDirectory(prefix='gdi-rc-')
        self.directory = Path(self.temporary.name)
        self.path = self.directory / 'rc.sock'
        self.errors = (self.directory / 'stderr').open('w+')
        # Do not inherit RC listeners, auth, files or browser settings.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('RCLONE_RC_')}
        environment.update(RCLONE_RC_USER='gdi', RCLONE_RC_PASS=self.password)
        args = [self.executable, 'rcd', '--rc-addr', str(self.path), '--rc-serve',
                '--contimeout', str(self.options.get('connect_timeout_seconds', 10)) + 's',
                '--timeout', str(self.options.get('timeout_seconds', 60)) + 's',
                '--retries', str(self.options.get('retries', 3)),
                '--low-level-retries', str(self.options.get('low_level_retries', 3)),
                '--rc-server-read-timeout', '0', '--rc-server-write-timeout', '0']
        started = time.monotonic()
        self.record({'event': 'process_start', 'argv': args})
        self.profile_call = subprocess_call(args)
        self.profile_result = self.profile_call.__enter__()
        try:
            if not sys.platform.startswith('linux'):
                raise GdiError('native RC requires Linux parent-death protection; use GDI_TRANSPORT=cli')
            child = str(Path(__file__).with_name('rc_child.py'))
            self.process = subprocess.Popen([sys.executable, child, str(os.getpid()), *args],
                                            env=environment, stdout=subprocess.DEVNULL,
                                            stderr=self.errors)
            deadline = time.monotonic() + 10
            while not self.path.exists():
                if self.process.poll() is not None or time.monotonic() > deadline:
                    self.errors.seek(0)
                    detail = self.errors.read(2000).replace(self.password, '[redacted]')
                    raise GdiError('RC startup failed: ' + detail)
                time.sleep(0.02)
            self.record({'event': 'process_ready', 'seconds': time.monotonic() - started})
            return self
        except BaseException as error:
            self.profile_result['error'] = error
            self.record({'event': 'process_startup_failure', 'error': type(error).__name__})
            self.close()
            raise

    def record(self, value):
        value = {'time': datetime.now(timezone.utc).isoformat(), **value}
        self.events.append(value)
        if self.trace is not None:
            self.trace.write(json.dumps(value, ensure_ascii=False) + '\n')
            self.trace.flush()

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.record({'event': 'process_finish', 'returncode': self.process.returncode})
            self.profile_result['result'] = subprocess.CompletedProcess(
                [self.executable, 'rcd'], self.process.returncode, '', '')
        if self.profile_call is not None:
            self.profile_call.__exit__(None, None, None)
            self.profile_call = None
        if self.errors is not None:
            self.errors.close()
            self.errors = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None
        if self.trace is not None:
            self.trace.close()
            self.trace = None

    def transport(self, url):
        return RcTransport(url, self)

    def request(self, route, value=None, *, get=False):
        # Serialize cache resets with operations, including worker log publication.
        with self.lock, rc_call('read' if get else route.lstrip('/'), value) as profile:
            return self._request(route, value, get=get, profile=profile)

    def _request(self, route, value=None, *, get=False, profile=None):
        if self.process is None or self.process.poll() is not None:
            raise GdiError('RC process is not running')
        call_id = len(self.events)
        self.record({'event': 'rc_start', 'call_id': call_id,
                            'route': route, 'parameters': value})
        started = time.monotonic()
        connection = UnixHTTP(self.path, self.timeout)
        status, error = None, None
        try:
            connection.request('GET' if get else 'POST', route,
                               None if get else json.dumps(value or {}),
                               {'Content-Type': 'application/json',
                                'Authorization': self.authorization})
            response = connection.getresponse()
            status = response.status
            profile['status'] = status
            # GET is used only for bounded metadata, never bundles/artifacts.
            raw = response.read(1024 * 1024 + 1) if get else response.read()
            if status != 200:
                detail = raw.decode('utf-8', errors='replace').replace(self.password, '[redacted]')
                raise RcError(status, detail)
            if get:
                if len(raw) > 1024 * 1024:
                    raise GdiError('metadata exceeds 1 MiB')
                return raw
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise GdiError('invalid RC response JSON') from exc
            if not isinstance(data, dict):
                raise GdiError('invalid RC response')
            return data
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            connection.close()
            self.record({'event': 'rc_finish', 'call_id': call_id,
                                'seconds': time.monotonic() - started,
                                'status': status, 'error': error})


def relative_path(path, *, empty=False):
    if (not isinstance(path, str) or not path and not empty or
            path and any(part in ('', '.', '..') for part in path.split('/')) or
            '\\' in path or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise GdiError('invalid RC relative path')
    return path


class RcTransport(Rclone):
    def __init__(self, url, server):
        super().__init__(url)
        self.server = server

    def call(self, *args, **kwargs):
        raise AssertionError('native RC transport must not spawn CLI commands')

    def api(self, endpoint, **value):
        return self.server.request('/' + endpoint, value)

    def mkdir(self, relative):
        self.api('operations/mkdir', fs=self.url, remote=relative_path(relative, empty=True))

    def list(self, relative, *, recursive=False):
        relative_path(relative, empty=True)
        data = self.api('operations/list', fs=self.url, remote=relative,
                        opt={'recurse': recursive, 'noModTime': True, 'noMimeType': True}).get('list')
        if not isinstance(data, list) or any(not isinstance(item, dict) or
                not isinstance(item.get('Path'), str) or type(item.get('IsDir')) is not bool
                for item in data):
            raise GdiError('invalid RC directory listing')
        prefix = relative + '/' if relative else ''
        result = []
        for item in data:
            if not item['Path'].startswith(prefix):
                raise GdiError('RC listing path outside requested directory')
            result.append({**item, 'Path': item['Path'][len(prefix):]})
        names = [item['Path'] for item in result]
        if len(names) != len(set(names)):
            raise GdiError('duplicate paths on remote')
        return result

    def read(self, relative):
        relative_path(relative)
        with self.server.lock:
            if relative == 'repository.json':
                # Drive backends retain folder IDs inside cached Fs objects.
                # A new Fs resolves the current path rather than the renamed folder.
                self.api('fscache/clear')
            return self.server.request('/' + quote('[' + self.url + ']/' + relative, safe='/'),
                                       {'fs': self.url, 'remote': relative}, get=True)

    def publication_listing(self, ref):
        from .branches import branch_directory
        branch = branch_directory(ref)
        try:
            rows = self.list('branches/' + branch)
        except RcError as error:
            if error.status != 404:
                raise
            # Only an absent selected branch is empty. A missing branches root
            # or duplicate folder names must still fail like the CLI transport.
            parents = self.list('branches')
            if any(item['Path'] == branch for item in parents):
                raise
            return []
        return [{**item, 'Path': branch + '/' + item['Path']} for item in rows]

    def read_optional(self, relative):
        relative_path(relative)
        value = self.api('operations/stat', fs=self.url, remote=relative,
                         opt={'filesOnly': True}).get('item')
        if value is None:
            return None
        if not isinstance(value, dict) or value.get('IsDir') is not False:
            raise GdiError('expected a regular remote metadata file')
        return self.read(relative)

    def read_many(self, relatives):
        paths = list(relatives)
        if len(paths) != len(set(paths)) or any(not re.fullmatch(
                r'branches/[^/]+/[0-9a-f]{64}\.json', path) for path in paths):
            raise GdiError('invalid batch publication paths')
        for path in paths:
            directory_ref(path.split('/')[1])
        if not paths:
            return {}
        if len(paths) == 1:
            return {paths[0]: self.read(paths[0])}
        note('download publication metadata batch', files=len(paths), paths=paths)
        with tempfile.TemporaryDirectory(prefix='gdi-rc-metadata-') as directory:
            root = Path(directory)
            selection = root / 'files.txt'
            selection.write_text(''.join(path + '\n' for path in paths), encoding='utf-8')
            destination = root / 'snapshot'
            self.api('sync/copy', srcFs=self.url, dstFs=str(destination),
                     _config={'NoTraverse': True, 'IgnoreTimes': True},
                     _filter={'FilesFromRaw': [str(selection)]})
            result = {}
            for path in paths:
                source = destination / path
                if source.is_symlink() or not source.is_file():
                    raise GdiError('remote metadata missing: ' + path)
                with source.open('rb') as handle:
                    raw = handle.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise GdiError('metadata exceeds 1 MiB')
                result[path] = raw
            return result

    def download(self, relative, target):
        target = Path(target).resolve()
        self.api('operations/copyfile', srcFs=self.url, srcRemote=relative_path(relative),
                 dstFs=str(target.parent), dstRemote=target.name,
                 _config={'IgnoreTimes': True})

    def upload(self, source, relative):
        relative_path(relative)
        source = Path(source).resolve(strict=True)
        note('upload immutable file', path=relative, source_file_bytes=source.stat().st_size)
        with tempfile.TemporaryDirectory(prefix='gdi-rc-upload-') as directory:
            staged = Path(directory) / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, staged)
            except OSError:
                shutil.copyfile(source, staged)
            self.api('sync/copy', srcFs=directory, dstFs=self.url,
                     _config={'NoTraverse': True, 'Immutable': True, 'CheckSum': True})

    def update_advisory(self, source, relative):
        if not re.fullmatch(r'ci/(?:workers/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/(?:status|capabilities)|jobs/[0-9a-f]{32}/status)\.json', relative):
            raise GdiError('only CI advisory snapshots may be overwritten')
        source = Path(source).resolve(strict=True)
        self.api('operations/copyfile', srcFs=str(source.parent), srcRemote=source.name,
                 dstFs=self.url, dstRemote=relative, _config={'IgnoreTimes': True})

    def delete_queue(self, relative):
        if not re.fullmatch(r'ci/queue/[0-9a-f]{32}\.json', relative):
            raise GdiError('only a single CI queue pointer may be removed')
        self.api('operations/deletefile', fs=self.url, remote=relative)

    def delete_notification(self, relative):
        if not re.fullmatch(r'inbox/[0-9a-f]{32}-[0-9a-f]{64}\.json', relative):
            raise GdiError('only a single immutable inbox notification may be removed')
        self.api('operations/deletefile', fs=self.url, remote=relative)

    def delete_bundle(self, relative):
        if not re.fullmatch(r'bundles/[0-9a-f]{64}\.bundle', relative):
            raise GdiError('GC may delete only a single content-addressed bundle')
        self.api('operations/deletefile', fs=self.url, remote=relative)


class TransportSession:
    """Lazy RC process shared only within one command or one worker operation."""

    def __init__(self, *, options=None):
        self.options = options or {}
        self.server = None
        self.depth = 0
        self.mode = os.environ.get('GDI_TRANSPORT', 'rc' if sys.platform.startswith('linux') else 'cli')
        if self.mode not in ('cli', 'rc'):
            raise GdiError('GDI_TRANSPORT must be rc or cli')

    def __enter__(self):
        self.depth += 1
        return self

    def __exit__(self, *args):
        self.depth -= 1
        if self.depth == 0:
            self.close()

    def close(self):
        if self.server is not None:
            try:
                self.server.close()
            finally:
                self.server = None

    def __call__(self, url):
        if self.depth == 0:
            raise GdiError('rclone transport used outside its operation lifetime')
        if self.mode == 'cli':
            return Rclone(url, options=self.options)
        # Worker starts its first request on the main operation thread before
        # starting the log publisher. No process is spawned for local-only commands.
        if self.server is None:
            self.server = RcServer(options=self.options)
            self.server.__enter__()
        return self.server.transport(url)
