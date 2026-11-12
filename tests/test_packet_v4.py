"""Default protocol contracts and exact logical Drive-operation budgets (v4)."""

import contextlib
import io
import os
import shutil
from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from gdi import packet
from gdi.agent_receiver import receive, recover
from gdi.agent_sync import pull, push, receipt_path
from gdi.ci import CiClient
from gdi.exchange import Exchange, LegacyExchange
from gdi.git import GdiError, Git
from gdi.publication import decode, encode
from gdi.worker import Worker
from gdi.worker_config import load_config
from tests.test_exchange import ExchangeTestCase, MemoryTransport


class Transport:
    creates_parents = True

    def __init__(self, owner, url):
        self.owner = owner
        self.prefix = url.removeprefix('memory:hub').lstrip('/')
        self.url = url

    def path(self, name):
        return self.prefix + ('/' if self.prefix and name else '') + name

    def record(self, verb, name):
        self.owner.calls.append((verb, self.path(name)))

    def mkdir(self, name):
        self.record('mkdir', name)
        self.owner.store.mkdir(self.path(name))

    def list(self, name, *, recursive=False):
        self.record('list', name)
        return self.owner.store.list(self.path(name), recursive=recursive)

    def read(self, name):
        self.record('read', name)
        return self.owner.store.read(self.path(name))

    def read_optional(self, name):
        self.record('read', name)
        return self.owner.store.data.get(self.path(name))

    def upload(self, source, name):
        self.record('upload', name)
        self.owner.store.mkdir(self.path(name).rpartition('/')[0])
        self.owner.store.upload(source, self.path(name))
        if self.owner.failure == 'after-upload' and name.startswith('packets/'):
            self.owner.failure = None
            raise GdiError('response lost after packet upload')

    def download(self, name, target):
        self.record('download', name)
        self.owner.store.download(self.path(name), target)

    def update_head(self, source, name):
        self.record('head', name)
        if self.owner.failure == 'head':
            raise GdiError('pointer upload failed')
        self.owner.store.mkdir(self.path(name).rpartition('/')[0])
        self.owner.store.data[self.path(name)] = Path(source).read_bytes()

    def packet_exists(self, name):
        self.record('stat', name)
        return self.path(name) in self.owner.store.data

    def move_packet(self, source_url, source_name, name):
        self.record('move', name)
        source = Transport(self.owner, source_url).path(source_name)
        self.owner.store.mkdir(self.path(name).rpartition('/')[0])
        self.owner.store.data[self.path(name)] = self.owner.store.data.pop(source)

    def update_repository(self, source):
        self.record('upgrade', 'repository.json')
        self.owner.store.data[self.path('repository.json')] = Path(source).read_bytes()

    def update_advisory(self, source, name):
        self.record('advisory', name)
        self.owner.store.mkdir(self.path(name).rpartition('/')[0])
        self.owner.store.data[self.path(name)] = Path(source).read_bytes()

    def update_agent_export(self, source, name):
        self.record('legacy-export', name)
        self.owner.store.data[self.path(name)] = Path(source).read_bytes()

    def update_checkpoint(self, source, name):
        self.owner.store.data[self.path(name)] = Path(source).read_bytes()

    def delete_publication(self, name):
        self.owner.store.data.pop(self.path(name), None)

    def delete_notification(self, name):
        self.record('delete', name)
        self.owner.store.data.pop(self.path(name), None)

    delete_queue = delete_notification
    delete_packet = delete_notification
    delete_head = delete_notification


class PacketV4Tests(unittest.TestCase):
    # Reuse Git fixture helpers, not the historical v3 setup or monkeypatches.
    repo = ExchangeTestCase.repo
    commit = ExchangeTestCase.commit

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-v4-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = MemoryTransport()
        self.calls = []
        self.failure = None
        self.factory = lambda url: Transport(self, url)
        self.a = self.repo('operator')
        self.first = self.commit(self.a, 'first')
        self.ea = Exchange(self.a, self.factory)
        self.ea.publisher_worker_id = 'user-host'
        self.identity = self.ea.create('drive', 'memory:hub/project', inbox_root='memory:hub')
        self.store.mkdir('inbox')

    def publish(self):
        self.ea.push('drive')
        return decode(self.store.data['project/heads/main.json'])

    def inputs(self, value=None):
        value = value or self.publish()
        head = self.root / 'head.json'
        head.write_bytes(encode(value))
        path = self.root / 'download.zip.bin'
        path.write_bytes(self.store.data['project/' + packet.packet_path(value['publication_id'])])
        return head, path

    def agent(self):
        head, path = self.inputs()
        destination = self.root / 'agent'
        pull(path, self.identity, head_path=head, destination=destination)
        return Git.discover(destination, isolated=True)

    def worker(self):
        path = self.root / 'worker.json'
        executable = self.root / 'fake-act'
        executable.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys
