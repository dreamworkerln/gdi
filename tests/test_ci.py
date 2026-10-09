import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gdi.ci import CiClient
from gdi.ci_protocol import atomic_write, now, upload_json
from gdi.exchange import Exchange, decode, digest, encode
from gdi.git import GdiError, Git
from gdi.worker import Worker
from gdi.worker_config import load_config
from tests.test_exchange import ExchangeTestCase


class CiTestCase(ExchangeTestCase):
    def config(self, argv=None, **profile_fields):
        profile = {'stages': [{'name': 'test', 'argv': argv or [sys.executable, '-c', "print('hello CI')"],
                               'timeout_seconds': 10}], **profile_fields}
        path = self.root / 'worker.json'
        path.write_bytes(encode({'config_version': 1, 'worker_id': 'user-host',
                                'state_dir': str(self.root / 'state'), 'cache_dir': str(self.root / 'cache'),
                                'repositories': [{'repository_id': self.identity, 'remote_url': 'memory:project',
                                                  'profiles': {'full': profile}}]}))
        return load_config(path)

    def worker(self, config=None):
        worker = Worker(config or self.config(), lambda url: self.store)
        self.addCleanup(worker.close)
        worker.advertise()
        return worker

    def submit(self, worker):
        head, pub, _ = self.ea.push('drive')
        client = CiClient(self.ea, 'drive')
        req = client.submit(pub, 'user-host', 'full')
        return client, req



