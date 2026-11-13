"""Private diagnostics survive process exit; failed inbox work waits before retry."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from gdi import rclone_policy
from gdi.git import GdiError
from gdi.rc_transport import RcServer
from gdi.transport import Rclone
from gdi.worker import Worker


class RclonePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.handlers = []
        original = rclone_policy.PrivateRotatingFileHandler
        def handler(*args, **kwargs):
            value = original(*args, **kwargs)
            self.handlers.append(value)
            return value
        for context in (patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root)}),
                        patch.object(rclone_policy, '_LOGS', {}),
                        patch.object(rclone_policy, 'PrivateRotatingFileHandler', side_effect=handler)):
            context.start()
            self.addCleanup(context.stop)
        self.addCleanup(lambda: [value.close() for value in self.handlers])

    def test_worker_rotation_keeps_private_files_and_recent_diagnostics(self):
        path, write = rclone_policy.diagnostic_log('worker')
        self.handlers[0].maxBytes = 180
        for index in range(30):
            write(f'operation {index:03d} ' + 'x' * 80)
        files = list(path.parent.glob('worker-rclone.log*'))
        self.assertEqual(len(files), 5)
        self.assertIn('operation 029', path.read_text())
        self.assertNotIn('operation 000', ''.join(p.read_text() for p in files))
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        for file in files:
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_cli_retention_only_removes_old_cli_logs_and_redacts_proxy(self):
        directory = self.root / 'gdi/logs'
        directory.mkdir(parents=True)
        old = directory / 'cli-rclone-old-123.log'
        old.write_text('old')
        os.utime(old, (0, 0))
        unrelated = directory / 'notes.txt'
        unrelated.write_text('keep')
        worker = directory / 'worker-rclone.log'
        worker.write_text('keep worker')
        path, write = rclone_policy.diagnostic_log()
        write('proxy http://owner:secret@localhost:1234 failed')
        self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertEqual(worker.read_text(), 'keep worker')
        self.assertNotIn('secret', path.read_text())
        self.assertIn('[redacted]@localhost', path.read_text())

    def test_cli_passes_pacing_and_saves_error_output_with_log_location(self):
        network = Mock()
        network.log_kind = 'cli'
        network.run.side_effect = lambda operation: operation({})
        network.environment.return_value = {}
        def failed(args, **kwargs):
            self.assertEqual(args[args.index('--tpslimit') + 1], '1')
            self.assertEqual(args[args.index('--tpslimit-burst') + 1], '1')
            kwargs['stderr_line']('rate limit diagnostic\n')
            raise GdiError('rclone failed')
        with patch('gdi.transport.run', side_effect=failed):
            with self.assertRaisesRegex(GdiError, 'rclone log:'):
                Rclone('test:folder', network=network).list('inbox')
        path, _ = rclone_policy.diagnostic_log()
        self.assertIn('rate limit diagnostic', path.read_text())
        self.assertIn('operation finish:', path.read_text())

    def test_rc_startup_failure_persists_stderr_and_passes_pacing(self):
        executable = self.root / 'rclone'
        arguments = self.root / 'arguments.json'
        executable.write_text('#!' + sys.executable + '\nimport sys,json\n'
                              + f'open({str(arguments)!r}, "w").write(json.dumps(sys.argv))\n'
                              + 'sys.stderr.write("startup failed marker\\n")\nsys.exit(1)\n')
        executable.chmod(0o700)
        network = Mock()
        network.log_kind = 'worker'
        network.routes.return_value = [{}]
        network.available.return_value = True
        network.environment.return_value = dict(os.environ)
        with self.assertRaisesRegex(GdiError, 'rclone log:'):
            with RcServer(executable=str(executable), network=network):
                self.fail('startup must fail')
        args = json.loads(arguments.read_text())
        self.assertEqual(args[args.index('--tpslimit') + 1], '1')
        self.assertEqual(args[args.index('--tpslimit-burst') + 1], '1')
        path, _ = rclone_policy.diagnostic_log('worker')
        self.assertIn('startup failed marker', path.read_text())

    def test_recovery_failure_is_not_retried_by_inbox_in_same_cycle(self):
        name = 'agent-push-' + 'a' * 32 + '-' + 'b' * 32 + '-' + 'c' * 64 + '-worker.zip'
        worker = Worker.__new__(Worker)
        worker.root = self.root
        worker.config = {'remote_url': 'test:hub'}
        worker.worker_id = 'worker'
        worker.stop = threading.Event()
        worker.inbox_retries = {}
        worker.inbox_lock = threading.RLock()
        root = Mock()
        root.list.return_value = [{'Path': name, 'IsDir': False}]
        worker.transport_factory = Mock(return_value=root)
        journal = self.root / 'agent-pushes/repository/request/journal.json'
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps({'name': name, 'phase': 'PREPARED'}))
        with patch('gdi.agent_receiver.receive', side_effect=GdiError('network failure')) as receive, \
                patch('gdi.worker.time.monotonic', return_value=100):
            worker.discover_inbox()
            worker.discover_inbox()
            self.assertEqual(receive.call_count, 1)
        with patch('gdi.agent_receiver.receive', side_effect=GdiError('network failure')) as receive, \
                patch('gdi.worker.time.monotonic', return_value=220):
            worker.discover_inbox()
            self.assertEqual(receive.call_count, 1)
        self.assertEqual(worker.inbox_retries[name], (240, 460))
        self.assertTrue(journal.exists())

    def test_cycle_errors_back_off_even_when_idle_cap_equals_poll_interval(self):
        worker = Worker.__new__(Worker)
        worker.config = {'poll_active_seconds': 60, 'poll_idle_max_seconds': 60}
        worker.stop = Mock()
        worker.stop.is_set.side_effect = [False] * 5 + [True]
        worker.shared = False
        worker.advertise = Mock()
        worker.tick = Mock(side_effect=GdiError('connection timeout'))
        worker.recover_delivery = Mock()
        with patch('gdi.worker.threading.current_thread', return_value=threading.Thread(name='test-worker')):
            worker.run()
        self.assertEqual([call.args[0] for call in worker.stop.wait.call_args_list],
                         [120, 240, 480, 900, 900])

    def test_retention_keeps_an_active_cli_log(self):
        path, write = rclone_policy.diagnostic_log()
        write('active operation')
        os.utime(path, (0, 0))
        # Simulate another CLI process initializing while this handler is open.
        with patch.object(rclone_policy, '_LOGS', {}):
            rclone_policy.diagnostic_log()
        self.assertTrue(path.exists())
        self.assertIn('active operation', path.read_text())
