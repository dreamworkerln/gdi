"""Status and its alias wait for publication before reading remote pointers."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from gdi.cli import execute, main, parser
from gdi.git import GdiError
from tests import test_worker_control_packets as fixtures


class StatusBarrierTests(unittest.TestCase):
    setUp = fixtures.PacketBarrierTests.setUp
    repo = fixtures.PacketBarrierTests.repo
    commit = fixtures.PacketBarrierTests.commit
    publish = fixtures.PacketBarrierTests.publish
    inputs = fixtures.PacketBarrierTests.inputs
    agent = fixtures.PacketBarrierTests.agent
    worker = fixtures.PacketBarrierTests.worker
    request = fixtures.PacketBarrierTests.request
    session = fixtures.PacketBarrierTests.session
    sync = fixtures.PacketBarrierTests.sync

    def status(self, *arguments):
        output = io.StringIO()
        with patch('gdi.cli.Git.discover', return_value=self.a), contextlib.redirect_stdout(output):
            code = execute(parser().parse_args(['status', *arguments, '--json']), transport_factory=self.factory)
        return code, json.loads(output.getvalue())

    def test_waits_before_git_lock_and_reports_new_head_without_fetching_refs(self):
        _, _, _, head = self.request()
        worker = self.worker()
        before = self.a.snapshot()
        refs = self.a.call('show-ref').stdout
        def barrier(settings):
            with self.a.lock():
                self.sync(worker)
        with patch('gdi.worker_control.synchronize', side_effect=barrier) as synchronize:
            code, result = self.status('drive')
        self.assertEqual(code, 0)
        synchronize.assert_called_once()
        self.assertEqual(result['connections'][0]['published_head'], head)
        self.assertEqual(result['connections'][0]['state'], 'unknown')
        self.assertEqual(self.a.snapshot(), before)
        self.assertEqual(self.a.call('show-ref').stdout, refs)

    def test_default_alias_s_uses_the_same_barrier(self):
        _, _, _, head = self.request()
        worker = self.worker()
        aliases = self.root / 'aliases.ini'
        aliases.write_text('[alias]\ns = status\n')
        output = io.StringIO()
        with self.session(), patch('gdi.cli.Git.discover', return_value=self.a), \
                patch('gdi.aliases.config_path', return_value=aliases), \
                patch('gdi.worker_control.synchronize', side_effect=lambda settings: self.sync(worker)) as sync, \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(['--no-progress', 's', '--json']), 0)
        sync.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())['connections'][0]['published_head'], head)

    def test_explicit_remote_does_not_synchronize_other_connections(self):
        self.publish()
        self.ea.create('other', 'memory:hub/other', inbox_root='memory:hub')
        with patch('gdi.worker_control.synchronize') as sync:
            code, result = self.status('drive')
        self.assertEqual(code, 0)
        self.assertEqual([item['name'] for item in result['connections']], ['drive'])
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(sync.call_args.args[0]['url'], 'memory:hub/project')

    def test_all_connections_sync_before_any_remote_status_read(self):
        self.publish()
        self.ea.create('other', 'memory:hub/other', inbox_root='memory:hub')
        self.calls.clear()
        seen = []
        def barrier(settings):
            self.assertEqual(self.calls, [], 'all barriers must precede status reads')
            seen.append(settings['url'])
        with patch('gdi.worker_control.synchronize', side_effect=barrier):
            code, result = self.status()
        self.assertEqual(code, 0)
        self.assertEqual(seen, ['memory:hub/project', 'memory:hub/other'])
        self.assertEqual(len(result['connections']), 2)

    def test_duplicate_connection_names_share_one_barrier(self):
        self.publish()
        self.ea.add('alias', 'memory:hub/project', expected_id=self.identity, inbox_root='memory:hub')
        with patch('gdi.worker_control.synchronize') as sync:
            code, result = self.status()
        self.assertEqual(code, 0)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(len(result['connections']), 2)

    def test_error_backoff_or_timeout_prevents_remote_reads(self):
        self.publish()
        for error in ('backoff', 'Drive failed', 'synchronization timed out'):
            with self.subTest(error=error):
                self.calls.clear()
                before = self.a.snapshot()
                output = io.StringIO()
                with patch('gdi.worker_control.synchronize', side_effect=GdiError(error)), \
                        contextlib.redirect_stderr(output):
                    code, result = self.status('drive')
                self.assertEqual(code, 1)
                self.assertIn(error, result['error'])
                self.assertEqual(self.calls, [])
                self.assertEqual(self.a.snapshot(), before)

    def test_error_from_one_connection_does_not_report_partial_success(self):
        self.publish()
        self.ea.create('other', 'memory:hub/other', inbox_root='memory:hub')
        self.calls.clear()
        with patch('gdi.worker_control.synchronize', side_effect=[None, GdiError('second worker failed')]), \
                contextlib.redirect_stderr(io.StringIO()):
            code, result = self.status()
        self.assertEqual(code, 1)
        self.assertIn('second worker failed', result['error'])
        self.assertEqual(self.calls, [])

    def test_invalid_remote_is_rejected_before_sync(self):
        with patch('gdi.worker_control.synchronize') as sync, contextlib.redirect_stderr(io.StringIO()):
            code, result = self.status('missing')
        self.assertEqual(code, 1)
        sync.assert_not_called()

    def test_no_connections_needs_no_worker(self):
        self.ea.remove('drive')
        with patch('gdi.worker_control.synchronize') as sync:
            code, result = self.status()
        self.assertEqual(code, 0)
        self.assertEqual(result['connections'], [])
        sync.assert_not_called()
