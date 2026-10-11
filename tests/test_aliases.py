"""User aliases work across repositories without executing shell text."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gdi.aliases import config_path, expand
from gdi.cli import main
from gdi.git import GdiError


class AliasTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='gdi-aliases-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                              'GDI_PROFILE_LOG': '', 'GDI_TRANSPORT': 'cli'})
        environment.start()
        self.addCleanup(environment.stop)

    def cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(argv))
        return code, output.getvalue(), errors.getvalue()

    def write_config(self, value):
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')

    def test_management_outside_git_without_transport_and_preserves_other_aliases(self):
        with patch('gdi.cli.Git.discover', side_effect=AssertionError('must not access git')), \
                patch('gdi.rc_transport.TransportSession', side_effect=AssertionError('must not access rclone')):
            self.assertEqual(self.cli('alias', 'list'), (0, 's = status\n', ''))
            self.assertTrue(config_path().exists())
            self.assertEqual(self.cli('alias', 'set', 's', 'status'), (0, 's = status\n', ''))
            self.assertEqual(self.cli('alias', 'set', 'pc', 'push --ci')[0], 0)
            self.assertEqual(self.cli('alias', 'set', 's', 'status --json')[0], 0)
            self.assertEqual(self.cli('alias', 'list'), (0, 'pc = push --ci\ns = status --json\n', ''))
            self.assertEqual(self.cli('alias', 'remove', 'pc')[0], 0)
            self.assertEqual(self.cli('alias', 'list'), (0, 's = status --json\n', ''))
            before = config_path().read_bytes()
            self.assertEqual(self.cli('alias', 'remove', 'missing')[0], 1)
            self.assertEqual(config_path().read_bytes(), before)

    def test_first_launch_creates_default_alias_for_help_version_and_alias(self):
        for number, argv in enumerate((['--help'], ['--version'], ['-v'],
                                       ['status', '--help'], ['s', '--help'])):
            with self.subTest(argv=argv), \
                    patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / str(number) / 'config')}):
                self.assertFalse(config_path().parent.exists())
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    main(argv)
                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(config_path().read_text(encoding='utf-8'), '[alias]\ns = status\n')
                self.assertEqual(expand(['s', '--json']), ['status', '--json'])

    def test_first_command_creates_config_before_execution(self):
        self.assertFalse(config_path().parent.exists())

        def logs(**kwargs):
            self.assertEqual(config_path().read_text(encoding='utf-8'), '[alias]\ns = status\n')
            return 0

        with patch('gdi.service.logs', side_effect=logs) as command, \
                patch('gdi.rc_transport.TransportSession', side_effect=AssertionError('unused')):
            self.assertEqual(self.cli('--no-progress', 'worker', 'logs'), (0, '', ''))
        command.assert_called_once_with(lines=100, follow=False, boot=False)

    def test_first_launch_creates_config_in_home_when_xdg_is_unset_or_empty(self):
        for number, base in enumerate((None, '')):
            home = self.root / str(number) / 'home'
            with self.subTest(base=base), patch.dict(os.environ), \
                    patch('pathlib.Path.home', return_value=home):
                if base is None:
                    os.environ.pop('XDG_CONFIG_HOME', None)
                else:
                    os.environ['XDG_CONFIG_HOME'] = base
                self.assertFalse(home.exists())
                self.assertEqual(self.cli('alias', 'list'), (0, 's = status\n', ''))
                self.assertEqual((home / '.config/gdi/aliases.ini').read_text(encoding='utf-8'),
                                 '[alias]\ns = status\n')

    def test_startup_does_not_restore_removed_default_alias(self):
        self.assertEqual(self.cli('alias', 'remove', 's'), (0, 'Removed alias s\n', ''))
        before = config_path().read_bytes()
        self.assertEqual(self.cli('alias', 'list'), (0, '', ''))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(config_path().read_bytes(), before)
        self.assertEqual(expand(['s']), ['s'])

    def test_startup_preserves_existing_config_bytes_and_timestamp(self):
        for raw in ('# custom aliases\n[alias]\nwl = worker logs --follow\n', '', 'broken'):
            with self.subTest(raw=raw):
                self.write_config(raw)
                path = config_path()
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))
                before = path.stat().st_mtime_ns
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    main(['--help'])
                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(path.read_bytes(), raw.encode('utf-8'))
                self.assertEqual(path.stat().st_mtime_ns, before)

    def test_direct_agent_help_does_not_access_alias_config(self):
        for prefix in ([], ['--no-progress'], ['--profile-log', 'agent'],
                       ['--prof=agent', '--progress']):
            with self.subTest(prefix=prefix), \
                    patch('gdi.aliases.config_path', side_effect=AssertionError('must not access alias config')), \
                    patch('gdi.aliases.expand', side_effect=AssertionError('must not expand aliases')), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                main([*prefix, 'agent', '--help'])
            self.assertEqual(raised.exception.code, 0)
        self.assertFalse((self.root / 'config').exists())

    def test_direct_agent_command_works_with_missing_broken_or_unavailable_alias_config(self):
        argv = ['--no-progress', 'agent', 'clone', '--snapshot', 'snapshot.json',
                '--repository-id', 'repo-id', '--destination', 'checkout']
        for state in ('missing', 'broken', 'unavailable'):
            base = self.root / state
            path = base / 'gdi/aliases.ini'
            if state == 'broken':
                path.parent.mkdir(parents=True)
                path.write_bytes(b'broken alias config\n')
            elif state == 'unavailable':
                base.write_bytes(b'file blocking the config directory\n')
            for fails in (False, True):
                with self.subTest(state=state, fails=fails), \
                        patch.dict(os.environ, {'XDG_CONFIG_HOME': str(base)}), \
                        patch('gdi.agent.clone', return_value={'destination': 'checkout'},
                              side_effect=GdiError('clone failed') if fails else None) as clone, \
                        patch('gdi.rc_transport.TransportSession', side_effect=AssertionError('unused')):
                    code, output, error = self.cli(*argv)
                    clone.assert_called_once_with('snapshot.json', 'repo-id', 'checkout')
                    self.assertEqual(code, 1 if fails else 0)
                    if fails:
                        self.assertEqual(json.loads(output), {'error': 'clone failed', 'kind': 'GDI_ERROR'})
                        self.assertIn('clone failed', error)
                    else:
                        self.assertEqual(json.loads(output), {'destination': 'checkout'})
                        self.assertEqual(error, '')
                    if state == 'missing':
                        self.assertFalse(base.exists())
                    elif state == 'broken':
                        self.assertEqual(path.read_bytes(), b'broken alias config\n')
                    else:
                        self.assertEqual(base.read_bytes(), b'file blocking the config directory\n')

    def test_quotes_literal_shell_text_case_and_global_options(self):
        target = self.root / 'must-not-be-created'
        value = f'push --ci --input "message=hello world" --input "literal=$(touch {target})" --input "pattern=%HOME%"'
        self.assertEqual(self.cli('alias', 'set', 'PC', value)[0], 0)
        prefix = ['--progress', '--profile-log', str(self.root / 'profile log')]
        args = expand([*prefix, 'PC', '--worker', 'host'])
        self.assertEqual(args, [*prefix, 'push', '--ci', '--input', 'message=hello world',
                                '--input', f'literal=$(touch {target})', '--input', 'pattern=%HOME%',
                                '--worker', 'host'])
        self.assertEqual(expand(['--prof=trace', 'PC'])[:2], ['--prof=trace', 'push'])
        self.assertEqual(expand(['pc']), ['pc'])
        self.assertFalse(target.exists())

    def test_invalid_definitions_do_not_change_config(self):
        self.assertEqual(self.cli('alias', 'set', 's', 'status')[0], 0)
        before = config_path().read_bytes()
        for name, value in (('push', 'status'), ('-s', 'status'), ('s name', 'status'),
                            ('s', ''), ('s', 'status "'), ('s', 'unknown'),
                            ('s', 's'), ('s', '!echo hello'), ('s', 'gdi status')):
            with self.subTest(name=name, value=value):
                code, _, error = self.cli('alias', 'set', '--', name, value)
                self.assertEqual(code, 1)
                self.assertIn('gdi:', error)
                self.assertEqual(config_path().read_bytes(), before)

    def test_manual_config_help_and_builtin_commands_do_not_read_broken_config(self):
        self.write_config('# comment\n[alias]\ns = status\nwl = worker logs --follow\n')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['s', '--help'])
        self.assertEqual(raised.exception.code, 0)
        with patch('gdi.service.logs', return_value=0) as logs, \
                patch('gdi.rc_transport.TransportSession', side_effect=AssertionError('unused')):
            self.assertEqual(self.cli('wl', '-n', '7')[0], 0)
        logs.assert_called_once_with(lines=7, follow=True, boot=False)
        for raw in ('broken', '[alias]\ns = status\ns = push\n', '[other]\ns = status\n',
                    '[DEFAULT]\ns = status\n[alias]\n'):
            with self.subTest(raw=raw):
                self.write_config(raw)
                self.assertEqual(self.cli('s')[0], 1)
                self.assertEqual(self.cli('alias', 'list')[0], 1)
                for argv in (['--help'], ['-v'], ['status', '--help']):
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                        main(argv)
                    self.assertEqual(raised.exception.code, 0)
                with patch('gdi.cli.execute', return_value=0):
                    self.assertEqual(self.cli('status')[0], 0)
        self.write_config('[alias]\ns = status "\n')
        code, _, error = self.cli('s')
        self.assertEqual(code, 1)
        self.assertIn('invalid alias s', error)

    def test_home_fallback_and_unknown_command_remains_an_argument_error(self):
        for base in (None, ''):
            with self.subTest(base=base), patch.dict(os.environ), patch('pathlib.Path.home', return_value=self.root):
                if base is None:
                    os.environ.pop('XDG_CONFIG_HOME', None)
                else:
                    os.environ['XDG_CONFIG_HOME'] = base
                self.assertEqual(config_path(), self.root / '.config/gdi/aliases.ini')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['unknown-command'])
        self.assertEqual(raised.exception.code, 2)

    def test_real_status_alias_has_identical_json_in_another_repository(self):
        self.assertEqual(self.cli('alias', 'set', 's', 'status')[0], 0)
        repo = self.root / 'repository'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True,
                       capture_output=True, timeout=10)
        environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        outputs = []
        for command in ('status', 's'):
            result = subprocess.run([sys.executable, '-m', 'gdi', command, '--json'], cwd=repo,
                                    env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout))
        self.assertEqual(outputs[0], outputs[1])
