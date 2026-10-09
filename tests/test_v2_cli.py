"""Real CLI fixtures; Docker and live Drive require explicit opt-in roots/tools."""

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from gdi.exchange import encode


class V2CliFixture(unittest.TestCase):
    platform = '-self-hosted'
    docker = False

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gdi-v2-cli-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agent = self.root / 'agent'
        self.user = self.root / 'user'
        self.agent.mkdir()
        self.env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                    'PYTHONDONTWRITEBYTECODE': '1', 'RCLONE_CONFIG': os.devnull,
                    'RCLONE_CONFIG_GDITEST_TYPE': 'local', 'GDI_FIXTURE_SECRET': 'fixture-value'}
        self.remote_root = 'gditest:' + str(self.root / 'drive')
        live = os.environ.get('GDI_TEST_DRIVE_ROOT')
        self.live_drive = bool(live)
        if live:
            # The operator grants a dedicated root; preserve it for inspection.
            self.remote_root = live.rstrip('/') + '/' + self.root.name
            self.env.pop('RCLONE_CONFIG')
        self.command(['git', 'init', '-q', '-b', 'main'])
        workflow = self.agent / '.github/workflows/ci.yml'
        workflow.parent.mkdir(parents=True)
        uploads = '''
      - uses: actions/upload-artifact@v3
        with:
          name: v3-${{ matrix.variant }}
          path: file.txt
      - uses: actions/upload-artifact@v4
        with:
          name: v4-${{ matrix.variant }}
          path: file.txt
''' if self.docker else ''
        workflow.write_text('''name: GDI isolated fixture
on: [push, workflow_dispatch]
jobs:
  tests:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        variant: [one, two]
    steps:
      - uses: actions/checkout@v4
      - name: Exact source and GitHub context
        env:
          REPOSITORY: ${{ github.repository }}
          OWNER: ${{ github.repository_owner }}
          SECRET: ${{ secrets.GDI_FIXTURE_SECRET }}
        run: |
          echo EARLY-CONSOLE-${{ matrix.variant }}
          test "$REPOSITORY" = example/fixture
          test "$OWNER" = example
          test "$SECRET" = fixture-value
          test "$(cat file.txt)" = fixed
      - name: Optional failure
        continue-on-error: true
        run: exit 7
''' + uploads + '''
  finish:
    needs: tests
    runs-on: ubuntu-latest
    steps:
      - run: echo COMPLETE
''')
        self.command(['git', 'remote', 'add', 'origin', 'git@github.com:example/fixture.git'])
        self.commit('broken', add=['.github', 'file.txt'])
        self.command(['git', 'clone', '-q', str(self.agent), str(self.user)])
        url = self.remote_root + '/project'
        self.gdi('remote', 'add', 'drive', url, '--init', '--inbox-root', self.remote_root)
        self.gdi('remote', 'add', 'drive', url, '--inbox-root', self.remote_root, cwd=self.user)
        with socket.socket() as port:
            port.bind(('127.0.0.1', 0))
            number = port.getsockname()[1]
        self.config = self.root / 'worker.json'
        self.config_value = {'config_version': 2, 'worker_id': 'fixture-worker', 'remote_url': self.remote_root,
                            'state_dir': str(self.root / 'state'), 'cache_dir': str(self.root / 'cache'),
                            'act_executable': os.environ['GDI_TEST_ACT'], 'act_version': '0.2.89',
                            'platforms': {'ubuntu-latest': self.platform}, 'secret_names': ['GDI_FIXTURE_SECRET'],
                            'artifact_server_port': number, 'poll_active_seconds': .2,
                            'poll_idle_max_seconds': .5, 'timeout_seconds': 600 if self.docker else 60}
        self.config.write_bytes(encode(self.config_value))
        self.console = (self.root / 'worker.log').open('w+')
        self.addCleanup(self.console.close)
        self.worker = None
        self.addCleanup(self.stop_worker)
        self.start_worker()

    def command(self, argv, *, cwd=None, ok=(0,), timeout=90):
        if self.live_drive or self.docker:
            timeout = max(timeout, 900)
        result = subprocess.run(argv, cwd=cwd or self.agent, env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        self.assertIn(result.returncode, ok, result.stdout + result.stderr)
        return result

    def gdi(self, *args, cwd=None, ok=(0,), timeout=90):
        if (self.live_drive or self.docker) and args[:2] == ('ci', 'wait') and '--timeout' in args:
            args = list(args)
            args[args.index('--timeout') + 1] = '720'
        return self.command([sys.executable, '-m', 'gdi', *args], cwd=cwd, ok=ok, timeout=timeout)

    def commit(self, content, *, add=None):
        (self.agent / 'file.txt').write_text(content + '\n')
        self.command(['git', 'add', *(add or ['file.txt'])])
        self.command(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                      'commit', '-qm', content])
        return self.command(['git', 'rev-parse', 'HEAD']).stdout.strip()

    def start_worker(self):
        capabilities = self.root / 'drive/ci/workers/fixture-worker/capabilities.json'
        before = capabilities.stat().st_mtime_ns if capabilities.exists() else None
        self.worker = subprocess.Popen([sys.executable, '-m', 'gdi', 'worker', 'run', '--config', str(self.config)],
                         cwd=self.root, env=self.env, stdout=self.console, stderr=self.console)
        if self.remote_root.startswith('gditest:'):
            self.await_condition(lambda: capabilities.exists() and capabilities.stat().st_mtime_ns != before)
        else:
            self.await_condition(lambda: self.command(['rclone', 'cat', self.remote_root + '/ci/workers/fixture-worker/capabilities.json',
                                 '--retries', '1', '--low-level-retries', '1'], ok=(0, 1, 3, 4, 5, 7)).returncode == 0)

    def await_condition(self, condition, timeout=30):
        if self.live_drive:
            timeout = max(timeout, 120)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if self.worker.poll() is not None:
                break
            time.sleep(.1)
        self.console.flush(); self.console.seek(0)
        self.fail('fixture worker did not reach expected state: ' + self.console.read())

    def stop_worker(self):
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.worker.kill(); self.worker.wait(timeout=5)

    def cycle(self):
        bad = json.loads(self.gdi('push', 'drive', '--ci', '--worker', 'fixture-worker', '--json').stdout)
        first = self.gdi('ci', 'wait', 'drive', bad['job_id'], '--follow', '--timeout', '60', '--json', ok=(1,))
        self.assertEqual(json.loads(first.stdout)['state'], 'FAIL')
        self.assertIn('EARLY-CONSOLE', first.stderr)
        fixed = self.commit('fixed')
        good = json.loads(self.gdi('push', 'drive', '--ci', '--worker', 'fixture-worker', '--json').stdout)
        result = json.loads(self.gdi('ci', 'wait', 'drive', good['job_id'], '--follow', '--timeout', '60', '--json').stdout)
        self.assertEqual(result['state'], 'PASS')
        self.assertTrue(result['verified'])
        self.assertEqual(result['head'], fixed)
        self.assertIsNone(self.worker.poll())
        log = self.root / 'downloaded.log'
        self.gdi('ci', 'logs', 'drive', good['job_id'], '--output', str(log))
        self.assertIn('COMPLETE', log.read_text())
        environment = next((self.agent / '.gdi/ci').glob('*/results/' + good['job_id'] + '/artifacts/environment.json'))
        self.assertEqual(json.loads(environment.read_text())['act_version'], '0.2.89')
        self.assertNotIn('fixture-value', environment.read_text())
        self.commit('unverified')
        self.gdi('push', 'drive')
        self.gdi('pull', 'drive', '--passed', '--job', good['job_id'], '--profile', 'full', cwd=self.user)
        self.assertEqual(self.command(['git', 'rev-parse', 'HEAD'], cwd=self.user).stdout.strip(), fixed)
        if self.docker:
            archive = next((self.agent / '.gdi/ci').glob('*/results/' + good['job_id'] + '/artifacts/workflow-artifacts.zip'))
            self.assertGreater(archive.stat().st_size, 0)

    def interrupt(self, sig):
        workflow = self.agent / '.github/workflows/ci.yml'
        workflow.write_text('''name: Interrupted fixture
on: push
jobs:
  slow:
    runs-on: ubuntu-latest
    steps:
      - run: echo STARTED; sleep 300
''')
        self.commit('slow', add=['.github', 'file.txt'])
        req = json.loads(self.gdi('push', 'drive', '--ci', '--worker', 'fixture-worker', '--json').stdout)
        spool = self.root / 'state/fixture-worker/jobs' / req['job_id']
        self.await_condition(lambda: self.output_count(spool / 'build.log', 'STARTED') == 1)
        self.worker.send_signal(sig)
        self.worker.wait(timeout=10)
        self.start_worker()
        value = json.loads(self.gdi('ci', 'wait', 'drive', req['job_id'], '--timeout', '30', '--json', ok=(1,)).stdout)
        self.assertEqual(value['state'], 'INTERRUPTED')
        self.assertEqual(self.output_count(spool / 'build.log', 'STARTED'), 1)

    def output_count(self, path, message):
        if not path.exists():
            return 0
        count = 0
        for line in path.read_bytes().splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get('raw_output') and value.get('msg', '').strip() == message:
                count += 1
        return count

    def slow_job(self, seconds):
        workflow = self.agent / '.github/workflows/ci.yml'
        workflow.write_text('''name: Slow fixture
on: push
jobs:
  slow:
    runs-on: ubuntu-latest
    steps:
      - run: echo STARTED; sleep ''' + str(seconds) + '\n')
        self.commit('slow', add=['.github', 'file.txt'])
        req = json.loads(self.gdi('push', 'drive', '--ci', '--worker', 'fixture-worker', '--json').stdout)
        return req, self.root / 'state/fixture-worker/jobs' / req['job_id']


