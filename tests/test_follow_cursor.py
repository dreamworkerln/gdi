import io
import sys
from unittest.mock import patch

from gdi.ci import CiClient
from gdi.exchange import decode, encode
from gdi.git import GdiError
from tests.ci_packages import package_path
from tests.test_ci import CiTestCase


class FollowCursorTests(CiTestCase):
    def live(self, config=None):
        worker = self.worker(config)
        client, req = self.submit(worker)
        return worker, client, req

    def test_first_follow_poll_reuses_request_but_next_poll_rechecks_ready(self):
        _, client, req = self.live()
        prefix = 'ci/jobs/' + req['job_id']
        def tamper_after_first_poll(_):
            paths = [call.args[0] for call in reads.call_args_list]
            self.assertEqual(paths.count(prefix + '/request.json'), 1)
            self.assertEqual(paths.count(prefix + '/request.ready'), 1)
            self.store.data[prefix + '/request.ready'] = b'{}'
        with patch.object(self.store, 'read', wraps=self.store.read) as reads:
            with patch('gdi.ci.time.sleep', side_effect=tamper_after_first_poll):
                with self.assertRaisesRegex(GdiError, 'ready/request'):
                    client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=10)
            paths = [call.args[0] for call in reads.call_args_list]
            self.assertEqual(paths.count(prefix + '/request.json'), 2)
            self.assertEqual(paths.count(prefix + '/request.ready'), 2)

    def test_pending_follow_reads_status_without_chunks_or_partial_log(self):
        worker, client, req = self.live()
        worker.discover()
        worker.status(worker.ledger.get(req['job_id']), 'RUNNING', 'test')
        output = io.StringIO()
        with patch.object(self.store, 'list', wraps=self.store.list) as listings:
            self.assertEqual(client.wait(req['job_id'], follow=True, stream=output,
                                         timeout=.01, interval=.005)[1], 124)
        self.assertFalse(any('log-chunks' in call.args[0] for call in listings.call_args_list))
        self.assertNotIn('hello CI', output.getvalue())

    def test_timeout_then_complete_log_resume_and_explicit_restart(self):
        worker, client, req = self.live()
        self.assertEqual(client.wait(req['job_id'], follow=True, stream=io.StringIO(),
                                     timeout=.01, interval=.005)[1], 124)
        worker.tick()
        output = io.StringIO()
        self.assertEqual(client.wait(req['job_id'], follow=True, stream=output)[1], 0)
        self.assertEqual(output.getvalue().count('hello CI'), 1)
        resumed = CiClient(self.ea, 'drive')
        output = io.StringIO()
        resumed.wait(req['job_id'], follow=True, stream=output)
        self.assertNotIn('hello CI', output.getvalue())
        replay = io.StringIO()
        resumed.wait(req['job_id'], follow=True, stream=replay, restart=True)
        self.assertEqual(replay.getvalue().count('hello CI'), 1)
        self.assertFalse(any('/log-chunks/' in path or '/events/' in path for path in self.store.data))

    def test_changed_log_identity_cannot_be_mixed_with_printed_log(self):
        worker, client, req = self.live()
        worker.tick()
        with client.cursor_session(req['job_id']) as cursor:
            client.follow_saved(req['job_id'], cursor, io.StringIO())
        path = client.root / 'follow' / (req['job_id'] + '.json')
        saved = decode(path.read_bytes())
        saved['log_sha256'] = 'f' * 64
        path.write_bytes(encode(saved))
        with self.assertRaisesRegex(GdiError, 'final log changed'):
            client.wait(req['job_id'], follow=True, stream=io.StringIO())

    def test_bad_package_and_failed_flush_never_advance_position(self):
        worker, client, req = self.live()
        worker.tick()
        remote = package_path(self.store, 'ci/jobs/' + req['job_id'])
        original = self.store.data[remote]
        self.store.data[remote] += b'changed'
        with self.assertRaisesRegex(GdiError, 'checksum'):
            client.wait(req['job_id'], follow=True, stream=io.StringIO())
        path = client.root / 'follow' / (req['job_id'] + '.json')
        self.assertEqual(decode(path.read_bytes())['offset'], 0)
        self.store.data[remote] = original
        class FailingStream(io.StringIO):
            def flush(self):
                raise OSError('broken console')
        with client.cursor_session(req['job_id']) as cursor:
            with self.assertRaisesRegex(OSError, 'broken console'):
                client.follow_saved(req['job_id'], cursor, FailingStream())
        self.assertEqual(decode(path.read_bytes())['offset'], 0)

    def test_corrupt_cursor_identity_and_concurrent_follower(self):
        _, client, req = self.live()
        with client.cursor_session(req['job_id']):
            with self.assertRaisesRegex(GdiError, 'another command'):
                with CiClient(self.ea, 'drive').cursor_session(req['job_id']):
                    self.fail('concurrent cursor should be locked')
        path = client.root / 'follow' / (req['job_id'] + '.json')
        value = decode(path.read_bytes())
        value['request_sha256'] = 'f' * 64
        path.write_bytes(encode(value))
        with self.assertRaisesRegex(GdiError, 'invalid CI follow cursor'):
            client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=.01)
        client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=.01, restart=True)

    def test_binary_final_log_and_cursor_write_failure_replay_without_skipping(self):
        worker, client, req = self.live(self.config([sys.executable, '-c',
                                                   "import os; os.write(1,b'\\x00\\xff\\n')"]))
        worker.tick()
        expected = (worker.spool(req['job_id']) / 'build.log').read_bytes()
        class Stream:
            def __init__(self):
                self.buffer = io.BytesIO()
        output = Stream()
        with client.cursor_session(req['job_id']) as cursor:
            # Package retrieval is independent from the cursor save fault.
            result = client.result(req['job_id'])
            with patch('gdi.ci.atomic_write', side_effect=OSError('cursor fsync')):
                with self.assertRaisesRegex(OSError, 'cursor fsync'):
                    client.follow_saved(req['job_id'], cursor, output, _result=result)
        self.assertEqual(output.buffer.getvalue(), expected)
        again = Stream()
        with client.cursor_session(req['job_id']) as cursor:
            client.follow_saved(req['job_id'], cursor, again)
        self.assertEqual(again.buffer.getvalue(), expected)
