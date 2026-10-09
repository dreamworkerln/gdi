import json
import contextlib
import io
import os
from pathlib import Path
import sys
import shutil
import socket
import unittest
from unittest.mock import patch
import zipfile

from gdi.ci import CiClient
from gdi.exchange import Exchange, decode, encode
from gdi.git import GdiError
from gdi.inbox import filename, notification, publish, relative_repository
from gdi.worker_config import load_config
from tests.test_exchange import ExchangeTestCase, MemoryTransport


class PrefixTransport:
    def __init__(self, store, prefix, calls):
        self.store, self.prefix, self.calls = store, prefix, calls

    def path(self, path):
        return self.prefix + ('/' if self.prefix and path else '') + path

    def mkdir(self, path):
        return self.store.mkdir(self.path(path))

    def list(self, path, *, recursive=False):
        self.calls.append((self.prefix, path, recursive))
        return self.store.list(self.path(path), recursive=recursive)

    def read(self, path):
        return self.store.read(self.path(path))

    def upload(self, source, path):
        return self.store.upload(source, self.path(path))

    def download(self, path, target):
        return self.store.download(self.path(path), target)

    def update_advisory(self, source, path):
        return self.store.update_advisory(source, self.path(path))

    def delete_notification(self, path):
        if self.store.fail == 'delete-notification':
            raise GdiError('lost notification acknowledgement')
        return self.store.delete_queue(self.path(path))

    def delete_queue(self, path):
        return self.store.delete_queue(self.path(path))

    def delete_bundle(self, path):
        return self.store.delete_bundle(self.path(path))


