"""Delivery recovery, fresh batched metadata and complete profiling records."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gdi.diagnostics import command_session
from gdi.exchange import digest
from gdi.git import GdiError, run
from gdi.transport import Rclone
from tests.test_exchange import ExchangeTestCase


class PublicationDeliveryTests(ExchangeTestCase):
    def setUp(self):
        super().setUp()
        self.ea.remove('drive')
        self.ea.add('drive', 'memory:hub/project', expected_id=self.identity)

    def receipts(self):
        return list((self.a.gdi_dir() / 'notifications').glob('*.json'))

    def test_consumed_notification_is_not_recreated_on_unchanged_push(self):
        head, publication, _ = self.ea.push('drive')
        event = next(path for path in self.store.data if path.startswith('inbox/'))
        del self.store.data[event]
        before = list(self.store.uploads)
        self.assertEqual(self.ea.push('drive'), (head, publication, False))
        self.assertEqual(self.store.uploads, before)
        self.assertNotIn(event, self.store.data)
        self.assertEqual(len(self.receipts()), 1)
        # A receipt cannot bypass the fresh remote identity check.
        self.store.data['repository.json'] = self.store.data['repository.json'].replace(
            self.identity.encode(), b'0' * 32)
        with self.assertRaisesRegex(GdiError, 'repository ID mismatch'):
            self.ea.push('drive')

    def test_failed_delivery_is_retried_without_another_publication(self):
        self.store.fail = 'inbox/'
        with self.assertRaisesRegex(GdiError, 'network'):
            self.ea.push('drive')
        self.assertEqual(self.receipts(), [])
        publications = {path for path in self.store.data if path.startswith('branches/')}
        self.store.fail = None
        self.assertFalse(self.ea.push('drive')[2])
        self.assertEqual({path for path in self.store.data if path.startswith('branches/')}, publications)
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(sum(path.startswith('inbox/') for path in self.store.data), 1)

    def test_crash_after_upload_before_receipt_retries_identical_event(self):
        with patch('gdi.ci_protocol.atomic_write', side_effect=OSError('receipt fsync failed')):
            with self.assertRaisesRegex(OSError, 'fsync'):
                self.ea.push('drive')
        event = next(path for path in self.store.data if path.startswith('inbox/'))
        raw = self.store.data[event]
        self.assertEqual(self.receipts(), [])
        self.assertFalse(self.ea.push('drive')[2])
        self.assertEqual(self.store.uploads.count(event), 2)
        self.assertEqual(self.store.data[event], raw)

    def test_corruption_during_preupload_recheck_is_still_detected(self):
        self.ea.push('drive')
        self.commit(self.a, 'new commit')
        before = list(self.store.uploads)
        calls = []

        def read_many(paths):
            paths = list(paths)
            calls.append(paths)
            return {path: self.store.read(path) + (b' ' if len(calls) == 2 else b'') for path in paths}

        with patch.object(self.store, 'read_many', side_effect=read_many, create=True):
            with self.assertRaisesRegex(GdiError, 'metadata checksum'):
                self.ea.push('drive')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.uploads, before)


@unittest.skipUnless(shutil.which('rclone'), 'rclone required for local transport checks')
class RcloneBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-batch-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / 'remote'
        self.transport = Rclone('gdibatch:' + str(self.remote))
        environment = patch.dict(os.environ, RCLONE_CONFIG_GDIBATCH_TYPE='local', RCLONE_CONFIG=os.devnull)
        environment.start()
        self.addCleanup(environment.stop)

    def metadata(self, raw):
        relative = 'branches/main' + '/' + digest(raw) + '.json'
        path = self.remote / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return relative

    def test_fresh_snapshot_downloads_multiple_files_in_one_process(self):
        first = self.metadata(b'{"first":1}\n')
        second = self.metadata(b'{"second":2}\n')
        with patch.object(self.transport, 'call', wraps=self.transport.call) as call:
            self.assertEqual(self.transport.read_many([first, second]),
                             {first: b'{"first":1}\n', second: b'{"second":2}\n'})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], 'copy')
        (self.remote / first).write_bytes(b'changed remote bytes')
        self.assertEqual(self.transport.read_many([first, second])[first], b'changed remote bytes')

    def test_missing_or_oversized_metadata_cannot_produce_a_partial_snapshot(self):
        first = self.metadata(b'{"first":1}\n')
        second = self.metadata(b'x' * (1024 * 1024 + 1))
        with self.assertRaises(GdiError):
            self.transport.read_many([first, second])
        (self.remote / second).unlink()
        with self.assertRaises(GdiError):
            self.transport.read_many([first, second])

    def test_upload_creates_parents_and_preserves_immutability(self):
        source = self.root / 'source'
        source.write_bytes(b'original')
        relative = 'inbox/' + 'new-directory/' + 'event.json'
        self.transport.upload(source, relative)
        self.assertEqual((self.remote / relative).read_bytes(), b'original')
        # Equal size and mtime must not hide a changed immutable payload.
        original = source.stat()
        source.write_bytes(b'changed!')
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(GdiError):
            self.transport.upload(source, relative)
        self.assertEqual((self.remote / relative).read_bytes(), b'original')


class ProfilingTests(unittest.TestCase):
    def test_failed_call_is_logged_before_execution_without_private_output(self):
        with tempfile.TemporaryDirectory(prefix='gdi-profile-') as temporary:
            path = Path(temporary) / 'trace.jsonl'

            def fail(*args, **kwargs):
                events = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(events[-1]['event'], 'subprocess_start')
                return subprocess.CompletedProcess(args[0], 5, stdout='', stderr='private-token')

            with contextlib.redirect_stderr(io.StringIO()), command_session('push', path=path, progress=False):
                with patch('gdi.git.subprocess.run', side_effect=fail):
                    with self.assertRaises(GdiError):
                        run(['rclone', 'cat', 'remote:project/repository.json'])
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertNotIn('private-token', path.read_text())
            finish = next(event for event in events if event['event'] == 'subprocess_finish')
            self.assertEqual(finish['returncode'], 5)
            self.assertEqual(finish['error'], 'GdiError')
            self.assertEqual(events[-1]['subprocess_counts'], {'rclone': 1})

    def test_interrupt_leaves_complete_profile_and_restores_session(self):
        with tempfile.TemporaryDirectory(prefix='gdi-profile-') as temporary:
            path = Path(temporary) / 'trace.jsonl'
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    with command_session('push', path=path, progress=False):
                        with patch('gdi.git.subprocess.run', side_effect=KeyboardInterrupt):
                            run(['rclone', 'cat', 'remote:project/repository.json'])
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(events[-1]['event'], 'command_finish')
            self.assertEqual(events[-1]['error'], 'KeyboardInterrupt')
            self.assertEqual(events[-1]['subprocess_counts'], {'rclone': 1})
            self.assertEqual(next(event for event in events if event['event'] == 'subprocess_finish')['error'],
                             'KeyboardInterrupt')
