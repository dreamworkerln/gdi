"""Journal streaming works outside Git, without a user service or rclone."""

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest


class WorkerLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='gdi-worker-logs-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        binaries = self.root / 'bin'
        binaries.mkdir()
        executable = binaries / 'journalctl'
        executable.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys, time
pathlib.Path(os.environ['ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))
pathlib.Path(os.environ['CHILD_PID']).write_text(str(os.getpid()))
print('first journal entry', flush=True)
if '--follow' in sys.argv:
    while True:
        time.sleep(1)
print('journal detail', file=sys.stderr, flush=True)
sys.exit(int(os.environ.get('JOURNAL_EXIT', '0')))
''')
        executable.chmod(0o755)
        self.arguments = self.root / 'arguments.json'
        self.child_pid = self.root / 'child.pid'
        self.env = {**os.environ, 'PATH': str(binaries),
                    'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                    'PYTHONDONTWRITEBYTECODE': '1', 'GDI_TRANSPORT': 'invalid-but-unused',
                    'GDI_PROFILE_LOG': '', 'ARGUMENTS': str(self.arguments),
                    'CHILD_PID': str(self.child_pid)}

    def command(self, *args, **env):
        return subprocess.run([sys.executable, '-m', 'gdi', 'worker', 'logs', *args],
                              cwd=self.root, env={**self.env, **env},
                              capture_output=True, text=True, timeout=10)

    def test_default_and_boot_line_selection_with_optional_profile(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'first journal entry\n')
        self.assertEqual(result.stderr, 'journal detail\n')
        self.assertEqual(json.loads(self.arguments.read_text()),
                         ['--user', '--unit=gdi-worker.service', '--no-pager', '--lines=100'])
        profile = self.root / 'profile.jsonl'
        result = self.command('-b', '-n', '7', '--profile-log', str(profile))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'first journal entry\n')
        self.assertIn('--boot', json.loads(self.arguments.read_text()))
        self.assertIn('--lines=7', json.loads(self.arguments.read_text()))
        records = [json.loads(line) for line in profile.read_text().splitlines()]
        self.assertTrue(any(row.get('event') == 'subprocess_finish' and row.get('program') == 'journalctl'
                            and row.get('returncode') == 0 for row in records))

    def test_follow_streams_before_exit_and_parent_interrupt_reaps_journalctl(self):
        process = subprocess.Popen([sys.executable, '-m', 'gdi', 'worker', 'logs', '-f', '-n', '0'],
                                   cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), 'journal output was buffered')
                self.assertEqual(process.stdout.readline(), 'first journal entry\n')
            self.assertIsNone(process.poll())
            pid = int(self.child_pid.read_text())
            self.assertIn('--lines=0', json.loads(self.arguments.read_text()))
            process.send_signal(signal.SIGINT)  # Only the parent, not the child/process group.
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stderr)
            self.assertIn('log following stopped', stderr)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_journal_failure_and_missing_executable_are_reported(self):
        result = self.command(JOURNAL_EXIT='5')
        self.assertEqual(result.returncode, 5)
        self.assertIn('journal detail', result.stderr)
        result = self.command(PATH='')
        self.assertEqual(result.returncode, 1)
        self.assertIn('journalctl not found in PATH', result.stderr)

    def test_invalid_line_count_does_not_start_journalctl(self):
        for count in ('-1', 'invalid'):
            with self.subTest(count=count):
                result = self.command('--lines', count)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.arguments.exists())