if sys.argv[1:] == ['--version']:
    print('act version 0.2.89')
    raise SystemExit(0)
args = sys.argv[1:]
checkout = pathlib.Path(args[args.index('--directory') + 1])
ok = (checkout / 'file.txt').read_text().strip() == 'fixed'
print(json.dumps({'jobResult': 'success' if ok else 'failure', 'job': 'tests'}))
raise SystemExit(0 if ok else 1)
''')
        executable.chmod(0o700)
        path.write_bytes(encode({'config_version': 2, 'worker_id': 'user-host', 'remote_url': 'memory:hub',
            'state_dir': str(self.root / 'state'), 'cache_dir': str(self.root / 'cache'),
            'act_executable': str(executable), 'platforms': {'ubuntu-latest': '-self-hosted'}, 'timeout_seconds': 20}))
        worker = Worker(load_config(path), self.factory)
        self.addCleanup(worker.close)
        return worker

    def request(self):
        git = self.agent()
        head = self.commit(git, 'agent work')
        directory = self.root / 'plan'
        plan = push(git, self.identity, directory)
        info = plan['files']['package']
        self.factory('memory:hub').upload(info['local_path'], 'inbox/' + info['target_name'])
        return git, directory, plan, head

    def test_native_push_has_one_read_two_writes_and_no_exports_or_notifications(self):
        self.publish()
        head = self.commit(self.a, 'next')
        self.calls.clear()
        self.ea.push('drive', full=True)
        value = self.ea.last_publication['packet_pointer']
        self.assertEqual(self.calls, [('read', 'project/heads/main.json'),
            ('upload', 'project/' + packet.packet_path(value['publication_id'])), ('head', 'project/heads/main.json')])
        self.assertEqual(value['metadata']['head'], head)
        self.assertFalse(any('/agent/' in path or path.startswith('inbox/') for path in self.store.data))
        self.assertFalse(any(path.startswith('project/bundles/') or path.startswith('project/branches/') for path in self.store.data))

    def test_unchanged_full_push_only_reads_the_pointer(self):
        self.publish()
        self.calls.clear()
        self.assertFalse(self.ea.push('drive', full=True)[2])
        self.assertEqual(self.calls, [('read', 'project/heads/main.json')])

    def test_failed_pointer_retry_does_not_upload_packet_again(self):
        self.publish()
        self.commit(self.a, 'next')
        self.failure = 'head'
        with self.assertRaisesRegex(GdiError, 'pointer'):
            self.ea.push('drive')
        self.failure = None
        self.calls.clear()
        self.ea.push('drive')
        self.assertEqual([verb for verb, _ in self.calls], ['read', 'head'])

    def test_ambiguous_upload_uses_one_recovery_probe_not_reverse_download(self):
        self.publish()
        self.commit(self.a, 'next')
        self.failure = 'after-upload'
        with self.assertRaisesRegex(GdiError, 'response lost'):
            self.ea.push('drive')
        self.calls.clear()
        self.ea.push('drive')
        self.assertEqual([verb for verb, _ in self.calls], ['read', 'stat', 'head'])

    def test_fetch_downloads_one_packet_and_checks_git_locally(self):
        value = self.publish()
        git = self.repo('receiver')
        exchange = Exchange(git, self.factory)
        exchange.add('drive', 'memory:hub/project', expected_id=self.identity)
        self.calls.clear()
        exchange.fetch('drive', 'main')
        self.assertEqual(self.calls, [('read', 'project/heads/main.json'),
                                     ('download', 'project/' + packet.packet_path(value['publication_id']))])
        self.assertEqual(git.oid('refs/remotes/drive/main'), self.first)

    def test_corrupt_packet_does_not_change_checkout(self):
        self.publish()
        head, path = self.inputs()
        path.write_bytes(path.read_bytes()[:-20])
        with self.assertRaisesRegex(GdiError, 'checksum or size'):
            pull(path, self.identity, head_path=head, destination=self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())

    def test_push_rejects_non_fast_forward_without_uploads(self):
        self.publish()
        git = self.repo('other', clone=self.a)
        exchange = Exchange(git, self.factory)
        exchange.add('drive', 'memory:hub/project', expected_id=self.identity)
        self.commit(self.a, 'operator next')
        self.ea.push('drive')
        self.commit(git, 'other next')
        self.calls.clear()
        with self.assertRaisesRegex(GdiError, 'fast-forward'):
            exchange.push('drive')
        self.assertEqual(self.calls, [('read', 'project/heads/main.json')])

    def test_worker_moves_exact_uploaded_packet_without_second_upload(self):
        git, directory, plan, head = self.request()
        original = Path(plan['files']['package']['local_path']).read_bytes()
        worker = self.worker()
        operator = self.a.snapshot()
        self.calls.clear()
        result = receive(worker, self.factory('memory:hub'), plan['files']['package']['target_name'])
        self.assertEqual(result['state'], 'PUBLISHED')
        self.assertEqual(result['head'], head)
        self.assertEqual(self.a.snapshot(), operator)
        target = 'project/' + packet.packet_path(result['publication_id'])
        self.assertEqual(self.store.data[target], original)
        self.assertFalse(any(verb == 'upload' and path.endswith('.zip') for verb, path in self.calls))
        self.assertEqual(sum(verb == 'move' for verb, _ in self.calls), 1)
        receipt = self.root / 'receipt.json'
        receipt.write_bytes(encode(result))
        self.assertEqual(push(git, self.identity, directory, receipt=receipt)['state'], 'PUBLISHED')
        self.assertEqual(push(git, self.identity, self.root / 'empty')['state'], 'ALREADY_PUBLISHED')

    def test_worker_recovers_moved_packet_when_inbox_entry_is_gone(self):
        _, _, plan, _ = self.request()
        worker = self.worker()
        name = plan['files']['package']['target_name']
        self.failure = 'head'
        with self.assertRaises(GdiError):
            receive(worker, self.factory('memory:hub'), name)
        self.assertNotIn('inbox/' + name, self.store.data)
        self.failure = None
        self.calls.clear()
        recover(worker, self.factory('memory:hub'))
        self.assertFalse(any(verb in ('move', 'download') for verb, _ in self.calls))
        result = decode(self.store.data['project/' + receipt_path(self.identity, plan['request_id'])])
        self.assertEqual(result['state'], 'PUBLISHED')

    def test_worker_rejects_stale_base_without_moving_data(self):
        _, _, plan, _ = self.request()
        self.commit(self.a, 'someone else')
        self.ea.push('drive')
        worker = self.worker()
        self.calls.clear()
        result = receive(worker, self.factory('memory:hub'), plan['files']['package']['target_name'])
        self.assertEqual(result['state'], 'CONFLICT')
        self.assertFalse(any(verb == 'move' for verb, _ in self.calls))

    def test_cli_agent_interface_is_offline(self):
        from gdi.cli import main
        head, path = self.inputs()
        self.calls.clear()
        with patch('gdi.rc_transport.TransportSession', side_effect=AssertionError('network')):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['agent', 'pull', '--package', str(path), '--head', str(head),
                             '--repository-id', self.identity, '--destination', str(self.root / 'cli')])
            self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])

    def test_agent_pull_preserves_dirty_worktree_branch_and_divergent_history(self):
        git = self.agent()
        head, path = self.inputs()
        dirty = git.path / 'uncommitted'
        dirty.write_text('keep')
        with self.assertRaisesRegex(GdiError, 'dirty'):
            pull(path, self.identity, head_path=head, repo=git.path)
        self.assertEqual(dirty.read_text(), 'keep')
        dirty.unlink()
        git.call('checkout', '-qb', 'selected')
        with self.assertRaisesRegex(GdiError, 'does not switch branches'):
            pull(path, self.identity, head_path=head, repo=git.path)
        git.call('checkout', '--quiet', 'main')
        local = self.commit(git, 'local')
        self.commit(self.a, 'remote')
        self.publish()
        head, path = self.inputs()
        with self.assertRaisesRegex(GdiError, 'diverged'):
            pull(path, self.identity, head_path=head, repo=git.path)
        self.assertEqual(git.oid('HEAD'), local)

    def test_packet_reader_does_not_mask_consumer_failures(self):
        head, path = self.inputs()
        error = RuntimeError('consumer error')
        with self.assertRaises(RuntimeError) as caught:
            with packet.read(path, self.identity, decode(head.read_bytes())):
                raise error
        self.assertIs(caught.exception, error)

    def test_ci_prepare_and_accept_are_offline_without_upload_proof(self):
        from gdi import agent_ci
        workflow = self.a.path / '.github/workflows/ci.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('on: push\njobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo test\n')
        self.a.call('add', '.github')
        self.commit(self.a, 'fixed')
        git = self.agent()
        worker = self.worker()
        worker.advertise()
        caps = self.root / 'capabilities.json'
        caps.write_bytes(self.store.data['ci/workers/user-host/capabilities.json'])
        inputs = self.root / 'inputs.json'
        value = decode((self.root / 'head.json').read_bytes())
        inputs.write_bytes(encode({'snapshot_version': 2, 'repository_id': self.identity,
            'repository_path': 'project', 'ref': 'refs/heads/main', 'head': 'head.json', 'files': {}, 'listings': {}}))
        output = self.root / 'ci-plan'
        self.calls.clear()
        prepared = agent_ci.prepare(git, inputs, self.identity, output,
                                   value['publication_id'], 'user-host', 'full', caps)
        self.assertEqual(set(prepared['files']), {'inbox'})
        accepted = agent_ci.accept(git, output, inputs, self.identity, capabilities_path=caps, uploaded=True)
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(shutil.which('rclone'), 'requires rclone with local backend')
    def test_same_packet_is_adopted_by_real_cli_and_rc_transports(self):
        from gdi.rc_transport import TransportSession
        env = {'RCLONE_CONFIG': os.devnull, 'RCLONE_CONFIG_GDIV4TEST_TYPE': 'local'}
        for mode in ('cli', 'rc'):
            with self.subTest(mode=mode), patch.dict(os.environ, {**env, 'GDI_TRANSPORT': mode}):
                root_path = self.root / ('real-' + mode)
                root_url = 'gdiv4test:' + str(root_path)
                url = root_url + '/project'
                with TransportSession() as session:
                    exchange = Exchange(self.a, session)
                    exchange.publisher_worker_id = 'user-host'
                    identity = exchange.create('real-' + mode, url, inbox_root=root_url)
                    exchange.push('real-' + mode)
                    pointer = decode((root_path / 'project/heads/main.json').read_bytes())
                    downloaded = root_path / 'project' / packet.packet_path(pointer['publication_id'])
                    destination = self.root / ('agent-' + mode)
                    pull(downloaded, identity, head_path=root_path / 'project/heads/main.json', destination=destination)
                    git = Git.discover(destination, isolated=True)
                    head = self.commit(git, 'real packet')
                    plan = push(git, identity, self.root / ('plan-' + mode))
                    item = plan['files']['package']
                    root = session(root_url)
                    root.upload(item['local_path'], 'inbox/' + item['target_name'])
                    worker = SimpleNamespace(root=self.root / ('state-' + mode), worker_id='user-host',
                        config={'remote_url': root_url}, transport_factory=session)
                    result = receive(worker, root, item['target_name'])
                    self.assertEqual(result['state'], 'PUBLISHED')
                    current = decode((root_path / 'project/heads/main.json').read_bytes())
                    self.assertEqual(current['metadata']['head'], head)
                    adopted = root_path / 'project' / packet.packet_path(current['publication_id'])
                    self.assertEqual(adopted.read_bytes(), Path(item['local_path']).read_bytes())
                    self.assertFalse((root_path / 'inbox' / item['target_name']).exists())

    def test_native_clone_uses_selected_head_without_branch_scan_or_readback(self):
        from gdi.repository import clone
        self.publish()
        self.calls.clear()
        result = clone('memory:hub/project', self.root / 'clone', branch='main', expected_id=self.identity,
                       transport_factory=self.factory)
        self.assertEqual(result['head'], self.first)
        self.assertEqual([verb for verb, _ in self.calls], ['read', 'read', 'download'])

    def test_upgrade_preserves_identity_git_commits_and_legacy_files(self):
        legacy = LegacyExchange(self.a, self.factory)
        identity = legacy.create('old', 'memory:hub/old')
        legacy.push('old')
        old_files = {name: raw for name, raw in self.store.data.items() if name.startswith('old/')}
        result = self.ea.upgrade('old', quiescent=True, all_clients_updated=True)
        self.assertEqual(result['repository_id'], identity)
        self.assertEqual(result['version'], 4)
        for name, raw in old_files.items():
            if name != 'old/repository.json':
                self.assertEqual(self.store.data[name], raw)
        value = decode(self.store.data['old/heads/main.json'])
        self.assertEqual(value['metadata']['head'], self.first)
        self.calls.clear()
        self.assertFalse(self.ea.push('old')[2])
        self.assertEqual(self.calls, [('read', 'old/heads/main.json')])

    def test_upgrade_requires_quiescence_before_network(self):
        self.calls.clear()
        with self.assertRaisesRegex(GdiError, 'quiescent'):
            self.ea.upgrade('drive')
        self.assertEqual(self.calls, [])

    def test_gc_is_explicit_and_keeps_current_packet_and_runtime(self):
        self.publish()
        for text in ('second', 'third'):
            self.commit(self.a, text)
            latest = self.publish()
        self.store.data['project/runtime/gdi.pyz'] = b'runtime sentinel'
        before = dict(self.store.data)
        preview = self.ea.gc('drive', keep_checkpoints=1, report=lambda _: None)
        self.assertEqual(len(preview['remove_packets']), 2)
        self.assertEqual(self.store.data, before)
        self.ea.gc('drive', apply=True, quiescent=True, keep_checkpoints=1, report=lambda _: None)
        self.assertIn('project/' + packet.packet_path(latest['publication_id']), self.store.data)
        self.assertEqual(self.store.data['project/runtime/gdi.pyz'], b'runtime sentinel')

    def test_pending_agent_request_blocks_gc(self):
        self.request()
        with self.assertRaisesRegex(GdiError, 'pending agent push'):
            self.ea.gc('drive', apply=True, quiescent=True, report=lambda _: None)

    def test_work_import_and_explicit_retirement_preserve_target(self):
        from gdi import work, work_import
        self.publish()
        work.start(self.a, '1', self.identity)
        (self.a.path / 'file.txt').write_text('work final\n')
        self.a.call('add', 'file.txt')
        work.checkpoint(self.a, 'finished', final=True, repository_id=self.identity)
        self.ea.push('drive')
        self.a.call('checkout', '--quiet', 'main')
        available = work_import.candidates(self.ea, 'drive')
        self.assertEqual(len(available['choices']), 1)
        plan = work_import.prepare(self.ea, 'drive', available['choices'][0], available['target_head'])
        work_import.apply(self.ea, plan['plan_path'])
        work_import.push(self.ea, 'drive')
        target = self.a.oid('HEAD')
        result = self.ea.gc('drive', apply=True, quiescent=True, keep_imported=0, report=lambda _: None)
        self.assertIn('refs/heads/gdi-work/1', result['retired_works'])
        self.assertEqual(self.a.oid('HEAD'), target)
        self.assertNotIn('project/heads/gdi-work%2F1.json', self.store.data)
        with self.assertRaisesRegex(GdiError, 'retired'):
            self.ea.push('drive', 'gdi-work/1')

    def test_ci_consumes_same_packet_and_does_not_read_back_result(self):
        workflow = self.a.path / '.github/workflows/ci.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('on: push\njobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo test\n')
        self.a.call('add', '.github')
        self.commit(self.a, 'fixed')
        worker = self.worker()
        worker.advertise()
        value = self.publish()
        self.calls.clear()
        client = CiClient(self.ea, 'drive')
        req = client.submit(value['publication_id'], 'user-host')
        self.assertFalse(any(verb == 'download' for verb, _ in self.calls))
        self.calls.clear()
        worker.tick()
        self.assertFalse(any(verb == 'download' and '/ci/' in name for verb, name in self.calls))
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
