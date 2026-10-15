import contextlib
import io
import json
import os
from unittest.mock import patch

from gdi.branches import branch_directory, directory_ref
from gdi.cli import main
from gdi.exchange import Exchange, encode
from gdi.git import GdiError
from gdi.local_config import load, save, select
from gdi.status import inspect
from tests.test_exchange import ExchangeTestCase


class ShortCommandTests(ExchangeTestCase):
    def cli(self, args, exchange=None):
        exchange = exchange or self.ea
        output = io.StringIO()
        errors = io.StringIO()
        with patch('gdi.cli.Git.discover', return_value=exchange.git), patch('gdi.cli.Exchange', return_value=exchange), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            options = [] if '--progress' in args or '--no-progress' in args else ['--no-progress']
            code = main(args + options)
        return code, output.getvalue(), errors.getvalue()

    def test_short_push_fetch_pull_select_current_branch_and_default(self):
        code, output, _ = self.cli(['push'])
        self.assertEqual(code, 0)
        self.assertIn('Connection: drive', output)
        self.assertIn('Branch: main', output)
        self.assertIn('Published', output)
        code, output, _ = self.cli(['push', '--json'])
        value = json.loads(output)
        self.assertEqual((code, value['remote'], value['branch'], value['created']), (0, 'drive', 'main', False))
        for command in ('fetch', 'pull'):
            code, output, error = self.cli([command], self.eb)
            self.assertEqual(code, 0, error)
            self.assertIn('Connection: drive', output)
            self.assertIn('Branch: main', output)

    def test_pull_prints_file_statuses_for_fast_forward(self):
        self.ea.push('drive')
        (self.a.path / 'to-delete.txt').write_text('temporary\n')
        self.a.call('add', 'to-delete.txt')
        self.a.call('-c', 'user.name=Gdi Test', '-c', 'user.email=gdi@example.invalid',
                    'commit', '-qm', 'add file later deleted')
        self.ea.push('drive')
        self.eb.pull('drive')
        (self.a.path / 'file.txt').write_text('updated\n')
        (self.a.path / 'to-delete.txt').unlink()
        (self.a.path / 'new.txt').write_text('new\n')
        self.a.call('add', '-A', '--', 'file.txt', 'to-delete.txt', 'new.txt')
        self.a.call('-c', 'user.name=Gdi Test', '-c', 'user.email=gdi@example.invalid',
                    'commit', '-qm', 'change files')
        self.ea.push('drive')

        code, output, error = self.cli(['pull'], self.eb)

        self.assertEqual(code, 0, error)
        self.assertIn('Changed files:', output)
        self.assertIn('создан   new.txt', output)
        self.assertIn('изменен  file.txt', output)
        self.assertIn('удален   to-delete.txt', output)

    def test_pull_reports_yellow_auto_merge_and_conflict_command(self):
        self.ea.push('drive')
        (self.b.path / 'local.txt').write_text('local\n')
        self.b.call('add', 'local.txt')
        self.b.call('-c', 'user.name=Gdi Test', '-c', 'user.email=gdi@example.invalid',
                    'commit', '-qm', 'local change')
        self.commit(self.a, 'remote change')
        self.ea.push('drive')
        with patch.dict(os.environ, {'FORCE_COLOR': '1'}):
            os.environ.pop('NO_COLOR', None)
            code, output, error = self.cli(['pull'], self.eb)
        self.assertEqual(code, 0, error)
        self.assertIn('\x1b[33mAuto merge started.\x1b[0m', output)
        self.assertIn('\x1b[33mAuto merge completed without conflicts.\x1b[0m', output)
        self.assertIn('Local merge:', output)
        self.assertIn('изменен  file.txt', output)

        # A later conflicting pull must leave the branch clean and end with a usable command.
        self.commit(self.a, 'second remote change')
        self.ea.push('drive')
        self.commit(self.b, 'local conflicting change')
        before = self.b.snapshot()
        with patch.dict(os.environ, {'FORCE_COLOR': '1'}):
            os.environ.pop('NO_COLOR', None)
            code, _output, error = self.cli(['pull'], self.eb)
        self.assertEqual(code, 1)
        self.assertEqual(self.b.snapshot(), before)
        self.assertIn('\x1b[33mAuto merge attempted; conflicts found, rolling back.\x1b[0m', error)
        self.assertTrue(error.rstrip().endswith(
            '\x1b[33mResolve manually: git merge --no-edit --no-ff refs/remotes/drive/main || git mergetool\x1b[0m'))

    def test_auto_merge_start_precedes_git_even_on_failure_and_respects_no_color(self):
        self.prepare_divergent_pull()
        before = self.b.snapshot()
        original = self.b.call
        for no_color in (False, True):
            with self.subTest(no_color=no_color), patch.dict(os.environ, {'FORCE_COLOR': '1'}):
                if no_color:
                    os.environ['NO_COLOR'] = '1'
                else:
                    os.environ.pop('NO_COLOR', None)
                expected = ('Auto merge started.' if no_color else
                            '\x1b[33mAuto merge started.\x1b[0m')
                observed = []
                def fail_merge(*args, **kwargs):
                    if '--no-commit' in args:
                        import sys
                        observed.append(sys.stdout.getvalue())
                        raise GdiError('injected merge failure')
                    return original(*args, **kwargs)
                with patch.object(self.b, 'call', side_effect=fail_merge):
                    code, output, error = self.cli(['pull'], self.eb)
                self.assertEqual(code, 1)
                self.assertEqual(len(observed), 1)
                self.assertIn(expected, observed[0])
                self.assertIn('injected merge failure', error)
                self.assertEqual(self.b.snapshot(), before)
                if no_color:
                    self.assertNotIn('\x1b[', output + error)

    def test_conflict_hint_is_last_after_profile_and_progress_teardown(self):
        self.ea.push('drive')
        self.commit(self.a, 'remote conflict')
        self.commit(self.b, 'local conflict')
        self.ea.push('drive')
        before = self.b.snapshot()
        cases = [('plain', []), ('flag', ['--profile-log']),
                 ('environment', []), ('progress', ['--progress', '--profile-log'])]
        for kind, arguments in cases:
            with self.subTest(kind=kind), patch.dict(os.environ, {'NO_COLOR': '1'}):
                path = self.root / (kind + '.jsonl')
                os.environ.pop('GDI_PROFILE_LOG', None)
                if kind == 'environment':
                    os.environ['GDI_PROFILE_LOG'] = str(path)
                args = ['pull', *arguments]
                if '--profile-log' in args:
                    args.append(str(path))
                code, _, error = self.cli(args, self.eb)
                self.assertEqual(code, 1)
                self.assertEqual(self.b.snapshot(), before)
                self.assertEqual(error.rstrip().splitlines()[-1],
                                 'Resolve manually: git merge --no-edit --no-ff '
                                 'refs/remotes/drive/main || git mergetool')
                if kind != 'plain':
                    self.assertIn('gdi: total', error)
                    records = [json.loads(line) for line in path.read_text().splitlines()]
                    self.assertEqual(records[-1]['exit_code'], 1)

    def test_changed_file_names_keep_unicode_and_escape_controls(self):
        self.ea.push('drive')
        names = ['данные.txt', 'tab\tfile.txt', 'line\nfile.txt', '\x1b[31mred.txt',
                 'c1\x9b31m.txt', 'bidi\u202efile.txt', 'literal\\n.txt']
        for name in names:
            (self.a.path / name).write_text('content\n')
        self.a.call('add', '--', *names)
        self.a.call('commit', '-qm', 'Unicode and control characters in paths')
        self.ea.push('drive')
        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            code, output, error = self.cli(['pull'], self.eb)
        self.assertEqual(code, 0, error)
        self.assertEqual(set(self.eb.last_pull_info['files']), {('создан', name) for name in names})
        self.assertIn('создан   данные.txt', output)
        self.assertIn('"tab\\tfile.txt"', output)
        self.assertIn('"line\\nfile.txt"', output)
        self.assertIn('"\\u001b[31mred.txt"', output)
        self.assertIn('"c1\\x9b31m.txt"', output)
        self.assertIn('"bidi\\u202efile.txt"', output)
        self.assertNotIn('\x1b', output)
        self.assertNotIn('\x9b', output)
        self.assertNotIn('\u202e', output)

    def test_multiple_connections_require_default_or_explicit_selection(self):
        self.ea.add('backup', 'memory:project')
        self.assertEqual(select(self.a), 'drive')
        code, _, _ = self.cli(['remote', 'default', 'backup'])
        self.assertEqual(code, 0)
        self.assertEqual(select(self.a), 'backup')
        self.assertEqual(select(self.a, 'drive'), 'drive')
        data = load(self.a)
        data['default_remote'] = None
        save(self.a, data)
        code, _, error = self.cli(['push'])
        self.assertEqual(code, 1)
        self.assertIn('multiple connections', error)
        self.ea.remove('backup')
        self.assertEqual(select(self.a), 'drive')

    def test_removing_default_and_empty_status(self):
        self.ea.add('second', 'memory:project')
        self.ea.remove('drive')
        self.assertEqual(load(self.a)['default_remote'], 'second')
        self.ea.remove('second')
        code, output, _ = self.cli(['status', '--json'])
        value = json.loads(output)
        self.assertEqual((code, value['connections']), (0, []))
        self.assertEqual(value['head'], self.first)
        with self.assertRaisesRegex(GdiError, 'no gdi connections'):
            select(self.a)

    def test_status_reports_publication_and_ahead_without_fetching(self):
        self.assertEqual(inspect(self.ea)['connections'][0]['state'], 'unpublished')
        self.ea.push('drive')
        downloads = list(self.store.downloads)
        refs = self.a.text('show-ref')
        code, output, _ = self.cli(['status', '--json'])
        value = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(value['connections'][0]['state'], 'published')
        self.assertTrue(value['connections'][0]['default'])
        self.assertEqual(self.a.text('show-ref'), refs)
        self.assertEqual(self.store.downloads, downloads)
        self.commit(self.a, 'second')
        self.assertEqual(inspect(self.ea)['connections'][0]['state'], 'ahead')
        self.ea.push('drive')
        self.assertEqual(inspect(self.eb)['connections'][0]['state'], 'unknown')
        self.eb.fetch('drive')
        self.assertEqual(inspect(self.eb)['connections'][0]['state'], 'behind')
        self.commit(self.b, 'different')
        self.assertEqual(inspect(self.eb)['connections'][0]['state'], 'diverged')

    def test_status_dirty_detached_unborn_and_remote_errors(self):
        self.ea.push('drive')
        (self.a.path / 'file.txt').write_text('uncommitted\n')
        self.assertTrue(inspect(self.ea)['dirty'])
        self.assertEqual(inspect(self.ea)['connections'][0]['state'], 'published')
        self.a.call('checkout', '--detach', 'HEAD')
        self.assertIsNone(inspect(self.ea)['branch'])
        self.assertEqual(inspect(self.ea)['connections'][0]['state'], 'detached')
        empty = self.repo('empty')
        receiver = Exchange(empty, self.ea.transport_factory)
        receiver.add('drive', 'memory:project')
        value = inspect(receiver)
        self.assertIsNone(value['head'])
        self.assertEqual(value['connections'][0]['state'], 'behind')
        self.store.fail = 'read'
        code, output, _ = self.cli(['status', '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)['connections'][0]['state'], 'error')

    def test_old_local_and_remote_formats_are_rejected(self):
        old = encode({'config_version': 1, 'remotes': {}})
        config = self.a.gdi_dir() / 'config.json'
        config.write_bytes(old)
        with self.assertRaisesRegex(GdiError, 'config version 2'):
            load(self.a)
        self.assertEqual(config.read_bytes(), old)
        for version in (1, 2):
            self.store.data['repository.json'] = encode({'version': version, 'repository_id': self.identity, 'object_format': 'sha1'})
            with self.assertRaisesRegex(GdiError, 'protocol v3'):
                self.eb.connect('drive')

    def test_readable_branch_names_are_reversible_and_distinct(self):
        names = ['main', 'feature/login', 'feature_login', 'feature%2Flogin', 'ветка/исправление']
        directories = []
        for name in names:
            ref = self.a.ref(name)
            directory = branch_directory(ref)
            self.assertEqual(directory_ref(directory), ref)
            directories.append(directory)
            if name != 'main':
                self.a.call('branch', name, 'HEAD')
            self.ea.push('drive', name)
            self.assertTrue(any(path.startswith('branches/' + directory + '/') for path in self.store.data))
        self.assertEqual(directories, ['main', 'feature%2Flogin', 'feature_login', 'feature%252Flogin', 'ветка%2Fисправление'])
        self.assertEqual(len(set(directories)), len(names))
        self.ea.gc('drive', report=lambda _: None)
        for directory in ('..', '', 'a/b', 'a%2fb', 'a%00b', '%2Ftmp'):
            with self.assertRaises(GdiError):
                directory_ref(directory)
