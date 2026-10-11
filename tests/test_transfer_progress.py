import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from gdi.diagnostics import command_session, TransferProgress
from gdi.git import GdiError
from gdi.rc_transport import RcServer
from gdi.transport import Rclone


class ProgressTests(unittest.TestCase):
    def test_render_without_profile_and_keep_stdout_clean(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), command_session('push', progress=True):
            TransferProgress('upload source.bundle').update({'bytes': 50, 'totalBytes': 100, 'speed': 25}, final=True)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('50/100 bytes (50.0%), 25 bytes/s', err.getvalue())
        self.assertNotIn('total ', err.getvalue())
        self.assertNotIn('profile', err.getvalue())

    def test_all_bytes_wait_once_until_job_succeeds(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), command_session('push', progress=True), \
                patch('gdi.diagnostics.time.monotonic', return_value=0) as clock:
            progress = TransferProgress('upload source.bundle', 100)
            clock.return_value = 1
            progress.update({'bytes': 100, 'speed': 100})
            for second in range(2, 20):
                clock.return_value = second
                progress.update({'bytes': 100, 'speed': 100 / second})
            self.assertEqual(err.getvalue().splitlines(), [
                'gdi: upload source.bundle: 100/100 bytes (100.0%); waiting for transfer completion'])
            progress.update({'bytes': 100, 'speed': 5}, final=True)
        self.assertEqual(len(err.getvalue().splitlines()), 2)
        self.assertTrue(err.getvalue().endswith('5 bytes/s; complete\n'))

    def test_failure_after_all_bytes_does_not_report_complete(self):
        server = RcServer()
        responses = [{'jobid': 7}, {'finished': False}, {'bytes': 100, 'speed': 100},
                     {'finished': True, 'success': False, 'error': 'failed transfer'},
                     {'bytes': 100, 'speed': 50}, {}]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), command_session('push', progress=True), \
                patch.object(server, '_request', side_effect=responses), \
                patch('gdi.rc_transport.time.sleep'), \
                patch('gdi.diagnostics.time.monotonic', side_effect=range(100)):
            with self.assertRaisesRegex(GdiError, 'failed transfer'):
                server.transfer('/sync/copy', {}, 'upload', 100)
        self.assertIn('waiting for transfer completion', err.getvalue())
        self.assertNotIn('; complete', err.getvalue())

    def test_disabled_progress_makes_no_async_calls(self):
        with command_session('push', progress=False), patch.object(RcServer, 'request', return_value={}) as request:
            transport = RcServer().transport('test:project')
            source = Path(__file__)
            transport.upload(source, 'bundles/source.bundle')
            self.assertEqual(request.call_count, 1)
            self.assertNotIn('_async', request.call_args.args[1])

    def test_async_progress_failure_stops_job_and_all_calls_are_profiled(self):
        server = RcServer()
        responses = [{'jobid': 7}, {'finished': True, 'success': False, 'error': 'failed transfer'},
                     {'bytes': 12, 'totalBytes': 100, 'speed': 3}, {}]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'profile.jsonl'
            with contextlib.redirect_stderr(io.StringIO()), command_session('push', path=log, progress=True):
                with patch.object(server, '_request', side_effect=responses):
                    with self.assertRaisesRegex(GdiError, 'failed transfer'):
                        server.transfer('/sync/copy', {}, 'upload', 100)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            starts = [item for item in events if item['event'] == 'rc_start']
            self.assertEqual([item['operation'] for item in starts], ['sync/copy', 'job/status', 'core/stats', 'job/stop'])
            self.assertEqual(events[-1]['rc_calls'], 4)

    @unittest.skipUnless(shutil.which('rclone'), 'rclone required')
    def test_real_cli_stats_stream_has_bytes_and_does_not_change_payload(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, RCLONE_CONFIG_GDIPROGRESS_TYPE='local'):
            root = Path(directory); source = root / 'source'
            source.write_bytes(b'\x00\xff' * 32768)
            err = io.StringIO()
            transport = Rclone('gdiprogress:' + str(root / 'remote'))
            with contextlib.redirect_stderr(err), command_session('push', progress=True):
                transport.call('copyto', str(source), transport.path('destination'), '--bwlimit', '64k')
            self.assertEqual((root / 'remote/destination').read_bytes(), source.read_bytes())
            self.assertIn('65536/65536 bytes (100.0%)', err.getvalue())
            self.assertIn('bytes/s', err.getvalue())

    @unittest.skipUnless(os.sys.platform.startswith('linux') and shutil.which('rclone'), 'Linux/rclone required')
    def test_native_rc_upload_download_progress_and_immutable_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, RCLONE_CONFIG_GDIPROGRESS_TYPE='local', RCLONE_CONFIG=os.devnull):
            root = Path(directory); source = root / 'source'
            source.write_bytes(b'\x00\xff' * 32768)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    command_session('push', progress=True), RcServer() as server:
                transport = server.transport('gdiprogress:' + str(root / 'remote'))
                transport.upload(source, 'bundles/source.bundle')
                transport.download('bundles/source.bundle', root / 'received')
                self.assertEqual((root / 'received').read_bytes(), source.read_bytes())
                source.write_bytes(b'changed')
                with self.assertRaisesRegex(GdiError, 'immutable'):
                    transport.upload(source, 'bundles/source.bundle')
                routes = [event['route'] for event in server.events if event['event'] == 'rc_start']
                self.assertIn('/job/status', routes)
                self.assertIn('/core/stats', routes)
            self.assertEqual(out.getvalue(), '')
            self.assertIn('65536/65536 bytes (100.0%)', err.getvalue())
            self.assertIn('bytes/s', err.getvalue())
            self.assertNotIn('rclone finished', err.getvalue())