@unittest.skipUnless(os.environ.get('GDI_TEST_ACT') and shutil.which('rclone'), 'requires GDI_TEST_ACT and rclone')
class LocalV2CliTests(V2CliFixture):
    def test_fail_fix_pass_and_pull_via_cli(self):
        self.cycle()

    def test_sigkill_restart_does_not_execute_job_again(self):
        self.interrupt(signal.SIGKILL)

    def test_timeout_stops_act_and_worker_keeps_running(self):
        self.stop_worker()
        self.config_value['timeout_seconds'] = 1
        self.config.write_bytes(encode(self.config_value))
        self.start_worker()
        req, spool = self.slow_job(30)
        value = json.loads(self.gdi('ci', 'wait', 'drive', req['job_id'], '--timeout', '30', '--json', ok=(1,)).stdout)
        self.assertEqual(value['state'], 'TIMEOUT')
        self.assertIsNone(self.worker.poll())

    def test_sigint_finishes_current_job_then_exits_without_rerun(self):
        req, spool = self.slow_job(2)
        self.await_condition(lambda: self.output_count(spool / 'build.log', 'STARTED') == 1)
        self.worker.send_signal(signal.SIGINT)
        self.worker.wait(timeout=15)
        result = json.loads(self.gdi('ci', 'status', 'drive', req['job_id'], '--json').stdout)
        self.assertEqual(result['state'], 'PASS')
        self.assertTrue(result['verified'])
        self.start_worker()
        self.assertEqual(self.output_count(spool / 'build.log', 'STARTED'), 1)


