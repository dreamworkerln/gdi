"""Real packet adoption behind the local barrier, using an in-memory Drive."""

import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gdi import packet
from gdi.agent_receiver import receive
from gdi.agent_sync import receipt_path
from gdi.cli import execute, parser
from gdi.git import GdiError
from gdi.publication import decode, encode
from gdi.worker_control import sync_packets
from tests import test_packet_v4 as fixtures


class PacketBarrierTests(unittest.TestCase):
    setUp = fixtures.PacketV4Tests.setUp
    repo = fixtures.PacketV4Tests.repo
    commit = fixtures.PacketV4Tests.commit
    publish = fixtures.PacketV4Tests.publish
    inputs = fixtures.PacketV4Tests.inputs
    agent = fixtures.PacketV4Tests.agent
    worker = fixtures.PacketV4Tests.worker
    request = fixtures.PacketV4Tests.request

    def session(self):
        factory = self.factory
        class Session:
            def __init__(self, **kwargs):
                self.network = SimpleNamespace()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __call__(self, url):
                return factory(url)
        return patch('gdi.rc_transport.TransportSession', Session)

    def sync(self, worker, url='memory:hub/project'):
        with self.session():
            sync_packets(worker, self.identity, url)

    def test_packet_and_receipt_are_published_without_ci_or_second_upload(self):
        _, _, plan, head = self.request()
        worker = self.worker()
        self.calls.clear()
        with patch.object(worker, 'process', side_effect=AssertionError('no CI')):
            self.sync(worker)
        pointer = decode(self.store.data['project/heads/main.json'])
        self.assertEqual(pointer['metadata']['head'], head)
        receipt = decode(self.store.data['project/' + receipt_path(self.identity, plan['request_id'])])
        self.assertEqual(receipt['state'], 'PUBLISHED')
        self.assertEqual(sum(verb == 'move' for verb, _ in self.calls), 1)
        self.assertFalse(any(verb == 'upload' and path.endswith('.zip') for verb, path in self.calls))
        self.assertNotIn('inbox/' + plan['files']['package']['target_name'], self.store.data)

    def test_other_repositories_workers_and_ci_entries_are_not_consumed(self):
        _, _, plan, _ = self.request()
        name = plan['files']['package']['target_name']
        raw = self.store.data.pop('inbox/' + name)
        foreign_repo = name.replace(self.identity, 'f' * 32, 1)
        foreign_worker = name.removesuffix('-user-host.zip') + '-another.zip'
        for foreign in (foreign_repo, foreign_worker):
            self.store.data['inbox/' + foreign] = raw
        ci_name = 'inbox/' + 'd' * 32 + '-' + 'e' * 64 + '.json'
        self.store.data[ci_name] = b'CI must not even be read'
        self.calls.clear()
        self.sync(self.worker())
        self.assertEqual(self.calls, [('list', 'inbox')])
        self.assertIn(ci_name, self.store.data)

    def test_recovery_after_move_completes_pointer_and_receipt(self):
        _, _, plan, head = self.request()
        worker = self.worker()
        self.failure = 'head'
        with self.assertRaises(GdiError):
            receive(worker, self.factory('memory:hub'), plan['files']['package']['target_name'])
        self.failure = None
        self.calls.clear()
        self.sync(worker)
        self.assertEqual(decode(self.store.data['project/heads/main.json'])['metadata']['head'], head)
        self.assertIn('project/' + receipt_path(self.identity, plan['request_id']), self.store.data)
        self.assertFalse(any(verb in ('move', 'download') for verb, _ in self.calls))

    def test_failure_and_backoff_prevent_false_success_and_preserve_packet(self):
        _, _, plan, _ = self.request()
        worker = self.worker()
        self.failure = 'head'
        with self.assertRaises(GdiError):
            self.sync(worker)
        self.failure = None
        self.calls.clear()
        with self.assertRaisesRegex(GdiError, 'backoff'):
            self.sync(worker)
        self.assertEqual(self.calls, [('list', 'inbox')])
        name = plan['files']['package']['target_name']
        with patch('gdi.worker.time.monotonic', return_value=worker.inbox_retries[name][1]):
            self.sync(worker)
        self.assertNotIn(name, worker.inbox_retries)
        self.assertIn('project/' + receipt_path(self.identity, plan['request_id']), self.store.data)

    def test_conflicting_packages_are_rejected_before_mutation(self):
        _, _, plan, _ = self.request()
        name = plan['files']['package']['target_name']
        identity = plan['files']['package']['sha256']
        alternate = name.replace(identity, 'f' * 64)
        self.store.data['inbox/' + alternate] = self.store.data['inbox/' + name]
        worker = self.worker()
        self.calls.clear()
        with self.assertRaisesRegex(GdiError, 'conflicting packages'):
            self.sync(worker)
        self.assertEqual(self.calls, [('list', 'inbox')])

    def test_wrong_remote_path_cannot_publish_into_another_repository(self):
        _, _, plan, _ = self.request()
        before = self.store.data['project/heads/main.json']
        worker = self.worker()
        self.calls.clear()
        with self.assertRaisesRegex(GdiError, 'route differs'):
            self.sync(worker, 'memory:hub/other')
        self.assertEqual(self.store.data['project/heads/main.json'], before)
        self.assertIn('inbox/' + plan['files']['package']['target_name'], self.store.data)
        self.assertFalse(any(verb in ('move', 'head', 'upload') for verb, _ in self.calls))

    def test_fetch_waits_before_git_lock_and_reads_new_pointer(self):
        _, _, _, head = self.request()
        worker = self.worker()
        def barrier(settings):
            # Taking this lock proves CLI has not acquired it before waiting.
            with self.a.lock():
                self.sync(worker)
        with patch('gdi.cli.Git.discover', return_value=self.a), \
                patch('gdi.worker_control.synchronize', side_effect=barrier) as synchronize, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute(parser().parse_args(['fetch', 'drive']), transport_factory=self.factory), 0)
        synchronize.assert_called_once()
        self.assertEqual(self.a.oid('refs/remotes/drive/main'), head)

    def test_pull_waits_before_git_lock_and_updates_checkout(self):
        _, _, _, head = self.request()
        worker = self.worker()
        def barrier(settings):
            with self.a.lock():
                self.sync(worker)
        with patch('gdi.cli.Git.discover', return_value=self.a), \
                patch('gdi.worker_control.synchronize', side_effect=barrier), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute(parser().parse_args(['pull', 'drive']), transport_factory=self.factory), 0)
        self.assertEqual(self.a.oid('HEAD'), head)

    def test_barrier_error_prevents_fetch_and_pull_remote_reads(self):
        self.publish()
        for command in ('fetch', 'pull'):
            with self.subTest(command=command):
                self.calls.clear()
                with patch('gdi.cli.Git.discover', return_value=self.a), \
                        patch('gdi.worker_control.synchronize', side_effect=GdiError('barrier failed')), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(execute(parser().parse_args([command, 'drive']), transport_factory=self.factory), 1)
                self.assertEqual(self.calls, [])

    def test_operator_push_never_contacts_control_socket(self):
        self.publish()
        self.commit(self.a, 'operator next')
        with patch('gdi.cli.Git.discover', return_value=self.a), \
                patch('gdi.worker_control.synchronize', side_effect=AssertionError('push must not wake worker')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute(parser().parse_args(['push', 'drive']), transport_factory=self.factory), 0)

    def test_local_adoption_blocks_timer_and_packet_is_moved_only_once(self):
        import threading
        self.request()
        worker = self.worker()
        entered, release, timer_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def adopt(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise GdiError('test release timeout')
            return receive(*args, **kwargs)
        def local():
            try:
                self.sync(worker)
            except Exception as exc:
                errors.append(exc)
        def timer():
            try:
                worker.discover_inbox()
            except Exception as exc:
                errors.append(exc)
            finally:
                timer_done.set()
        self.calls.clear()
        with patch('gdi.agent_receiver.receive', side_effect=adopt) as adoption:
            local_thread = threading.Thread(target=local)
            timer_thread = threading.Thread(target=timer)
            local_thread.start()
            self.assertTrue(entered.wait(2))
            timer_thread.start()
            try:
                self.assertFalse(timer_done.wait(.05))
            finally:
                release.set()
                local_thread.join(3)
                timer_thread.join(3)
            self.assertFalse(local_thread.is_alive())
            self.assertFalse(timer_thread.is_alive())
            adoption.assert_called_once()
        self.assertEqual(errors, [])
        self.assertEqual(sum(verb == 'move' for verb, _ in self.calls), 1)

    def test_barrier_completes_while_tick_is_running_a_ci_job(self):
        import threading
        _, _, _, head = self.request()
        worker = self.worker()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def ci_job(row):
            entered.set()
            release.wait(3)
        def tick():
            try:
                worker.tick()
            except Exception as exc:
                errors.append(exc)
        with patch.object(worker, 'discover'), \
                patch.object(worker.ledger, 'pending', side_effect=[[{'job_id': 'job'}], []]), \
                patch.object(worker, 'heartbeat'), patch.object(worker, 'process', side_effect=ci_job):
            thread = threading.Thread(target=tick)
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                self.sync(worker)
                self.assertTrue(thread.is_alive(), 'barrier must not wait for CI completion')
                self.assertEqual(decode(self.store.data['project/heads/main.json'])['metadata']['head'], head)
            finally:
                release.set()
                thread.join(3)
        self.assertEqual(errors, [])
