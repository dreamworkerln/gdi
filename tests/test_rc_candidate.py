"""Acceptance gates for the production native RC transport."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from gdi.branches import branch_directory
from gdi.exchange import Exchange, decode, digest, encode
from gdi.git import GdiError, Git
from gdi.diagnostics import command_session
from gdi.rc_transport import TransportSession
from gdi.transport import Rclone
from tests.rc_transport import RcServer, UnixHTTP


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('rclone'), 'Linux and rclone required')
class RcCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-rc-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / 'remote'
        self.environment = patch.dict(os.environ, RCLONE_CONFIG_GDIRCTEST_TYPE='local',
                                      RCLONE_CONFIG=os.devnull)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server = RcServer(trace_path=self.root / 'rc.jsonl')
        self.server.__enter__()
        self.addCleanup(self.server.close)
        self.url = 'gdirctest:' + str(self.remote)
        self.transport = self.server.transport(self.url)
        self.a = self.repo('a')
        self.first = self.commit(self.a, 'first')
        self.exchange = Exchange(self.a, self.server.transport)
        self.identity = self.exchange.add('drive', self.url, initialize=True)

    def repo(self, name):
        path = self.root / name
        path.mkdir()
        git = Git(path)
        git.call('init', '-q', '-b', 'main')
        return git

    def commit(self, git, subject):
        (git.path / 'file.txt').write_text(subject + '\n')
        git.call('add', 'file.txt')
        git.call('-c', 'user.name=Gdi Test', '-c', 'user.email=gdi@example.invalid',
                 'commit', '-qm', subject)
        return git.oid('HEAD')

    def metadata(self, raw, branch='main'):
        path = 'branches/' + branch_directory('refs/heads/' + branch) + '/' + digest(raw) + '.json'
        target = self.remote / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return path

    def synthetic_chain(self, count, branch='main'):
        previous = None
        paths = []
        for number in range(count):
            raw = encode({'version': 3, 'repository_id': self.identity,
                          'ref': 'refs/heads/' + branch, 'head': self.first,
                          'bundle_sha256': '1' * 64, 'previous': previous,
                          'nonce': f'{number:032x}', 'bundle_bytes': 100,
                          'bundle_kind': 'full', 'base_publication': None,
                          'base_head': None, 'prerequisites': []})
            paths.append(self.metadata(raw, branch))
            previous = digest(raw)
        return paths

    def chain(self, transport=None, branch='main'):
        return self.exchange.publications(transport or self.transport, self.identity,
                                          'refs/heads/' + branch)

    def test_full_incremental_unchanged_fetch_pull_and_log(self):
        head, publication, new = self.exchange.push('drive')
        self.assertTrue(new)
        self.assertEqual(head, self.first)
        second = self.commit(self.a, 'second')
        self.assertTrue(self.exchange.push('drive')[2])
        self.assertFalse(self.exchange.push('drive')[2])
        chain = self.chain()
        self.assertEqual([data['bundle_kind'] for _, data in chain], ['full', 'incremental'])
        b = self.repo('b')
        receiver = Exchange(b, self.server.transport)
        receiver.add('drive', self.url, expected_id=self.identity)
        receiver.fetch('drive')
        self.assertIsNone(b.oid('HEAD'))
        receiver.pull('drive')
        self.assertEqual(b.oid('HEAD'), second)
        self.assertEqual((b.path / 'file.txt').read_text(), 'second\n')
        self.assertEqual(receiver.log('drive')['total'], 2)
        self.assertEqual(chain, self.chain(Rclone(self.url)))
        self.assertIsNone(self.server.process.poll())

    def test_long_chain_and_fresh_corruption_with_unchanged_size_and_mtime(self):
        paths = self.synthetic_chain(64)
        chain = self.chain()
        self.assertEqual(len(chain), 64)
        self.assertEqual(chain, self.chain(Rclone(self.url)))
        path = self.remote / paths[0]
        original = path.stat()
        path.write_bytes(path.read_bytes().replace(b'"bundle_bytes":100', b'"bundle_bytes":101'))
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaisesRegex(GdiError, 'checksum'):
            self.chain()

    def test_missing_and_conflicting_predecessors(self):
        paths = self.synthetic_chain(3)
        self.assertEqual(len(self.chain()), 3)
        first_raw = (self.remote / paths[0]).read_bytes()
        (self.remote / paths[0]).unlink()
        with self.assertRaisesRegex(GdiError, 'predecessor is missing'):
            self.chain()
        (self.remote / paths[0]).write_bytes(first_raw)
        fork = decode((self.remote / paths[1]).read_bytes())
        fork['nonce'] = 'f' * 32
        self.metadata(encode(fork))
        with self.assertRaisesRegex(GdiError, 'conflicting publications'):
            self.chain()

    def test_new_listing_and_metadata_are_not_cached(self):
        self.assertEqual(self.chain(), [])
        self.synthetic_chain(1)
        self.assertEqual(len(self.chain()), 1)
        self.synthetic_chain(2)
        self.assertEqual(len(self.chain()), 2)

    def test_branch_percent_slash_unicode_spaces_and_other_branches(self):
        branch = 'feature/ветка%25'
        self.synthetic_chain(3, branch)
        self.synthetic_chain(2, 'main')
        self.assertEqual(len(self.chain(branch=branch)), 3)
        self.assertEqual(self.chain(branch=branch), self.chain(Rclone(self.url), branch))
        source = self.root / 'source'
        source.write_bytes(b'spaces and percent')
        target = 'metadata/with space % and #.json'
        self.transport.upload(source, target)
        self.assertEqual(self.transport.read(target), source.read_bytes())

    def test_immutable_upload_same_size_mtime_and_retry(self):
        source = self.root / 'source'
        source.write_bytes(b'original')
        target = 'nested/folder/event.json'
        self.transport.upload(source, target)
        self.transport.upload(source, target)
        stamp = source.stat()
        source.write_bytes(b'changed!')
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaises(GdiError):
            self.transport.upload(source, target)
        self.assertEqual(self.transport.read(target), b'original')
        self.transport.upload(source, 'nested/folder/another.json')
        self.assertEqual(self.transport.read('nested/folder/another.json'), b'changed!')
        source.write_bytes(b'longer changed bytes')
        with self.assertRaises(GdiError):
            self.transport.upload(source, target)
        self.assertEqual(self.transport.read(target), b'original')

    def test_advisory_overwrite_does_not_disable_immutability(self):
        source = self.root / 'source'
        target = 'ci/jobs/' + 'a' * 32 + '/status.json'
        source.write_bytes(b'{"state":1}')
        self.transport.update_advisory(source, target)
        source.write_bytes(b'{"state":2}')
        self.transport.update_advisory(source, target)
        self.assertEqual(self.transport.read(target), source.read_bytes())
        source.write_bytes(b'{"state":3}')
        with self.assertRaises(GdiError):
            self.transport.upload(source, target)
        with self.assertRaises(GdiError):
            self.transport.update_advisory(source, 'repository.json')

    def test_identity_replacement_is_detected_in_same_session(self):
        self.exchange.connect('drive')
        value = decode((self.remote / 'repository.json').read_bytes())
        value['repository_id'] = '0' * 32
        (self.remote / 'repository.json').write_bytes(encode(value))
        with self.assertRaisesRegex(GdiError, 'repository ID mismatch'):
            self.exchange.connect('drive')

    def test_corrupted_bundle_does_not_update_receiver_refs(self):
        self.exchange.push('drive')
        publication = self.chain()[-1][1]
        bundle = self.remote / 'bundles' / (publication['bundle_sha256'] + '.bundle')
        raw = bytearray(bundle.read_bytes())
        raw[-1] ^= 1
        bundle.write_bytes(raw)
        b = self.repo('b')
        receiver = Exchange(b, self.server.transport)
        receiver.add('drive', self.url, expected_id=self.identity)
        with self.assertRaisesRegex(GdiError, 'SHA256 mismatch'):
            receiver.fetch('drive')
        self.assertIsNone(b.oid('HEAD'))
        self.assertIsNone(b.oid('refs/remotes/drive/main'))

    def test_preupload_corruption_blocks_publication(self):
        self.exchange.push('drive')
        self.commit(self.a, 'second')
        before = set((self.remote / 'bundles').iterdir())
        calls = []
        original = self.transport.read_many

        def read_many(paths):
            result = original(paths)
            calls.append(True)
            if len(calls) == 2:
                first = next(iter(result))
                (self.remote / first).write_bytes(result[first] + b' ')
                result = original(result)
            return result

        with patch.object(self.server, 'transport', return_value=self.transport), \
                patch.object(self.exchange, 'transport_factory', return_value=self.transport), \
                patch.object(self.transport, 'read_many', side_effect=read_many):
            with self.assertRaisesRegex(GdiError, 'checksum'):
                self.exchange.push('drive')
        self.assertEqual(set((self.remote / 'bundles').iterdir()), before)

    def test_failure_after_manifest_upload_recovers_without_duplicate_publication(self):
        original = self.transport.upload

        def upload(source, target):
            original(source, target)
            if target.startswith('branches/'):
                raise GdiError('lost upload response')

        with patch.object(self.exchange, 'transport_factory', return_value=self.transport), \
                patch.object(self.transport, 'upload', side_effect=upload):
            with self.assertRaisesRegex(GdiError, 'lost upload response'):
                self.exchange.push('drive')
        publication_paths = set((self.remote / 'branches/main').iterdir())
        self.assertFalse(self.exchange.push('drive')[2])
        self.assertEqual(set((self.remote / 'branches/main').iterdir()), publication_paths)

    def test_batch_missing_oversized_symlink_and_invalid_paths(self):
        first = self.metadata(b'{"first":1}\n')
        second = self.metadata(b'{"second":2}\n')
        self.assertEqual(len(self.transport.read_many([first, second])), 2)
        (self.remote / second).unlink()
        with self.assertRaises(GdiError):
            self.transport.read_many([first, second])
        (self.remote / second).write_bytes(b'x' * (1024 * 1024 + 1))
        with self.assertRaisesRegex(GdiError, '1 MiB'):
            self.transport.read_many([first, second])
        with self.assertRaisesRegex(GdiError, '1 MiB'):
            self.transport.read(second)
        (self.remote / second).unlink()
        (self.remote / second).symlink_to(self.remote / first)
        with self.assertRaises(GdiError):
            self.transport.read_many([first, second])
        for paths in ([first, first], ['../repository.json'], ['branches/bad%xx/' + 'a' * 64 + '.json']):
            with self.assertRaises(GdiError):
                self.transport.read_many(paths)

    def test_duplicate_and_outside_listing_paths_are_rejected(self):
        row = {'Path': 'branches/main/' + 'a' * 64 + '.json', 'IsDir': False}
        with patch.object(self.transport, 'api', return_value={'list': [row, row]}):
            with self.assertRaisesRegex(GdiError, 'duplicate paths'):
                self.transport.list('branches', recursive=True)
        with patch.object(self.transport, 'api', return_value={'list': [{'Path': 'other', 'IsDir': False}]}):
            with self.assertRaisesRegex(GdiError, 'outside requested'):
                self.transport.list('branches')

    def test_missing_file_is_distinct_from_transport_failure(self):
        self.assertIsNone(self.transport.read_optional('missing.json'))
        with self.assertRaises(GdiError):
            self.transport.read('missing.json')
        self.server.process.terminate()
        self.server.process.wait(timeout=5)
        for operation in (lambda: self.transport.list('branches'),
                          lambda: self.transport.read_optional('missing.json')):
            with self.assertRaisesRegex(GdiError, 'not running'):
                operation()

    def test_authentication_private_socket_and_complete_redacted_trace(self):
        self.assertEqual(self.server.directory.stat().st_mode & 0o777, 0o700)
        connection = UnixHTTP(self.server.path)
        try:
            connection.request('POST', '/operations/list', json.dumps({'fs': self.url, 'remote': ''}),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()
        self.transport.read('repository.json')
        with self.assertRaises(GdiError):
            self.transport.read('missing.json')
        events = self.server.events
        recorded = [json.loads(line) for line in (self.root / 'rc.jsonl').read_text().splitlines()]
        self.assertEqual(recorded, events)
        self.assertEqual((self.root / 'rc.jsonl').stat().st_mode & 0o777, 0o600)
        starts = {item['call_id'] for item in events if item['event'] == 'rc_start'}
        finishes = {item['call_id'] for item in events if item['event'] == 'rc_finish'}
        self.assertEqual(starts, finishes)
        self.assertNotIn(self.server.password, json.dumps(events))
        self.assertNotIn(self.server.authorization, json.dumps(events))
        self.assertTrue(any(item.get('error') == 'RcError' for item in events))

    def replace_folder(self):
        self.remote.rename(self.root / 'old-remote')
        self.remote.mkdir()
        (self.remote / 'branches').mkdir()
        (self.remote / 'bundles').mkdir()
        value = {'version': 3, 'repository_id': '0' * 32, 'object_format': 'sha1'}
        (self.remote / 'repository.json').write_bytes(encode(value))

    def test_folder_replacement_before_bundle_upload_is_rejected(self):
        original = self.a.call

        def call(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ('bundle', 'create'):
                self.replace_folder()
            return result

        with patch.object(self.a, 'call', side_effect=call):
            with self.assertRaisesRegex(GdiError, 'repository ID mismatch'):
                self.exchange.push('drive')
        self.assertFalse(list((self.remote / 'bundles').iterdir()))
        self.assertFalse(list((self.remote / 'branches').iterdir()))

    def test_folder_replacement_during_bundle_upload_blocks_manifest(self):
        original = self.transport.upload

        def upload(source, target):
            original(source, target)
            if target.startswith('bundles/'):
                self.replace_folder()

        with patch.object(self.exchange, 'transport_factory', return_value=self.transport), \
                patch.object(self.transport, 'upload', side_effect=upload):
            with self.assertRaisesRegex(GdiError, 'repository ID mismatch'):
                self.exchange.push('drive')
        self.assertFalse(list((self.remote / 'branches').iterdir()))
        self.assertFalse(list((self.root / 'old-remote' / 'branches').iterdir()))

    def test_profile_records_process_and_every_rc_call_including_cache_reset(self):
        path = self.root / 'profile.jsonl'
        with command_session('status', path=path, progress=False) as profile:
            with TransportSession() as factory:
                transport = factory(self.url)
                transport.read('repository.json')
                with self.assertRaises(GdiError):
                    transport.read('missing.json')
                server = factory.server
        events = [json.loads(line) for line in path.read_text().splitlines()]
        starts = [item for item in events if item['event'] == 'rc_start']
        finishes = [item for item in events if item['event'] == 'rc_finish']
        self.assertEqual(len(starts), 3)
        self.assertEqual({item['call_id'] for item in starts}, {item['call_id'] for item in finishes})
        self.assertEqual(starts[0]['operation'], 'fscache/clear')
        self.assertEqual(finishes[-1]['error'], 'RcError')
        self.assertEqual(finishes[-1]['status'], 404)
        self.assertEqual(events[-1]['subprocess_counts'], {'rclone': 1})
        self.assertEqual(events[-1]['rc_calls'], 3)
        self.assertEqual(profile.running, {})
        self.assertNotIn(server.password, path.read_text())
        self.assertNotIn(server.authorization, path.read_text())
        self.assertIsNotNone(server.process.poll())

    def test_worker_opens_and_closes_rc_for_each_operation(self):
        from gdi.worker import Worker
        from gdi.worker_config import load_config
        config_path = self.root / 'worker.json'
        config_path.write_bytes(encode({
            'config_version': 2, 'worker_id': 'rc-host', 'remote_url': self.url,
            'state_dir': str(self.root / 'state'), 'cache_dir': str(self.root / 'cache'),
            'transport': {'connect_timeout_seconds': 5, 'timeout_seconds': 7,
                          'retries': 1, 'low_level_retries': 2}}))
        config = load_config(config_path)
        worker = Worker(config)
        self.addCleanup(worker.close)
        created = []
        original = RcServer.__enter__

        def start(server):
            created.append(server)
            return original(server)

        with patch.object(RcServer, '__enter__', start), \
                patch('gdi.runner.resolve_profile', side_effect=lambda profile, root: profile):
            worker.advertise()
            self.assertIsNone(worker.transport_session.server)
            self.assertFalse(worker.tick())
            self.assertIsNone(worker.transport_session.server)
            self.assertFalse(worker.tick())
        self.assertEqual(len(created), 3)
        for server in created:
            self.assertIsNotNone(server.process.poll())
            self.assertFalse(server.directory.exists())
            args = server.events[0]['argv']
            self.assertEqual(args[args.index('--timeout') + 1], '7s')
            self.assertEqual(args[args.index('--retries') + 1], '1')

    def test_selected_branch_listing_does_not_read_other_branches(self):
        self.synthetic_chain(2, 'main')
        self.synthetic_chain(2, 'other')
        with patch.object(self.transport, 'api', wraps=self.transport.api) as api:
            self.assertEqual(len(self.chain()), 2)
        listings = [call.kwargs for call in api.call_args_list
                    if call.args[0] == 'operations/list']
        self.assertEqual([item['remote'] for item in listings], ['branches', 'branches/main'])
        self.assertTrue(all(not item['opt']['recurse'] for item in listings))
        with self.assertRaises(GdiError):
            shutil.rmtree(self.remote / 'branches')
            self.chain()

    def test_duplicate_branch_folder_is_rejected_before_selecting_a_tip(self):
        row = {'Path': 'branches/main', 'IsDir': True}
        with patch.object(self.transport, 'api', return_value={'list': [row, row]}):
            with self.assertRaisesRegex(GdiError, 'duplicate paths'):
                self.chain()

    def test_sigkill_parent_stops_rc_process(self):
        ready = self.root / 'parent-ready.json'
        script = '''
import json, sys, time
from pathlib import Path
from gdi.rc_transport import RcServer
with RcServer() as server:
    Path(sys.argv[1]).write_text(json.dumps({'pid': server.process.pid, 'socket': str(server.path)}))
    time.sleep(60)
'''
        parent = subprocess.Popen([sys.executable, '-c', script, str(ready)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        child = None
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), 'parent failed to start RC')
            value = json.loads(ready.read_text())
            child = value['pid']
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            # A dead adopted zombie is not a running daemon (container PID 1
            # need not reap immediately). On a normal host it disappears.
            while time.monotonic() < deadline:
                status = Path(f'/proc/{child}/stat')
                if not status.exists() or status.read_text().split(') ')[1][0] == 'Z':
                    break
                time.sleep(0.02)
            else:
                self.fail('RC survives parent SIGKILL')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                connection = UnixHTTP(value['socket'], timeout=0.2)
                try:
                    connection.connect()
                except OSError:
                    break
                finally:
                    connection.close()
                time.sleep(0.02)
            else:
                self.fail('RC listener survives parent SIGKILL')
            shutil.rmtree(Path(value['socket']).parent)
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_context_closes_process_on_error_and_interrupt(self):
        for exception in (GdiError('simulated error'), KeyboardInterrupt()):
            server = RcServer()
            with self.assertRaises(type(exception)):
                with server:
                    directory = server.directory
                    self.assertIsNone(server.process.poll())
                    raise exception
            self.assertIsNotNone(server.process.poll())
            self.assertFalse(directory.exists())

    def test_startup_failure_cleans_up(self):
        server = RcServer(executable='/no/such/gdi-rclone-' + uuid.uuid4().hex)
        with self.assertRaisesRegex(GdiError, 'startup failed'):
            with server:
                self.fail('startup must fail')
        self.assertIsNone(server.temporary)
        self.assertIsNone(server.errors)

    def test_timeout_during_transfer_stops_rc_and_leaves_no_process(self):
        entered = threading.Event()
        release = threading.Event()
        recorded_before_finish = threading.Event()

        class SlowFile(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', '8192')
                self.end_headers()

            def do_GET(self):
                entered.set()
                events = [json.loads(line) for line in
                          (self_root / 'timeout.jsonl').read_text().splitlines()]
                if events[-1]['event'] == 'rc_start':
                    recorded_before_finish.set()
                release.wait(10)
                try:
                    self.do_HEAD()
                    self.wfile.write(b'x' * 8192)
                except OSError:
                    pass

        self_root = self.root
        source = ThreadingHTTPServer(('127.0.0.1', 0), SlowFile)
        thread = threading.Thread(target=source.serve_forever, daemon=True)
        thread.start()
        server = RcServer(timeout=0.5, trace_path=self.root / 'timeout.jsonl')
        try:
            with patch.dict(os.environ, RCLONE_CONFIG_GDIRCSLOW_TYPE='http',
                            RCLONE_CONFIG_GDIRCSLOW_URL=f'http://127.0.0.1:{source.server_port}/'):
                with self.assertRaises(TimeoutError):
                    with server:
                        server.request('/operations/copyfile', {
                            'srcFs': 'gdircslow:', 'srcRemote': 'payload',
                            'dstFs': str(self.root), 'dstRemote': 'slow-download'})
            self.assertTrue(entered.is_set(), 'must time out during the actual GET')
            self.assertTrue(recorded_before_finish.is_set(), 'start must be flushed before execution')
            self.assertIsNotNone(server.process.poll())
            self.assertFalse(server.directory.exists())
            self.assertEqual(server.events[-2]['event'], 'rc_finish')
            self.assertEqual(server.events[-2]['error'], 'TimeoutError')
        finally:
            release.set()
            source.shutdown()
            source.server_close()
            thread.join(timeout=2)