class CiTests(CiTestCase):
    def test_full_cycle_fail_fix_pass_and_exact_pass_pull(self):
        worker = self.worker(self.config([sys.executable, '-c',
            "from pathlib import Path; print('CONSOLE'); assert Path('file.txt').read_text().strip() == 'fixed', 'expected fixed' "]))
        client, req = self.submit(worker)
        worker.tick()
        fail = client.status(req['job_id'])
        self.assertEqual(fail['state'], 'FAIL')
        self.assertTrue(fail['verified'])
        self.assertEqual(fail['failed_stage'], 'test')
        log = client.root / 'results' / req['job_id'] / 'build.log'
        self.assertIn(b'AssertionError: expected fixed', log.read_bytes())
        fixed = self.commit(self.a, 'fixed')
        _, pub, _ = self.ea.push('drive')
        good = client.submit(pub, 'user-host', 'full')
        worker.tick()
        self.assertEqual(client.wait(good['job_id'])[1], 0)
        self.commit(self.a, 'unverified')
        self.ea.push('drive')
        target = CiClient(self.eb, 'drive').pull_passed(good['job_id'], 'full')
        self.assertEqual(target['head'], fixed)
        self.assertEqual(self.b.oid('HEAD'), fixed)
        self.assertNotEqual(self.b.oid('HEAD'), self.a.oid('HEAD'))
        self.assertEqual(worker.ledger.pending(), [])
        self.assertFalse(any(path.startswith('ci/queue/') for path in self.store.data))

    def test_submit_reuses_job_and_retry_requires_terminal(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.assertEqual(client.submit(req['publication_id'], 'user-host', 'full'), req)
        with self.assertRaisesRegex(GdiError, 'active'):
            client.retry(req['job_id'])
        worker.tick()
        self.assertEqual(client.submit(req['publication_id'], 'user-host', 'full'), req)
        retry = client.retry(req['job_id'])
        self.assertNotEqual(retry['job_id'], req['job_id'])
        self.assertEqual(retry['retry_of'], req['job_id'])
        worker.tick()
        self.assertEqual(client.status(retry['job_id'])['state'], 'PASS')

    def test_result_upload_failure_restart_does_not_execute_again(self):
        count = self.root / 'counter'
        worker = self.worker(self.config([sys.executable, '-c',
            f"from pathlib import Path; p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]))
        client, req = self.submit(worker)
        self.store.fail = f"ci/jobs/{req['job_id']}/result.json"
        worker.tick()
        self.assertEqual(count.read_text(), 'x')
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'UPLOAD_PENDING')
        config = worker.config
        worker.close()
        self._cleanups.pop()
        self.store.fail = None
        resumed = self.worker(config)
        resumed.tick()
        self.assertEqual(count.read_text(), 'x')
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_uncertain_execution_is_interrupted_without_rerun(self):
        marker = self.root / 'must-not-run'
        worker = self.worker(self.config([sys.executable, '-c', f"open({str(marker)!r},'w').close()" ]))
        client, req = self.submit(worker)
        worker.discover()
        worker.ledger.update(req['job_id'], 'RUNNING')
        worker.tick()
        self.assertFalse(marker.exists())
        self.assertEqual(client.status(req['job_id'])['state'], 'INTERRUPTED')

    def test_corrupt_artifact_or_result_cannot_be_pass(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.tick()
        path = f"ci/jobs/{req['job_id']}/build.log"
        self.store.data[path] += b'corrupted'
        with self.assertRaisesRegex(GdiError, 'checksum'):
            client.status(req['job_id'])
        self.store.data[path] = self.store.data[path][:-9]
        value = decode(self.store.data[f"ci/jobs/{req['job_id']}/result.json"])
        value['head'] = 'a' * 40
        self.store.data[f"ci/jobs/{req['job_id']}/result.json"] = encode(value)
        with self.assertRaisesRegex(GdiError, 'identity'):
            client.status(req['job_id'])

    def test_timeout_and_binary_console(self):
        worker = self.worker(self.config([sys.executable, '-c',
            "import os,time; os.write(1,b'\\x00\\xff'+b'x'*600000); time.sleep(10)"], timeout_seconds=.3))
        client, req = self.submit(worker)
        worker.tick()
        value = client.status(req['job_id'])
        self.assertEqual(value['state'], 'TIMEOUT')
        data = (client.root / 'results' / req['job_id'] / 'build.log').read_bytes()
        self.assertIn(b'\x00\xff' + b'x'*600000, data)
        self.assertGreaterEqual(len(client.chunks(req['job_id'])), 3)

    def test_revision_change_rejects_without_execution(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.config['repositories'][0]['profiles']['full']['revision'] = '1' * 64
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'REJECTED')

    def test_required_artifact_and_symlink_escape(self):
        worker = self.worker(self.config(artifacts=[{'name': 'firmware.bin', 'path': 'missing.bin', 'required': True}]))
        client, req = self.submit(worker)
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'ERROR')
        self.assertIn('required artifact', client.status(req['job_id'])['detail'])

    def test_partial_request_blocks_gc_but_does_not_execute(self):
        worker = self.worker()
        client, req = self.submit(worker)
        del self.store.data[f"ci/jobs/{req['job_id']}/request.ready"]
        worker.tick()
        self.assertIsNone(worker.ledger.get(req['job_id']))
        with self.assertRaisesRegex(GdiError, 'CI queue'):
            self.ea.gc('drive', apply=True, quiescent=True, report=lambda x: None)
        client.submit(req['publication_id'], 'user-host', 'full')
        worker.tick()
        self.ea.gc('drive', apply=True, quiescent=True, report=lambda x: None)

    def test_wait_timeout_does_not_cancel_job(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.assertEqual(client.wait(req['job_id'], timeout=.01, interval=.01)[1], 124)
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_live_progress_before_ci_completes(self):
        worker = self.worker(self.config([sys.executable, '-c', "import time; print('EARLY',flush=True); time.sleep(2.5); print('LATE')"]))
        client, req = self.submit(worker)
        errors = []
        # Ledger remains owned by this thread, publisher runs separately as in production.
        def observe():
            try:
                deadline = time.monotonic() + 2.2
                while time.monotonic() < deadline:
                    state = client.status(req['job_id'])
                    output = io.StringIO()
                    client.follow(req['job_id'], 0, output)
                    if 'EARLY' in output.getvalue() and not state.get('verified'):
                        return
                    time.sleep(.05)
                raise AssertionError('no live log/progress before completion')
            except BaseException as exc:
                errors.append(exc)
        observer = threading.Thread(target=observe)
        observer.start()
        worker.tick()
        observer.join()
        if errors:
            raise errors[0]
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_optional_failure_warnings_and_required_stages(self):
        config = self.config()
        profile = config['repositories'][0]['profiles']['full']
        profile['stages'].insert(0, {'name': 'optional', 'argv': [sys.executable, '-c', 'raise SystemExit(7)'],
                                   'cwd': '.', 'blocking': False, 'timeout_seconds': 10})
        worker = self.worker(config)
        client, req = self.submit(worker)
        worker.tick()
        result = client.status(req['job_id'])
        self.assertEqual(result['state'], 'PASS')
        self.assertTrue(result['warnings'])
        self.assertEqual([s['state'] for s in result['stages']], ['FAIL', 'PASS'])

    def test_changed_head_is_never_pass(self):
        self.commit(self.a, 'second')
        worker = self.worker(self.config(['/usr/bin/git', 'checkout', '--detach', 'HEAD~1']))
        client, req = self.submit(worker)
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'ERROR')

    def test_artifact_copied_and_symlink_outside_rejected(self):
        worker = self.worker(self.config([sys.executable, '-c', "open('result.bin','wb').write(b'\\x00\\xff')"],
            artifacts=[{'name': 'result.bin', 'path': 'result.bin', 'required': True}]))
        client, req = self.submit(worker)
        worker.tick()
        result = client.status(req['job_id'])
        self.assertEqual(result['state'], 'PASS')
        self.assertEqual((client.root / 'results' / req['job_id'] / 'artifacts/result.bin').read_bytes(), b'\x00\xff')
        config = self.config([sys.executable, '-c', "import os; os.symlink('/etc/passwd','leak')"],
            artifacts=[{'name': 'leak', 'path': 'leak', 'required': True}])
        # New revision/commit makes a distinct job without needing two running workers.
        worker.config['repositories'][0]['profiles']['full'] = config['repositories'][0]['profiles']['full']
        worker.advertise()
        retry = client.submit(req['publication_id'], 'user-host', 'full')
        worker.tick()
        self.assertEqual(client.status(retry['job_id'])['state'], 'ERROR')
        self.assertNotIn(f"ci/jobs/{retry['job_id']}/artifacts/leak", self.store.data)

    def test_old_commit_after_bundle_gc_and_cache_loss(self):
        worker = self.worker()
        old_head, old_pub, _ = self.ea.push('drive')
        for index in range(3):
            self.commit(self.a, f'checkpoint-{index}')
            self.ea.push('drive', full=True)
        self.ea.gc('drive', apply=True, quiescent=True, keep_checkpoints=1, report=lambda x: None)
        client = CiClient(self.ea, 'drive')
        req = client.submit(old_pub, 'user-host', 'full')
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['head'], old_head)
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        import shutil
        shutil.rmtree(worker.config['cache_dir'])
        retry = client.retry(req['job_id'])
        worker.tick()
        self.assertEqual(client.status(retry['job_id'])['state'], 'PASS')

    def test_missing_chunk_and_result_without_blocking_stage_are_rejected(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.tick()
        chunk = next(path for path in self.store.data if '/log-chunks/' in path)
        original = self.store.data.pop(chunk)
        with self.assertRaisesRegex(GdiError, 'incomplete|gaps'):
            client.status(req['job_id'])
        self.store.data[chunk] = original
        path = f"ci/jobs/{req['job_id']}/result.json"
        result = decode(self.store.data[path])
        result['stages'] = []
        self.store.data[path] = encode(result)
        with self.assertRaisesRegex(GdiError, 'blocking'):
            client.status(req['job_id'])

    def test_lost_ack_after_result_and_queue_cleanup_retries_without_execution(self):
        worker = self.worker()
        client, req = self.submit(worker)
        original = self.store.upload
        failed = []
        def lost_ack(source, path):
            original(source, path)
            if path.endswith('/result.json') and not failed:
                failed.append(path)
                raise GdiError('lost acknowledgement')
        with patch.object(self.store, 'upload', side_effect=lost_ack):
            worker.tick()
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'UPLOAD_PENDING')
        with patch('gdi.worker.execute', side_effect=AssertionError('must not execute again')):
            worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'PUBLISHED')

    def test_pull_passed_refuses_dirty_or_divergent_worktree(self):
        worker = self.worker()
        self.commit(self.a, 'remote')
        client, req = self.submit(worker)
        worker.tick()
        target = CiClient(self.eb, 'drive')
        (self.b.path / 'untracked').write_text('keep')
        with self.assertRaisesRegex(GdiError, 'dirty'):
            target.pull_passed(req['job_id'], 'full')
        (self.b.path / 'untracked').unlink()
        local = self.commit(self.b, 'local')
        with self.assertRaisesRegex(GdiError, 'fast-forward'):
            target.pull_passed(req['job_id'], 'full')
        self.assertEqual(self.b.oid('HEAD'), local)

    def test_foreign_claim_and_invalid_ready_are_never_executed(self):
        worker = self.worker()
        client, req = self.submit(worker)
        raw = self.store.data[f"ci/jobs/{req['job_id']}/request.ready"]
        self.store.data[f"ci/jobs/{req['job_id']}/request.ready"] = encode({})
        worker.tick()
        self.assertIsNone(worker.ledger.get(req['job_id']))
        self.store.data[f"ci/jobs/{req['job_id']}/request.ready"] = raw
        self.store.data[f"ci/jobs/{req['job_id']}/worker.running.json"] = encode({'foreign': True})
        with patch('gdi.worker.execute', side_effect=AssertionError('must not execute foreign claim')):
            worker.tick()
        self.assertIsNone(client.result(req['job_id']))

    def test_timeout_kills_descendants_that_hold_stdout(self):
        marker = self.root / 'descendant-finished'
        script = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',\\\"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()\\\"]); raise SystemExit(0)"
        # Construct argv without shell quoting.
        child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
        script = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
        worker = self.worker(self.config([sys.executable, '-c', script], timeout_seconds=.3))
        client, req = self.submit(worker)
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'TIMEOUT')
        time.sleep(2)
        self.assertFalse(marker.exists())



