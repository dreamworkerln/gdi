"""Local Git-inbox barrier, independent of CI execution and Drive polling."""

import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading
from types import SimpleNamespace

from .git import GdiError


def directory():
    path = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / ('gdi-control-' + str(os.getuid()))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GdiError('unsafe local worker control directory')
    return path


def prefix(root):
    return hashlib.sha256(root.encode()).hexdigest()[:24]


def synchronize(settings, *, timeout=300):
    """No listener means normal remote-only operation; a connected failure is fatal."""
    root = settings.get('inbox_root')
    if root is None:
        return
    for path in sorted(directory().glob(prefix(root) + '-*.sock')):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            try:
                connection.connect(str(path))
            except (FileNotFoundError, ConnectionRefusedError):
                continue
            try:
                connection.sendall(json.dumps({'repository_id': settings['repository_id'],
                    'remote_url': settings['url']}).encode() + b'\n')
                with connection.makefile('rb') as stream:
                    raw = stream.readline(65537)
                if len(raw) > 65536 or not raw.endswith(b'\n'):
                    raise GdiError('invalid local worker synchronization response')
                response = json.loads(raw)
                if not isinstance(response, dict):
                    raise GdiError('invalid local worker synchronization response')
                if response.get('ok') is not True:
                    raise GdiError('local worker synchronization failed: ' + str(response.get('error', 'invalid response')))
            except (OSError, ValueError) as exc:
                raise GdiError('local worker synchronization failed: ' + str(exc)) from exc


def sync_packets(worker, repository_id, remote_url):
    from .agent_sync import PUSH_NAME
    from .agent_receiver import receive
    from .local_config import repository_path
    from .publication import decode
    from .rc_transport import TransportSession
    if len(repository_id) != 32 or any(c not in '0123456789abcdef' for c in repository_id):
        raise GdiError('invalid repository ID')
    route = repository_path(worker.config['remote_url'], remote_url)
    with worker.inbox_lock, TransportSession(options=worker.config.get('transport')) as session:
        session.network.log_kind = 'worker'
        # receive uses no ledger or CI state. Keep its transport private to this thread.
        receiver = SimpleNamespace(config=worker.config, worker_id=worker.worker_id,
            root=worker.root, transport_factory=session)
        root = session(worker.config['remote_url'])
        def adopt(name, *, present):
            if worker.stop.is_set():
                raise GdiError('worker is stopping')
            if not worker.inbox_retry_ready(name):
                raise GdiError('incoming Git packet is waiting for retry backoff: ' + name)
            try:
                receive(receiver, root, name, present=present, expected_repository_path=route)
                worker.inbox_retries.pop(name, None)
            except (GdiError, OSError):
                worker.inbox_retry_failed(name)
                raise
        entries = root.list('inbox')
        from collections import Counter
        requests = Counter(match.groups()[:2] for entry in entries if not entry['IsDir']
                           for match in [PUSH_NAME.fullmatch(entry['Path'])] if match)
        if any(count > 1 and identity[0] == repository_id for identity, count in requests.items()):
            raise GdiError('conflicting packages for the same agent request ID')
        attempted = set()
        for path in sorted((worker.root / 'agent-pushes' / repository_id).glob('*/journal.json')):
            journal = decode(path.read_bytes())
            if journal.get('phase') != 'DONE':
                adopt(journal['name'], present=False)
                attempted.add(journal['name'])
        for entry in entries:
            name = entry['Path']
            match = PUSH_NAME.fullmatch(name)
            if (match and not entry['IsDir'] and match[1] == repository_id
                    and match[4] == worker.worker_id and name not in attempted):
                adopt(name, present=True)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True


def start(worker):
    path = directory() / (prefix(worker.config['remote_url']) + '-' + hashlib.sha256(worker.worker_id.encode()).hexdigest()[:12] + '.sock')
    if path.exists():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise GdiError('local worker control endpoint already active')
    active = {}
    active_lock = threading.Lock()
    def barrier(request):
        key = (request['repository_id'], request['remote_url'])
        if not all(isinstance(value, str) for value in key):
            raise GdiError('invalid synchronization identity')
        with active_lock:
            owner = key not in active
            if owner:
                active[key] = {'done': threading.Event(), 'result': None}
            work = active[key]
        if not owner:
            if not work['done'].wait(300):
                raise GdiError('local worker synchronization timed out')
            return work['result']
        try:
            sync_packets(worker, *key)
            work['result'] = {'ok': True}
        except (GdiError, OSError, ValueError, TypeError) as exc:
            work['result'] = {'ok': False, 'error': str(exc)}
        finally:
            with active_lock:
                work['done'].set()
                active.pop(key, None)
        return work['result']
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(300)
            try:
                raw = self.rfile.readline(65537)
                if len(raw) > 65536 or not raw.endswith(b'\n'):
                    raise GdiError('invalid synchronization request')
                request = json.loads(raw)
                if not isinstance(request, dict) or set(request) != {'repository_id', 'remote_url'}:
                    raise GdiError('invalid synchronization request')
                result = barrier(request)
            except (GdiError, OSError, ValueError, TypeError) as exc:
                result = {'ok': False, 'error': str(exc)}
            try:
                self.wfile.write(json.dumps(result).encode() + b'\n')
            except OSError:
                pass
    server = Server(str(path), Handler)
    path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name='gdi-worker-control')
    thread.start()
    return server, path


def close(control):
    server, path = control
    server.shutdown()
    server.server_close()
    path.unlink(missing_ok=True)