@unittest.skipUnless(os.environ.get('GDI_TEST_DOCKER') and os.environ.get('GDI_TEST_ACT'), 'set GDI_TEST_DOCKER to a pulled image')
class DockerV2CliTests(V2CliFixture):
    docker = True

    def setUp(self):
        self.platform = os.environ['GDI_TEST_DOCKER']
        super().setUp()

    def test_matrix_secrets_external_actions_and_artifacts(self):
        self.cycle()

    def test_sigkill_cleans_owned_containers_before_result(self):
        self.interrupt(signal.SIGKILL)
        self.assert_owned_containers_removed(self.root / 'state/fixture-worker/jobs')

    def assert_owned_containers_removed(self, jobs):
        owned = {str((spool / 'checkout').resolve()) for spool in jobs.iterdir()}
        ids = self.command(['docker', 'ps', '--all', '--quiet', '--filter', 'name=act-']).stdout.splitlines()
        for cid in ids:
            workdir = self.command(['docker', 'inspect', '--format', '{{.Config.WorkingDir}}', cid]).stdout.strip()
            self.assertNotIn(workdir, owned, 'owned act container remains after terminal result')


class ServiceProcess:
    def poll(self):
        result = subprocess.run(['systemctl', '--user', 'is-active', 'gdi-worker.service'],
                                capture_output=True, text=True, timeout=15)
        return None if result.stdout.strip() == 'active' else result.returncode or 1