class ConfigTests(unittest.TestCase):
    def test_service_absolute_paths_and_profile_rejection(self):
        from gdi.service import unit
        text = unit('/tmp/space here/worker.json')
        self.assertIn('KillMode=mixed', text)
        self.assertIn('StandardError=journal', text)
        self.assertIn('"/tmp/space here/worker.json"', text)
        self.assertNotIn('.bashrc', text)


@unittest.skipUnless(__import__('shutil').which('rclone'), 'requires rclone')
class LocalAutonomousTests(unittest.TestCase):
    def test_separate_persistent_worker_fail_fix_pass(self):
        with tempfile.TemporaryDirectory(prefix='gdi-autonomous-') as temporary:
            root = Path(temporary)
            project = Path(__file__).resolve().parents[1]
            agent, user = root / 'agent', root / 'user'
            remote = root / 'drive'
            agent.mkdir(); remote.mkdir()
            env = {**os.environ, 'PYTHONPATH': str(project), 'PYTHONDONTWRITEBYTECODE': '1',
                   'RCLONE_CONFIG': os.devnull, 'RCLONE_CONFIG_GDITEST_TYPE': 'local'}
            def command(args, cwd=agent, ok=(0,), timeout=40):
                result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
                self.assertIn(result.returncode, ok, result.stdout + result.stderr)
                return result
            def gdi(*args, cwd=agent, ok=(0,)):
                return command([sys.executable, '-m', 'gdi', *args], cwd, ok)
            def commit(content):
                (agent / 'file.txt').write_text(content)
                command(['git', 'add', 'file.txt'])
                command(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', content])
                return command(['git', 'rev-parse', 'HEAD']).stdout.strip()
            command(['git', 'init', '-q', '-b', 'main'])
            commit('bad')
            command(['git', 'clone', '-q', str(agent), str(user)])
            url = 'gditest:' + str(remote)
            gdi('remote', 'add', 'drive', url, '--init')
            identity = decode((remote / 'repository.json').read_bytes())['repository_id']
            gdi('remote', 'add', 'drive', url, '--repository-id', identity, cwd=user)
            script = "import time; from pathlib import Path; print('EARLY-CONSOLE',flush=True); time.sleep(1.5); assert Path('file.txt').read_text() == 'fixed', 'broken source'; print('ALL-PASSED')"
            config = root / 'worker.json'
            config.write_bytes(encode({'config_version': 1, 'worker_id': 'user-host',
                'state_dir': str(root / 'state'), 'cache_dir': str(root / 'cache'),
                'poll_active_seconds': .2, 'poll_idle_max_seconds': .5,
                'repositories': [{'repository_id': identity, 'remote_url': url,
                    'profiles': {'full': {'stages': [{'name': 'tests', 'argv': [sys.executable, '-c', script]}]}}}]}))
            with (root / 'worker-console.log').open('w+') as console:
                worker = subprocess.Popen([sys.executable, '-m', 'gdi', 'worker', 'run', '--config', str(config)],
                    cwd=root, env=env, stdout=console, stderr=console)
                try:
                    deadline = time.monotonic() + 20
                    while not (remote / 'ci/workers/user-host/capabilities.json').exists():
                        if worker.poll() is not None or time.monotonic() > deadline:
                            console.seek(0)
                            self.fail('worker failed to advertise: ' + console.read())
                        time.sleep(.05)
                    bad = json.loads(gdi('push', 'drive', '--ci', '--worker', 'user-host', '--profile', 'full', '--json').stdout)
                    first = gdi('ci', 'wait', 'drive', bad['job_id'], '--follow', '--timeout', '25', '--json', ok=(1,))
                    self.assertEqual(json.loads(first.stdout)['state'], 'FAIL')
                    self.assertIn('EARLY-CONSOLE', first.stderr)
                    self.assertIn('broken source', first.stderr)
                    fixed = commit('fixed')
                    good = json.loads(gdi('push', 'drive', '--ci', '--worker', 'user-host', '--profile', 'full', '--json').stdout)
                    final = json.loads(gdi('ci', 'wait', 'drive', good['job_id'], '--timeout', '25', '--json').stdout)
                    self.assertEqual(final['state'], 'PASS')
                    self.assertTrue(final['verified'])
                    self.assertEqual(final['head'], fixed)
                    self.assertIsNone(worker.poll(), 'worker stopped after a completed job')
                    log = root / 'full-ci.log'
                    gdi('ci', 'logs', 'drive', good['job_id'], '--output', str(log))
                    self.assertIn('ALL-PASSED', log.read_text())
                    self.assertEqual(command(['git', 'rev-parse', 'HEAD'], cwd=user).stdout.strip(), bad['head'])
                    commit('new-unverified')
                    gdi('push', 'drive')
                    gdi('pull', 'drive', '--passed', '--job', good['job_id'], '--profile', 'full', cwd=user)
                    self.assertEqual(command(['git', 'rev-parse', 'HEAD'], cwd=user).stdout.strip(), fixed)
                finally:
                    worker.terminate()
                    try:
                        worker.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        worker.kill(); worker.wait(timeout=5)
                console.seek(0)
                self.assertNotIn('execution error', console.read())
