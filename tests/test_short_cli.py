import contextlib
import io
import json
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
            code = main(args + ['--no-progress'])
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