@unittest.skipUnless(os.environ.get('GDI_TEST_SERVICE_CONFIG') and os.environ.get('GDI_TEST_ACT'),
                     'requires an explicitly configured acceptance user service')
class ServiceV2CliTests(V2CliFixture):
    """Opt-in: exercise the installed service, retaining its dedicated Drive root/state."""

    docker = True
    assert_owned_containers_removed = DockerV2CliTests.assert_owned_containers_removed

    def setUp(self):
        from gdi.worker_config import load_config
        self.service_config = load_config(os.environ['GDI_TEST_SERVICE_CONFIG'])
        self.assertEqual(self.service_config['config_version'], 2)
        self.platform = self.service_config['platforms']['ubuntu-latest']
        V2CliFixture.setUp(self)

    def command(self, argv, **kwargs):
        self.remote_root = self.service_config['remote_url']
        self.live_drive = True
        self.env.pop('RCLONE_CONFIG', None)
        return super().command(argv, **kwargs)

    def await_condition(self, condition, timeout=300):
        return super().await_condition(condition, timeout=timeout)

    def gdi(self, *args, **kwargs):
        args = [self.service_config['worker_id'] if value == 'fixture-worker' else value for value in args]
        if args[:3] == ['remote', 'add', 'drive']:
            args[3] = self.remote_root + '/projects/' + self.root.name
        return super().gdi(*args, **kwargs)

    def start_worker(self):
        self.worker = ServiceProcess()
        self.assertIsNone(self.worker.poll(), 'acceptance user service is not active')
        path = self.remote_root + '/ci/workers/' + self.service_config['worker_id'] + '/capabilities.json'
        self.await_condition(lambda: self.command(['rclone', 'cat', path, '--retries', '1',
                              '--low-level-retries', '1'], ok=(0, 1, 3, 4, 5, 7)).returncode == 0)

    def stop_worker(self):
        # The operator keeps the permanent service running after these fixtures.
        pass

    def slow_job(self, seconds):
        req, _ = super().slow_job(seconds)
        return req, self.service_jobs() / req['job_id']

    def service_jobs(self):
        return Path(self.service_config['state_dir']) / self.service_config['worker_id'] / 'jobs'

    def test_matrix_secrets_external_actions_and_artifacts(self):
        self.cycle()

    def test_sigkill_cleans_owned_containers_before_result(self):
        req, spool = self.slow_job(300)
        self.await_condition(lambda: self.output_count(spool / 'build.log', 'STARTED') == 1)
        self.command(['systemctl', '--user', 'kill', '--kill-whom=all', '--signal=SIGKILL', 'gdi-worker.service'])
        self.command(['systemctl', '--user', 'restart', 'gdi-worker.service'])
        self.start_worker()
        result = json.loads(self.gdi('ci', 'wait', 'drive', req['job_id'], '--timeout', '30', '--json', ok=(1,)).stdout)
        self.assertEqual(result['state'], 'INTERRUPTED')
        self.assertTrue(result['verified'])
        self.assertEqual(self.output_count(spool / 'build.log', 'STARTED'), 1)
        self.assert_owned_containers_removed(self.service_jobs())

    def test_stop_during_job_finishes_then_restart_without_rerun(self):
        req, spool = self.slow_job(15)
        self.await_condition(lambda: self.output_count(spool / 'build.log', 'STARTED') == 1)
        self.command(['systemctl', '--user', 'stop', 'gdi-worker.service'], timeout=960)
        self.assertIsNotNone(self.worker.poll())
        self.command(['systemctl', '--user', 'start', 'gdi-worker.service'])
        self.start_worker()
        result = json.loads(self.gdi('ci', 'wait', 'drive', req['job_id'], '--timeout', '30', '--json').stdout)
        self.assertEqual(result['state'], 'PASS')
        self.assertTrue(result['verified'])
        self.assertEqual(self.output_count(spool / 'build.log', 'STARTED'), 1)
