"""Service controls work outside Git and report systemd failures to the caller."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='gdi-worker-service-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        binaries = self.root / 'bin'
        binaries.mkdir()
        executable = binaries / 'systemctl'
        executable.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys
with pathlib.Path(os.environ['ARGUMENTS']).open('a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[2] == os.environ.get('FAIL_OPERATION'):
    print('fixture systemd failure', file=sys.stderr)
    sys.exit(5)
if sys.argv[2] == 'show':
    print('ActiveState=active\\nSubState=running\\nExecMainStatus=0\\nUnitFileState=enabled')
''')
        executable.chmod(0o755)
        self.arguments = self.root / 'arguments.jsonl'
        self.env = {**os.environ, 'PATH': str(binaries),
                    'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                    'PYTHONDONTWRITEBYTECODE': '1', 'GDI_TRANSPORT': 'invalid-but-unused',
                    'GDI_PROFILE_LOG': '', 'ARGUMENTS': str(self.arguments)}

    def command(self, *args, **env):
        return subprocess.run([sys.executable, '-m', 'gdi', 'worker', *args],
                              cwd=self.root, env={**self.env, **env},
                              capture_output=True, text=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in self.arguments.read_text().splitlines()]

    def test_restart_reloads_unit_before_restarting_without_git_or_rclone(self):
        result = self.command('restart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [['--user', 'daemon-reload'],
                                       ['--user', 'restart', 'gdi-worker.service']])

    def test_reload_failure_does_not_restart_and_restart_failure_is_reported(self):
        for operation in ('daemon-reload', 'restart'):
            with self.subTest(operation=operation):
                self.arguments.unlink(missing_ok=True)
                result = self.command('restart', FAIL_OPERATION=operation)
                self.assertEqual(result.returncode, 1)
                self.assertIn('fixture systemd failure', result.stderr)
                self.assertEqual(len(self.calls()), 1 if operation == 'daemon-reload' else 2)

    def test_existing_service_controls_still_work_without_transport(self):
        for operation, expected in (('start', ['--user', 'enable', '--now', 'gdi-worker.service']),
                                    ('stop', ['--user', 'stop', 'gdi-worker.service'])):
            with self.subTest(operation=operation):
                self.arguments.unlink(missing_ok=True)
                result = self.command(operation)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), [expected])
        result = self.command('status', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['ActiveState'], 'active')

    def test_missing_systemctl_is_reported(self):
        result = self.command('restart', PATH='')
        self.assertEqual(result.returncode, 1)
        self.assertIn('systemctl not found in PATH', result.stderr)