class InboxTests(ExchangeTestCase):
    def setUp(self):
        super().setUp()
        self.store = MemoryTransport()
        self.calls = []
        self.factory = lambda url: PrefixTransport(self.store, url.removeprefix('memory:hub').lstrip('/'), self.calls)
        for git in (self.a, self.b):
            Exchange(git).remove('drive')
        self.ea = Exchange(self.a, self.factory)
        self.eb = Exchange(self.b, self.factory)
        self.identity = self.ea.add('drive', 'memory:hub/repos/a', initialize=True, inbox_root='memory:hub')
        self.eb.add('drive', 'memory:hub/repos/a', inbox_root='memory:hub')
        self.fake = self.root / 'fake-act'
        self.counter = self.root / 'executions'
        self.arguments = self.root / 'arguments.json'
        self.fake.write_text('#!' + sys.executable + '\n' + r'''import json, pathlib, subprocess, sys
args = sys.argv[1:]
if args == ['--version']:
    print('act version 0.2.89')
    sys.exit(0)
def option(name): return args[args.index(name) + 1]
checkout = pathlib.Path(option('--directory'))
event = json.loads(pathlib.Path(option('--eventpath')).read_text())
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip() == event['after']
assert pathlib.Path(option('--workflows')).exists()
assert event['ref'].startswith('refs/heads/')
assert 'SHA_REF=' + event['after'] in args
counter = pathlib.Path(COUNTER)
counter.write_text(counter.read_text() + 'x' if counter.exists() else 'x')
pathlib.Path(ARGUMENTS).write_text(json.dumps(args))
output = pathlib.Path(option('--artifact-server-path')) / 'firmware.bin'
output.write_bytes(b'firmware artifact')
source = (checkout / 'file.txt').read_text().strip()
print(json.dumps({'raw_output': True, 'msg': source}), flush=True)
if source != 'skip':
    print(json.dumps({'jobResult': 'success' if source == 'fixed' else 'failure', 'job': 'tests'}), flush=True)
sys.exit(0 if source in ('fixed', 'skip') else 1)
'''.replace('COUNTER', repr(str(self.counter))).replace('ARGUMENTS', repr(str(self.arguments))))
        self.fake.chmod(0o700)
        workflow = self.a.path / '.github/workflows/checks.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('name: tests\non: [push, workflow_dispatch]\njobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo tests\n')
        self.a.call('add', '.github')
        self.commit(self.a, 'fixed')

    def config(self):
        path = self.root / 'worker.json'
        path.write_bytes(encode({'config_version': 2, 'worker_id': 'user-host', 'remote_url': 'memory:hub',
                                'state_dir': str(self.root / 'state'), 'cache_dir': str(self.root / 'cache'),
                                'act_executable': str(self.fake), 'platforms': {'ubuntu-latest': '-self-hosted'}, 'timeout_seconds': 10}))
        return load_config(path)

    def worker(self, config=None):
        from gdi.worker import Worker
        worker = Worker(config or self.config(), self.factory)
        self.addCleanup(worker.close)
        worker.advertise()
        return worker

    def submit(self, worker, exchange=None, **kwargs):
        exchange = exchange or self.ea
        _, pub, _ = exchange.push('drive')
        client = CiClient(exchange, 'drive')
        return client, client.submit(pub, 'user-host', **kwargs)

    def ci_events(self):
        return {path: decode(raw) for path, raw in self.store.data.items()
                if path.startswith('inbox/') and decode(raw)['type'] == 'ci_requested'}

    def test_empty_poll_lists_only_the_shared_inbox(self):
        worker = self.worker()
        for number in range(100):
            self.store.mkdir('repos/repository-' + str(number))
        self.calls.clear()
        worker.tick()
        self.assertEqual(self.calls, [('', 'inbox', False)])
        self.assertNotIn('repositories', worker.config)

    def test_two_projects_publish_independent_notifications_and_complete(self):
        worker = self.worker()
        other = self.repo('other')
        (other.path / '.github/workflows').mkdir(parents=True)
        (other.path / '.github/workflows/ci.yaml').write_text('on: push\njobs: {}\n')
        other.call('add', '.github')
        self.commit(other, 'fixed')
        other_exchange = Exchange(other, self.factory)
        other_exchange.add('drive', 'memory:hub/repos/b', initialize=True, inbox_root='memory:hub')
        a, req_a = self.submit(worker)
        b, req_b = self.submit(worker, other_exchange)
        self.assertEqual(len(self.ci_events()), 2)
        for event in self.ci_events().values():
            prefix = event['repository_path'] + '/ci/jobs/' + event['job_id']
            self.assertIn(prefix + '/request.ready', self.store.data)
        worker.tick()
        self.assertEqual(a.status(req_a['job_id'])['state'], 'PASS')
        self.assertEqual(b.status(req_b['job_id'])['state'], 'PASS')
        self.assertEqual(self.counter.read_text(), 'xx')
        self.assertFalse(any(path.startswith('inbox/') for path in self.store.data))
        worker.tick()
        self.assertEqual(self.counter.read_text(), 'xx')
        self.assertFalse(any('/ci/queue/' in path for path in self.store.data))

    def test_partial_notification_and_missing_ready_are_retained_without_execution(self):
        worker = self.worker()
        client, req = self.submit(worker)
        path, event = next(iter(self.ci_events().items()))
        raw = self.store.data[path]
        self.store.data[path] = raw[:10]
        worker.tick()
        self.assertIsNone(worker.ledger.get(req['job_id']))
        self.assertIn(path, self.store.data)
        self.store.data[path] = raw
        ready_path = 'repos/a/ci/jobs/' + req['job_id'] + '/request.ready'
        ready = self.store.data.pop(ready_path)
        worker.tick()
        self.assertIsNone(worker.ledger.get(req['job_id']))
        self.assertFalse(self.counter.exists())
        self.store.data[ready_path] = ready
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_failed_notification_upload_reuses_durable_job(self):
        worker = self.worker()
        _, pub, _ = self.ea.push('drive')
        client = CiClient(self.ea, 'drive')
        self.store.fail = 'inbox/'
        with self.assertRaises(GdiError):
            client.submit(pub, 'user-host')
        raw = next((client.root / 'outbox').glob('*.json')).read_bytes()
        req = decode(raw)
        self.assertIn('repos/a/ci/jobs/' + req['job_id'] + '/request.ready', self.store.data)
        self.store.fail = None
        self.assertEqual(client.submit(pub, 'user-host')['job_id'], req['job_id'])
        worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_restart_after_result_upload_failure_preserves_route_and_does_not_rerun(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.store.fail = 'repos/a/ci/jobs/' + req['job_id'] + '/result.json'
        worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'UPLOAD_PENDING')
        self.assertTrue(self.ci_events())
        config = worker.config
        worker.close(); self._cleanups.pop()
        self.store.fail = None
        resumed = self.worker(config)
        resumed.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        self.assertFalse(self.ci_events())

    def test_lost_acknowledgement_retries_deletion_without_execution(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.store.fail = 'delete-notification'
        worker.tick()
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'PUBLISHED')
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        self.assertTrue(self.ci_events())
        self.store.fail = None
        worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertFalse(self.ci_events())

    def test_fail_fix_pass_logs_artifacts_and_workflow_selection(self):
        worker = self.worker()
        self.commit(self.a, 'broken')
        client, req = self.submit(worker, workflow={'path': '.github/workflows/checks.yml', 'event': 'workflow_dispatch',
                                                  'inputs': {'MODE': 'full'}})
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'FAIL')
        log = client.root / 'results' / req['job_id'] / 'build.log'
        self.assertIn(b'broken', log.read_bytes())
        retry = client.retry(req['job_id'])
        self.assertEqual(retry['workflow'], req['workflow'])
        worker.tick()
        self.commit(self.a, 'fixed')
        client, good = self.submit(worker, workflow=req['workflow'])
        worker.tick()
        result = client.status(good['job_id'])
        self.assertEqual(result['state'], 'PASS')
        args = json.loads(self.arguments.read_text())
        self.assertEqual(args[0], 'workflow_dispatch')
        self.assertIn('MODE=full', args)
        archive = client.root / 'results' / good['job_id'] / 'artifacts/workflow-artifacts.zip'
        with zipfile.ZipFile(archive) as handle:
            self.assertEqual(handle.read('firmware.bin'), b'firmware artifact')
        self.assertEqual(CiClient(self.eb, 'drive').pull_passed(good['job_id'], 'full')['head'], good['head'])

    def test_no_completed_jobs_cannot_be_pass(self):
        worker = self.worker()
        self.commit(self.a, 'skip')
        client, req = self.submit(worker)
        worker.tick()
        result = client.status(req['job_id'])
        self.assertEqual(result['state'], 'ERROR')
        self.assertIn('without any successful jobs', result['detail'])

    def test_foreign_worker_and_mismatched_identity_do_not_execute(self):
        worker = self.worker()
        client, req = self.submit(worker)
        path, event = next(iter(self.ci_events().items()))
        del self.store.data[path]
        event['worker_id'] = 'other-worker'
        publish(self.factory('memory:hub'), event)
        worker.tick()
        self.assertFalse(self.counter.exists())
        event['worker_id'] = 'user-host'
        event['head'] = '1' * 40
        publish(self.factory('memory:hub'), event)
        worker.tick()
        self.assertFalse(self.counter.exists())
        self.assertIsNone(worker.ledger.get(req['job_id']))

    def test_unsafe_routes_and_global_repository_settings_are_rejected(self):
        for value in ('../other', '/other', 'repo/../other', 'repo//other', 'remote:path', 'inbox/repo', 'repo\\other'):
            with self.assertRaises(GdiError):
                relative_repository(value)
        path = self.root / 'worker.json'
        path.write_bytes(encode({'config_version': 2, 'worker_id': 'user-host', 'remote_url': 'memory:hub',
                                'repositories': []}))
        with self.assertRaisesRegex(GdiError, 'repositories/profiles'):
            load_config(path)

    def test_linked_worktrees_share_settings_and_the_lock(self):
        from gdi.git import Git
        linked = self.root / 'linked'
        self.a.call('worktree', 'add', '--detach', str(linked), 'HEAD')
        other = Git.discover(linked)
        self.assertEqual(other.gdi_dir(), self.a.gdi_dir())
        self.assertEqual(Exchange(other, self.factory).remote('drive'), self.ea.remote('drive'))
        with self.a.lock(), self.assertRaisesRegex(GdiError, 'another gdi command'):
            with other.lock():
                pass

    def test_git_configuration_is_unchanged_and_legacy_settings_are_ignored(self):
        git_config = self.a.path / '.git/config'
        before = git_config.read_bytes()
        self.ea.add('second', 'memory:hub/repos/a', inbox_root='memory:hub')
        self.ea.remove('second')
        self.assertEqual(git_config.read_bytes(), before)
        self.assertEqual(decode((self.a.path / '.gdi/config.json').read_bytes())['remotes']['drive']['inbox_root'], 'memory:hub')
        self.a.call('config', 'gdi.remote.old.url', 'memory:hub/repos/a')
        self.a.call('config', 'gdi.remote.old.repositoryid', self.identity)
        before = git_config.read_bytes()
        (self.a.path / '.gdi/config.json').unlink()
        with self.assertRaisesRegex(GdiError, 'missing remote configuration'):
            self.ea.remote('old')
        self.ea.add('new', 'memory:hub/repos/a', inbox_root='memory:hub')
        self.assertEqual(self.ea.remotes(), [('new', 'memory:hub/repos/a')])
        self.assertEqual(git_config.read_bytes(), before)

    def test_ci_migration_resumes_partial_tree_and_preserves_outbox_ids(self):
        worker = self.worker()
        client, req = self.submit(worker)
        old = self.a.common_dir() / 'gdi-ci' / self.identity
        old.parent.mkdir()
        shutil.move(client.root, old)
        (old / 'results').mkdir()
        (old / 'results/log').write_bytes(b'legacy result')
        original = shutil.copyfileobj
        calls = []
        def interrupted(source, target):
            calls.append(1)
            if len(calls) == 2:
                target.write(b'partial')
                raise OSError('simulated crash during migration')
            return original(source, target)
        with patch('gdi.ci.shutil.copyfileobj', side_effect=interrupted), self.assertRaises(OSError):
            CiClient(self.ea, 'drive')
        self.assertFalse((client.root / '.legacy-migrated').exists())
        resumed = CiClient(self.ea, 'drive')
        self.assertEqual(resumed.submit(req['publication_id'], 'user-host')['job_id'], req['job_id'])
        self.assertEqual((resumed.root / 'results/log').read_bytes(), b'legacy result')
        self.assertTrue((resumed.root / '.legacy-migrated').exists())
        self.assertTrue(old.exists())

    def test_ci_migration_conflicts_preserve_both_states(self):
        from gdi.ci import migrate_state
        old, target = self.root / 'legacy', self.root / 'current'
        old.mkdir(); target.mkdir()
        (old / 'request.json').write_bytes(b'original')
        (target / 'request.json').write_bytes(b'another job')
        with self.assertRaisesRegex(GdiError, 'conflicting legacy'):
            migrate_state(old, target)
        self.assertEqual((target / 'request.json').read_bytes(), b'another job')
        self.assertFalse((target / '.legacy-migrated').exists())

    def test_capabilities_corruption_and_network_failures_do_not_fall_back(self):
        worker = self.worker()
        _, pub, _ = self.ea.push('drive')
        client = CiClient(self.ea, 'drive')
        path = 'ci/workers/user-host/capabilities.json'
        self.store.data['repos/a/' + path] = encode({'ci_version': 1, 'worker_id': 'user-host',
                                                  'repositories': {self.identity: {'full': 'a' * 64}}})
        raw = self.store.data[path]
        self.store.data[path] = b'{broken'
        with self.assertRaisesRegex(GdiError, 'invalid metadata JSON'):
            client.submit(pub, 'user-host')
        self.store.data[path] = raw
        original = PrefixTransport.read
        def unavailable(transport, relative):
            if transport.prefix == '' and relative == path:
                raise OSError('connection reset')
            return original(transport, relative)
        with patch.object(PrefixTransport, 'read', unavailable), self.assertRaisesRegex(OSError, 'connection reset'):
            client.submit(pub, 'user-host')
        del self.store.data[path]
        request = client.submit(pub, 'user-host')
        self.assertEqual(request['ci_version'], 1)

    def test_invalid_selector_is_rejected_before_cli_push(self):
        from gdi.cli import main
        self.worker()
        for arguments in (['--workflow', '../outside.yml'], ['--event', 'bad event'],
                          ['--job', 'bad job'], ['--input', 'A=1', '--input', 'A=2'],
                          ['--workflow', '.github/workflows/missing.yml']):
            with self.subTest(arguments=arguments), patch('gdi.cli.Git.discover', return_value=self.a), \
                    patch('gdi.cli.Exchange', return_value=self.ea), \
                    patch.object(self.ea, 'push') as push, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['push', 'drive', '--ci', '--worker', 'user-host', '--json', *arguments]), 1)
                push.assert_not_called()

    def test_v2_revision_change_rejects_pending_job(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.config['execution_profile']['revision'] = 'a' * 64
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'REJECTED')
        self.assertFalse(self.counter.exists())

    def test_pending_route_survives_root_change_without_rerun(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.store.fail = 'repos/a/ci/jobs/' + req['job_id'] + '/result.json'
        worker.tick()
        self.store.fail = None
        config = worker.config
        worker.close(); self._cleanups.pop()
        config['remote_url'] = 'memory:hub-other'
        resumed = self.worker(config)
        with self.assertRaisesRegex(GdiError, 'original inbox root'):
            resumed.process(resumed.ledger.get(req['job_id']))
        self.assertEqual(self.counter.read_text(), 'x')
        config['remote_url'] = 'memory:hub'
        resumed.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        self.assertEqual(self.counter.read_text(), 'x')

    def test_v2_uncertain_execution_is_interrupted_without_rerun(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.discover()
        worker.ledger.update(req['job_id'], 'RUNNING')
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'INTERRUPTED')
        self.assertFalse(self.counter.exists())

    def test_workflow_path_and_missing_secrets_fail_without_execution(self):
        for kind in ('missing', 'symlink', 'secret'):
            with self.subTest(kind=kind):
                config = self.config()
                if kind == 'secret':
                    config['execution_profile']['workflow']['secrets'] = ['GDI_TEST_UNSET_SECRET']
                worker = self.worker(config)
                workflow = {'path': '.github/workflows/missing.yml'} if kind == 'missing' else None
                if kind == 'symlink':
                    path = self.a.path / '.github/workflows/checks.yml'
                    path.unlink()
                    path.symlink_to(self.root / 'outside.yml')
                    self.a.call('add', '.github')
                    self.commit(self.a, 'symlink')
                client, req = self.submit(worker, workflow=workflow)
                worker.tick()
                self.assertEqual(client.status(req['job_id'])['state'], 'ERROR')
                self.assertFalse(self.counter.exists())
                worker.close(); self._cleanups.pop()
                if kind == 'symlink':
                    path.unlink()
                    path.write_text('on: push\njobs: {}\n')
                    self.a.call('add', '.github')
                    self.commit(self.a, 'fixed')

    def test_github_namespace_is_pinned_and_retry_preserves_it(self):
        self.a.call('remote', 'add', 'origin', 'git@github.com:example/project.git')
        worker = self.worker()
        client, req = self.submit(worker)
        self.assertEqual(req['github_repository'], 'example/project')
        worker.tick()
        arguments = json.loads(self.arguments.read_text())
        self.assertIn('GITHUB_REPOSITORY=example/project', arguments)
        self.assertIn('GITHUB_REPOSITORY_OWNER=example', arguments)
        self.a.call('remote', 'set-url', 'origin', 'https://github.com/other/project.git')
        self.assertEqual(client.retry(req['job_id'])['github_repository'], 'example/project')

    def test_durable_result_before_ledger_update_is_delivered_without_execution(self):
        worker = self.worker()
        client, req = self.submit(worker)
        update = worker.ledger.update
        def crash(jid, state, process=None):
            if state == 'RESULT_READY':
                raise OSError('crash after result fsync')
            return update(jid, state, process)
        with patch.object(worker.ledger, 'update', side_effect=crash):
            worker.tick()
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'FINALIZING')
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        self.assertEqual(self.counter.read_text(), 'x')

    def test_ready_result_is_delivered_when_runner_is_unavailable(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.store.fail = 'repos/a/ci/jobs/' + req['job_id'] + '/result.json'
        worker.tick()
        self.store.fail = None
        with patch('gdi.runner.resolve_profile', side_effect=GdiError('act unavailable')):
            worker.run(once=True)
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        self.assertEqual(self.counter.read_text(), 'x')

    def test_publication_receipt_before_ack_survives_restart(self):
        worker = self.worker()
        _, pub, _ = self.ea.push('drive')
        self.store.fail = 'delete-notification'
        worker.tick()
        self.assertEqual(len(list((worker.root / 'notifications').iterdir())), 1)
        config = worker.config
        worker.close(); self._cleanups.pop()
        self.store.fail = None
        resumed = self.worker(config)
        with patch('gdi.worker.publication', side_effect=AssertionError('receipt should avoid replay')):
            resumed.tick()
        self.assertFalse(any(path.startswith('inbox/') for path in self.store.data))


@unittest.skipUnless(os.environ.get('GDI_TEST_ACT') and shutil.which('rclone'), 'set GDI_TEST_ACT to test real act and rclone')
class RealInboxTests(ExchangeTestCase):
    def test_real_rclone_and_act_fail_fix_pass(self):
        env = {'RCLONE_CONFIG': os.devnull, 'RCLONE_CONFIG_GDITEST_TYPE': 'local'}
        with patch.dict(os.environ, env):
            self.ea.remove('drive'); self.eb.remove('drive')
            self.ea, self.eb = Exchange(self.a), Exchange(self.b)
            root_url = 'gditest:' + str(self.root / 'drive')
            url = root_url + '/project'
            self.identity = self.ea.add('drive', url, initialize=True, inbox_root=root_url)
            self.eb.add('drive', url, inbox_root=root_url)
            workflow = self.a.path / '.github/workflows/ci.yml'
            workflow.parent.mkdir(parents=True)
            workflow.write_text("""name: Real workflow
on: [push, workflow_dispatch]
jobs:
  tests:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        variant: [one, two]
    steps:
      - uses: actions/checkout@v4
      - name: Check exact source
        run: python3 -c 'from pathlib import Path; assert Path("file.txt").read_text().strip() == "fixed"'
      - name: Optional failure
        continue-on-error: true
        run: exit 7
  finish:
    needs: tests
    runs-on: ubuntu-latest
    steps:
      - run: echo COMPLETE
""")
            self.a.call('add', '.github')
            self.commit(self.a, 'broken')
            config_path = self.root / 'worker.json'
            with socket.socket() as port:
                port.bind(('127.0.0.1', 0))
                artifact_port = port.getsockname()[1]
            config_path.write_bytes(encode({'config_version': 2, 'worker_id': 'user-host', 'remote_url': root_url,
                                           'state_dir': str(self.root / 'state'), 'cache_dir': str(self.root / 'cache'),
                                           'act_executable': os.environ['GDI_TEST_ACT'],
                                           'artifact_server_port': artifact_port,
                                           'platforms': {'ubuntu-latest': '-self-hosted'}, 'timeout_seconds': 60}))
            from gdi.worker import Worker
            worker = Worker(load_config(config_path))
            self.addCleanup(worker.close)
            worker.advertise()
            _, pub, _ = self.ea.push('drive')
            client = CiClient(self.ea, 'drive')
            bad = client.submit(pub, 'user-host')
            worker.tick()
            self.assertEqual(client.status(bad['job_id'])['state'], 'FAIL')
            self.commit(self.a, 'fixed')
            _, pub, _ = self.ea.push('drive')
            good = client.submit(pub, 'user-host')
            worker.tick()
            result = client.status(good['job_id'])
            log = (client.root / 'results' / good['job_id'] / 'build.log').read_text()
            self.assertEqual(result['state'], 'PASS', log + result['detail'])
            self.assertIn('COMPLETE', log)
            self.assertEqual(CiClient(self.eb, 'drive').pull_passed(good['job_id'], 'full')['head'], good['head'])
            self.assertEqual(list((self.root / 'drive/inbox').iterdir()), [])
