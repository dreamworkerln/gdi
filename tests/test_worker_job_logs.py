"""Local job console selection and following require no Git, Drive, or daemon."""

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest

from gdi.ledger import Ledger


class WorkerJobLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-local-job-logs-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.spool = self.root / 'state/fixture'
        self.config = self.root / 'worker.json'
        self.config.write_text(json.dumps({'config_version': 2, 'worker_id': 'fixture',
                                          'remote_url': 'unused:remote',
                                          'state_dir': str(self.root / 'state')}))
        self.ledger = Ledger(self.spool / 'ledger.sqlite3')
        self.addCleanup(self.ledger.close)
        self.env = {**os.environ, 'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1',
                    'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                    'XDG_CONFIG_HOME': str(self.root / 'config'),
                    'GDI_TRANSPORT': 'invalid-but-unused', 'GDI_PROFILE_LOG': ''}
        self.argv = [sys.executable, '-m', 'gdi', 'worker', 'job', 'logs', '--config', str(self.config)]

    def job(self, number, *, content=b'', started=True, terminal=True):
        jid = f'{number:032x}'
        self.ledger.db.execute('INSERT INTO jobs(job_id,repository_id,request,run_id,state) VALUES(?,?,?,?,?)',
                               (jid, 'a' * 32, b'{}', 'b' * 32, 'PUBLISHED' if terminal else 'RUNNING'))
        self.ledger.db.commit()
        path = self.spool / 'jobs' / jid
        path.mkdir(parents=True)
        (path / 'build.log').write_bytes(content)
        if started:
            events = path / 'events'
            events.mkdir()
            (events / '00000001.json').write_text(json.dumps({'state': 'RUNNING'}))
        if terminal:
            self.finish(path, started=started)
        return path

    def finish(self, path, *, started=True):
        (path / 'result.json').write_text(json.dumps({'state': 'FAIL' if started else 'REJECTED',
                                                    'stages': [{'name': 'build'}] if started else []}))

    def command(self, *args):
        return subprocess.run([*self.argv, *args], cwd=self.root, env=self.env,
                              capture_output=True, timeout=5)

    def follow(self, *args):
        process = subprocess.Popen([*self.argv, '--follow', *args], cwd=self.root, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(cleanup)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), 'local follower did not start')
        self.assertIn(b'gdi: local job', process.stderr.readline())
        return process

    def test_latest_started_job_skips_newer_unstarted_and_defaults_to_ten_lines(self):
        old = self.job(1, content=b'old job\n')
        os.utime(old / 'build.log', (2000000000, 2000000000))
        content = b''.join(f'line {n}\n'.encode() for n in range(15)) + b'partial\xff'
        self.job(2, content=content)
        self.job(3, content=b'rejected\n', started=False)
        for args in [(), ('--follow',)]:
            result = self.command(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b''.join(content.splitlines(keepends=True)[-10:]))
            self.assertIn(b'00000000000000000000000000000002', result.stderr)
        self.assertEqual(self.command('-n', '1').stdout, b'partial\xff')
        self.assertEqual(self.command('-n', '0').stdout, b'')

    def test_follow_streams_history_and_appends_without_switching_jobs(self):
        content = b''.join(f'line {n}\n'.encode() for n in range(15))
        path = self.job(1, content=content, terminal=False)
        process = self.follow()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), 'history was buffered')
        self.assertIsNone(process.poll())
        self.job(2, content=b'new job must not appear\n')
        with (path / 'build.log').open('ab') as log:
            log.write(b'new output\x00\xff\n')
        self.finish(path)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, b''.join(content.splitlines(keepends=True)[-10:]) + b'new output\x00\xff\n')

    def test_follow_zero_lines_and_interrupt_only_stop_the_reader(self):
        path = self.job(1, content=b'history\n', terminal=False)
        process = self.follow('-n', '0')
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, stderr)
        self.assertEqual(stdout, b'')
        self.assertIn(b'log following stopped', stderr)
        self.assertEqual((path / 'build.log').read_bytes(), b'history\n')
        self.assertFalse((path / 'result.json').exists())

    def test_missing_jobs_and_invalid_count_do_not_create_or_change_worker_state(self):
        result = self.command()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'no locally started jobs', result.stderr)
        for lines in ['-1', 'invalid']:
            self.assertEqual(self.command('-n', lines).returncode, 2)
        self.config.write_text(json.dumps({'config_version': 2, 'worker_id': 'missing',
                                          'remote_url': 'unused:remote',
                                          'state_dir': str(self.root / 'state')}))
        self.assertEqual(self.command().returncode, 1)
        self.assertFalse((self.root / 'state/missing').exists())
