import contextlib
import io
import json
from unittest.mock import patch

from gdi.cli import main
from gdi.exchange import decode, digest, encode
from gdi.local_config import load, save
from tests.test_exchange import ExchangeTestCase


class LogTests(ExchangeTestCase):
    def cli(self, arguments, exchange=None):
        exchange = exchange or self.ea
        output = io.StringIO()
        errors = io.StringIO()
        with patch('gdi.cli.Git.discover', return_value=exchange.git), patch('gdi.cli.Exchange', return_value=exchange), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(['log', *arguments, '--no-progress'])
        return code, output.getvalue(), errors.getvalue()

    def test_default_history_newest_first_limit_and_no_git_mutation(self):
        _, first_id, _ = self.ea.push('drive')
        head = self.commit(self.a, 'second commit')
        _, second_id, _ = self.ea.push('drive')
        before = self.a.snapshot(), self.a.text('show-ref'), list(self.store.uploads), list(self.store.downloads)
        code, output, error = self.cli([])
        self.assertEqual(code, 0, error)
        lines = output.splitlines()
        self.assertIn('Connection: drive', lines[0])
        self.assertEqual(lines[1], 'Branch: main')
        self.assertTrue(lines[2].startswith(head[:12]))
        self.assertIn('second commit', lines[2])
        self.assertIn(second_id[:12], lines[2])
        self.assertIn(first_id[:12], lines[3])
        code, output, error = self.cli(['-n', '1', '--json'])
        value = json.loads(output)
        self.assertEqual(code, 0, error)
        self.assertEqual(value['total'], 2)
        self.assertEqual(value['branch'], 'main')
        self.assertEqual(len(value['publications']), 1)
        self.assertEqual(value['publications'][0]['publication_id'], second_id)
        self.assertEqual(before, (self.a.snapshot(), self.a.text('show-ref'), list(self.store.uploads), list(self.store.downloads)))

    def test_empty_branch_and_remote_only_commit(self):
        code, output, _ = self.cli([])
        self.assertEqual(code, 0)
        self.assertIn('No publications', output)
        code, output, _ = self.cli(['--json'])
        self.assertEqual(json.loads(output)['publications'], [])
        self.commit(self.a, 'remote commit absent from receiver')
        self.ea.push('drive')
        before = self.b.snapshot(), self.b.text('show-ref')
        code, output, error = self.cli(['--json'], self.eb)
        self.assertEqual(code, 0, error)
        self.assertIsNone(json.loads(output)['publications'][0]['subject'])
        self.assertEqual(before, (self.b.snapshot(), self.b.text('show-ref')))
        self.assertEqual(self.store.downloads, [])

    def test_explicit_remote_branch_and_detached_head(self):
        self.a.call('branch', 'feature/login', 'HEAD')
        self.ea.push('drive', 'feature/login')
        self.ea.add('backup', 'memory:project')
        self.a.call('checkout', '--detach', 'HEAD')
        code, output, error = self.cli(['backup', 'feature/login', '--json'])
        self.assertEqual(code, 0, error)
        value = json.loads(output)
        self.assertEqual((value['remote'], value['branch'], value['total']), ('backup', 'feature/login', 1))
        code, _, error = self.cli([])
        self.assertEqual(code, 1)
        self.assertIn('detached HEAD', error)
        self.a.call('checkout', 'main')
        config = load(self.a)
        config['default_remote'] = None
        save(self.a, config)
        code, _, error = self.cli([])
        self.assertEqual(code, 1)
        self.assertIn('multiple connections', error)

    def test_limit_does_not_hide_invalid_older_metadata_or_conflicts(self):
        self.ea.push('drive')
        self.commit(self.a, 'second')
        self.ea.push('drive')
        path = next(path for path in self.store.data if path.startswith('branches/'))
        original = self.store.data[path]
        self.store.data[path] += b'corrupt'
        code, output, error = self.cli(['--max-count', '1', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('checksum', error)
        self.assertNotIn('publications', json.loads(output))
        self.store.data[path] = original
        data = decode(original)
        data['nonce'] = '0' * 32
        raw = encode(data)
        self.store.data['branches/main/' + digest(raw) + '.json'] = raw
        code, _, error = self.cli(['-n', '1'])
        self.assertEqual(code, 1)
        self.assertIn('conflicting publications', error)

    def test_invalid_limits_are_argument_errors(self):
        for value in ('0', '-1', 'invalid'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(['log', '-n', value])
                self.assertEqual(raised.exception.code, 2)

    def test_terminal_control_in_commit_subject_is_escaped(self):
        self.a.call('-c', 'user.name=Gdi Test', '-c', 'user.email=gdi@example.invalid',
                    'commit', '--allow-empty', '-qm', 'message \033[31mred')
        self.ea.push('drive')
        code, output, _ = self.cli([])
        self.assertEqual(code, 0)
        self.assertNotIn('\033', output)
        self.assertIn('message \\x1b[31mred', output)
