import io
from unittest.mock import patch

from gdi.ci import CiClient
from gdi.exchange import decode, digest, encode
from gdi.git import GdiError
from tests.test_ci import CiTestCase


class FollowCursorTests(CiTestCase):
    def live(self):
        worker = self.worker()
        client, req = self.submit(worker)
        prefix = 'ci/jobs/' + req['job_id'] + '/log-chunks'
        self.store.mkdir(prefix)
        return worker, client, req, prefix

    def add_chunk(self, prefix, number, data):
        name = f'{number:08d}-{digest(data)}.bin'
        self.store.data[prefix + '/' + name] = data
        return name

    def test_follow_resume_after_wait_timeout_and_new_client_then_restart(self):
        worker, client, req, prefix = self.live()
        self.add_chunk(prefix, 1, b'FIRST\n')
        out = io.StringIO()
        self.assertEqual(client.wait(req['job_id'], follow=True, stream=out, timeout=.01, interval=.005)[1], 124)
        self.assertEqual(out.getvalue().count('FIRST'), 1)
        self.add_chunk(prefix, 2, b'SECOND\n')
        resumed = CiClient(self.ea, 'drive')
        out = io.StringIO()
        resumed.wait(req['job_id'], follow=True, stream=out, timeout=.01, interval=.005)
        self.assertNotIn('FIRST', out.getvalue())
        self.assertEqual(out.getvalue().count('SECOND'), 1)
        replay = io.StringIO()
        resumed.wait(req['job_id'], follow=True, stream=replay, timeout=.01, interval=.005, restart=True)
        self.assertEqual(replay.getvalue().count('FIRST'), 1)
        self.assertEqual(replay.getvalue().count('SECOND'), 1)

    def test_printed_prefix_cannot_shrink_or_change(self):
        _, client, req, prefix = self.live()
        first = self.add_chunk(prefix, 1, b'FIRST')
        with client.cursor_session(req['job_id']) as cursor:
            client.follow_saved(req['job_id'], cursor, io.StringIO())
        del self.store.data[prefix + '/' + first]
        with self.assertRaisesRegex(GdiError, 'shrank'):
            client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=.01)
        self.add_chunk(prefix, 1, b'CHANGED')
        with self.assertRaisesRegex(GdiError, 'changed'):
            client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=.01)

    def test_bad_checksum_and_failed_flush_never_advance_position(self):
        _, client, req, prefix = self.live()
        name = self.add_chunk(prefix, 1, b'FIRST')
        self.store.data[prefix + '/' + name] = b'WRONG'
        with self.assertRaisesRegex(GdiError, 'checksum'):
            client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=.01)
        path = client.root / 'follow' / (req['job_id'] + '.json')
        self.assertEqual(decode(path.read_bytes())['chunks'], [])
        self.store.data[prefix + '/' + name] = b'FIRST'
        class FailingStream(io.StringIO):
            def flush(self):
                raise OSError('broken console')
        with client.cursor_session(req['job_id']) as cursor:
            with self.assertRaisesRegex(OSError, 'broken console'):
                client.follow_saved(req['job_id'], cursor, FailingStream())
        self.assertEqual(decode(path.read_bytes())['chunks'], [])

    def test_corrupt_cursor_identity_and_concurrent_follower(self):
        _, client, req, _ = self.live()
        with client.cursor_session(req['job_id']):
            with self.assertRaisesRegex(GdiError, 'another command'):
                with CiClient(self.ea, 'drive').cursor_session(req['job_id']):
                    self.fail('concurrent cursor should be locked')
        path = client.root / 'follow' / (req['job_id'] + '.json')
        value = decode(path.read_bytes()); value['request_sha256'] = 'f' * 64
        path.write_bytes(encode(value))
        with self.assertRaisesRegex(GdiError, 'invalid CI follow cursor'):
            client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=.01)
        client.wait(req['job_id'], follow=True, stream=io.StringIO(), timeout=.01, restart=True)

    def test_binary_chunks_and_cursor_write_failure_can_replay_but_do_not_skip(self):
        _, client, req, prefix = self.live()
        self.add_chunk(prefix, 1, b'\x00\xff\n')
        class Stream:
            def __init__(self): self.buffer = io.BytesIO()
        stream = Stream()
        with client.cursor_session(req['job_id']) as cursor:
            with patch('gdi.ci.atomic_write', side_effect=OSError('cursor fsync')):
                with self.assertRaisesRegex(OSError, 'cursor fsync'):
                    client.follow_saved(req['job_id'], cursor, stream)
        self.assertEqual(stream.buffer.getvalue(), b'\x00\xff\n')
        again = Stream()
        with client.cursor_session(req['job_id']) as cursor:
            client.follow_saved(req['job_id'], cursor, again)
        self.assertEqual(again.buffer.getvalue(), b'\x00\xff\n')
