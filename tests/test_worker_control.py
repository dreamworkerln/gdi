"""Local fetch barrier waits for publication and shares the timer inbox mutex."""

import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gdi.git import GdiError
from gdi.worker import Worker
from gdi import worker_control as control


class ControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-ctl-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.real_directory = control.directory
        context = patch.object(control, 'directory', return_value=self.root)
        context.start()
        self.addCleanup(context.stop)
        self.worker = SimpleNamespace(config={'remote_url': 'test:hub'}, worker_id='worker',
                                      root=self.root, inbox_lock=threading.RLock(),
                                      stop=threading.Event())
        self.settings = {'inbox_root': 'test:hub', 'url': 'test:hub/project', 'repository_id': 'a' * 32}

    def test_absent_worker_uses_no_network(self):
        with patch('gdi.rc_transport.TransportSession', side_effect=AssertionError('no Drive')):
            control.synchronize(self.settings)

    def test_waits_for_worker_and_surfaces_failure(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        def sync(*args):
            entered.set()
            if not release.wait(3):
                raise GdiError('test release timeout')
        with patch.object(control, 'sync_packets', side_effect=sync) as synchronize:
            server = control.start(self.worker)
            try:
                errors = []
                def client():
                    try:
                        control.synchronize(self.settings)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        completed.set()
                thread = threading.Thread(target=client)
                thread.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(completed.is_set())
                release.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                synchronize.side_effect = GdiError('Drive unavailable')
                with self.assertRaisesRegex(GdiError, 'Drive unavailable'):
                    control.synchronize(self.settings)
            finally:
                release.set()
                control.close(server)

    def test_socket_identity_is_bounded_for_long_worker_id(self):
        self.worker.worker_id = 'w' * 63
        server = control.start(self.worker)
        try:
            self.assertLess(len(os.fsencode(server[1])), 108)
            self.assertEqual(server[1].stat().st_mode & 0o777, 0o600)
        finally:
            control.close(server)

    def test_timer_and_local_request_never_access_inbox_concurrently(self):
        worker = Worker.__new__(Worker)
        worker.inbox_lock = threading.RLock()
        worker.config = self.worker.config
        worker.root = self.root
        worker.stop = threading.Event()
        worker.worker_id = 'worker'
        worker.inbox_retry_ready = Mock(return_value=True)
        worker.inbox_retry_failed = Mock()
        worker.inbox_retries = {}
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        def timer_scan():
            entered.set()
            release.wait(3)
        worker._discover_inbox = timer_scan
        root = Mock()
        root.list.return_value = []
        class Session:
            network = SimpleNamespace()
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                attempted.set()
                return self
            def __exit__(self, *args):
                pass
            def __call__(self, url):
                return root
        errors = []
        def local():
            try:
                control.sync_packets(worker, 'a' * 32, 'test:hub/project')
            except Exception as exc:
                errors.append(exc)
        with patch('gdi.rc_transport.TransportSession', Session):
            timer = threading.Thread(target=worker.discover_inbox)
            request = threading.Thread(target=local)
            timer.start()
            self.assertTrue(entered.wait(2))
            request.start()
            try:
                self.assertFalse(attempted.wait(.05))
                root.list.assert_not_called()
            finally:
                release.set()
                timer.join(3)
                request.join(3)
        self.assertEqual(errors, [])
        root.list.assert_called_once_with('inbox')

    def test_concurrent_callers_share_one_completed_scan(self):
        entered, release, joined = threading.Event(), threading.Event(), threading.Event()
        original_wait = threading.Event.wait
        def wait(event, timeout=None):
            if timeout == 300:
                joined.set()
            return original_wait(event, timeout)
        def sync(*args):
            entered.set()
            if not release.wait(3):
                raise GdiError('test release timeout')
        errors = []
        def client():
            try:
                control.synchronize(self.settings)
            except Exception as exc:
                errors.append(exc)
        with patch.object(control, 'sync_packets', side_effect=sync) as scan, \
                patch.object(threading.Event, 'wait', wait):
            server = control.start(self.worker)
            clients = [threading.Thread(target=client) for _ in range(2)]
            try:
                clients[0].start()
                self.assertTrue(entered.wait(2))
                clients[1].start()
                self.assertTrue(joined.wait(2), 'second handler must join the active barrier')
                scan.assert_called_once()
            finally:
                release.set()
                for thread in clients:
                    if thread.ident is not None:
                        thread.join(3)
                control.close(server)
        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in clients))
        scan.assert_called_once()

    def test_client_timeout_is_an_error_and_shutdown_waits_for_active_work(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def sync(*args):
            entered.set()
            release.wait(3)
        with patch.object(control, 'sync_packets', side_effect=sync):
            server = control.start(self.worker)
            with self.assertRaisesRegex(GdiError, 'synchronization failed'):
                control.synchronize(self.settings, timeout=.05)
            self.assertTrue(entered.is_set())
            def stop():
                control.close(server)
                stopped.set()
            thread = threading.Thread(target=stop)
            thread.start()
            try:
                self.assertFalse(stopped.wait(.1))
            finally:
                release.set()
                thread.join(3)
            self.assertTrue(stopped.is_set())
            self.assertFalse(server[1].exists())

    def test_multiple_workers_are_awaited_and_error_is_not_ignored(self):
        another = SimpleNamespace(config=self.worker.config, worker_id='second')
        with patch.object(control, 'sync_packets') as sync:
            first = control.start(self.worker)
            second = control.start(another)
            try:
                control.synchronize(self.settings)
                self.assertEqual(sync.call_count, 2)
                sync.side_effect = GdiError('worker failed')
                with self.assertRaisesRegex(GdiError, 'worker failed'):
                    control.synchronize(self.settings)
            finally:
                control.close(first)
                control.close(second)

    def test_stale_socket_is_skipped_and_live_endpoint_still_used(self):
        import socket
        stale = self.root / (control.prefix('test:hub') + '-000.sock')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(stale))
        sock.close()
        with patch.object(control, 'sync_packets') as sync:
            server = control.start(self.worker)
            try:
                control.synchronize(self.settings)
                sync.assert_called_once()
            finally:
                control.close(server)

    def test_unsafe_control_directory_is_rejected(self):
        # Exercise the real path selector, not the fixture's patched directory().
        self.root.chmod(0o755)
        actual = Path(self.root) / ('gdi-control-' + str(os.getuid()))
        actual.mkdir(mode=0o755)
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.root)}):
            with self.assertRaisesRegex(GdiError, 'unsafe'):
                self.real_directory()

    def test_malformed_request_never_starts_drive_work(self):
        import json
        import socket
        with patch.object(control, 'sync_packets') as sync:
            server = control.start(self.worker)
            try:
                for payload in (b'[]\n', b'{bad}\n', b'{"repository_id":42,"remote_url":[]}\n'):
                    with self.subTest(payload=payload), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.settimeout(2)
                        connection.connect(str(server[1]))
                        connection.sendall(payload)
                        with connection.makefile('rb') as stream:
                            response = json.loads(stream.readline())
                        self.assertIs(response['ok'], False)
                sync.assert_not_called()
            finally:
                control.close(server)

    def test_invalid_worker_response_fails_instead_of_reading_stale_remote(self):
        import socketserver
        for payload in (b'[]\n', b'not-json\n', b'', b'x' * 65537):
            with self.subTest(payload=payload[:20]):
                class Handler(socketserver.StreamRequestHandler):
                    def handle(self):
                        self.rfile.readline(65537)
                        try:
                            self.wfile.write(payload)
                        except OSError:
                            pass
                path = self.root / (control.prefix('test:hub') + '-response.sock')
                server = control.Server(str(path), Handler)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    with self.assertRaises(GdiError):
                        control.synchronize(self.settings)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(2)
                    path.unlink(missing_ok=True)

    def test_run_manages_control_lifetime_only_for_continuous_shared_worker(self):
        for shared, once in ((True, False), (True, True), (False, False)):
            with self.subTest(shared=shared, once=once):
                worker = Worker.__new__(Worker)
                worker.config = {'poll_active_seconds': 60, 'poll_idle_max_seconds': 60}
                worker.shared = shared
                worker.stop = Mock()
                worker.stop.is_set.side_effect = [False, True]
                worker.advertise = Mock()
                worker.tick = Mock(return_value=False)
                with patch.object(control, 'start', return_value='endpoint') as start, \
                        patch.object(control, 'close') as close, \
                        patch('gdi.worker.threading.current_thread', return_value=threading.Thread(name='test-run')):
                    worker.run(once=once)
                if shared and not once:
                    start.assert_called_once_with(worker)
                    close.assert_called_once_with('endpoint')
                else:
                    start.assert_not_called()
                    close.assert_not_called()
