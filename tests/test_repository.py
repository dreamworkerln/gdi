"""Ordinary init/clone/create commands preserve local data and verify remote history."""

import contextlib
import copy
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gdi.aliases import expand, validate_name
from gdi.cli import main, parser
from gdi.exchange import Exchange, decode, encode
from gdi.git import GdiError, Git
from gdi.local_config import load
from gdi.repository import clone, initialize
from tests.test_exchange import ExchangeTestCase, MemoryTransport


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class InitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-init-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_init_without_git_or_transport_and_git_add_does_not_track_gdi(self):
        target = self.root / 'project'
        with patch('gdi.rc_transport.TransportSession', side_effect=AssertionError('local command')), \
                patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['init', str(target), '-b', 'main']), 0)
        self.assertIn(str(target), output.getvalue())
        git = Git.discover(target)
        self.assertEqual(git.branch(), 'main')
        self.assertIsNone(git.oid('HEAD'))
        self.assertEqual(load(git), {'config_version': 2, 'default_remote': None, 'remotes': {}})
        (target / 'hello.txt').write_text('hello\n')
        git.call('add', '.')
        self.assertEqual(git.text('ls-files'), 'hello.txt')

    def test_repeated_init_preserves_git_state_connections_and_exclude(self):
        git = initialize(self.root / 'project', branch='main')
        (git.path / 'hello.txt').write_text('first\n')
        git.call('add', 'hello.txt')
        git.call('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'first')
        Exchange(git, lambda url: MemoryTransport()).create('drive', 'memory:project')
        (git.path / 'hello.txt').write_text('staged\n')
        git.call('add', 'hello.txt')
        (git.path / 'hello.txt').write_text('unstaged\n')
        (git.path / 'untracked').write_text('keep\n')
        paths = [git.path / '.git/config', git.path / '.git/index', git.path / '.git/info/exclude',
                 git.path / '.gdi/config.json']
        before = git.snapshot(), {path: path.read_bytes() for path in paths}
        initialize(git.path, branch='other')
        self.assertEqual(before, (git.snapshot(), {path: path.read_bytes() for path in paths}))

    def test_init_preserves_existing_exclude_without_trailing_newline(self):
        target = self.root / 'project'
        target.mkdir()
        Git(target).call('init', '-q', '-b', 'main', '--template=')
        exclude = target / '.git/info/exclude'
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_bytes(b'local-only')
        initialize(target)
        self.assertEqual(exclude.read_bytes(), b'local-only\n/.gdi/\n')

    def test_init_handles_local_ignore_negation_and_reports_project_override(self):
        git = initialize(self.root / 'project', branch='main')
        exclude = git.path / '.git/info/exclude'
        exclude.write_bytes(b'/.gdi/\n!/.gdi/\n')
        initialize(git.path)
        self.assertEqual(exclude.read_bytes(), b'/.gdi/\n!/.gdi/\n/.gdi/\n')
        (git.path / '.gitignore').write_text('!/.gdi/\n')
        with self.assertRaisesRegex(GdiError, 'ignore rules reinclude'):
            initialize(git.path)
        self.assertEqual((git.path / '.gitignore').read_text(), '!/.gdi/\n')

    def test_init_does_not_attach_a_new_directory_to_its_parent_repository(self):
        parent = initialize(self.root / 'parent', branch='main')
        child = initialize(parent.path / 'child', branch='other')
        self.assertEqual(child.path, parent.path / 'child')
        self.assertEqual(child.branch(), 'other')
        self.assertEqual(parent.branch(), 'main')

    def test_init_refuses_bare_and_symlink_destinations(self):
        bare = self.root / 'bare'
        bare.mkdir()
        Git(bare).call('init', '--bare', '-q')
        with self.assertRaisesRegex(GdiError, 'bare'):
            initialize(bare)
        self.assertFalse((bare / '.gdi').exists())
        link = self.root / 'link'
        link.symlink_to(bare, target_is_directory=True)
        with self.assertRaisesRegex(GdiError, 'symlink'):
            initialize(link)

    def test_init_in_linked_worktree_uses_shared_gdi_without_changing_detached_head(self):
        git = initialize(self.root / 'project', branch='main')
        git.call('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-qm', 'first')
        Exchange(git, lambda url: MemoryTransport()).create('drive', 'memory:project')
        linked = self.root / 'linked'
        git.call('worktree', 'add', '--detach', str(linked), 'HEAD')
        config = (git.gdi_dir() / 'config.json').read_bytes()
        received = initialize(linked)
        self.assertEqual(received.gdi_dir(), git.gdi_dir())
        self.assertEqual((git.gdi_dir() / 'config.json').read_bytes(), config)
        self.assertEqual(received.oid('HEAD'), git.oid('HEAD'))
        self.assertEqual(received.call('symbolic-ref', '-q', 'HEAD', allowed=(0, 1)).returncode, 1)

    def test_init_refuses_tracked_gdi_and_preserves_invalid_local_config(self):
        git = initialize(self.root / 'project', branch='main')
        path = git.path / '.gdi/config.json'
        path.write_text('{"config_version":1}\n')
        before = path.read_bytes()
        with self.assertRaisesRegex(GdiError, 'config version 2'):
            initialize(git.path)
        self.assertEqual(path.read_bytes(), before)
        git.call('add', '-f', '.gdi/config.json')
        with self.assertRaisesRegex(GdiError, 'must not be tracked'):
            initialize(git.path)
        self.assertEqual(path.read_bytes(), before)

    def test_parser_and_aliases_expose_new_commands_and_reject_old_init_flag(self):
        for name in ('init', 'clone'):
            with self.subTest(name=name), self.assertRaisesRegex(GdiError, 'built-in'):
                validate_name(name)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser().parse_args(['remote', 'add', 'drive', 'memory:project', '--init'])
        self.assertEqual(raised.exception.code, 2)
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')}):
            config = self.root / 'config/gdi/aliases.ini'
            config.parent.mkdir(parents=True)
            config.write_text('[alias]\nget = clone\n')
            self.assertEqual(expand(['get', 'memory:project']), ['clone', 'memory:project'])


class CloneTests(ExchangeTestCase):
    def setUp(self):
        super().setUp()
        self.ea.push('drive')

    def receive(self, destination, **options):
        return clone('memory:project', destination, transport_factory=lambda url: self.store, **options)

    def assert_no_partial_clone(self):
        self.assertEqual(list(self.root.glob('.gdi-clone-*')), [])

    def test_clone_restores_incremental_history_and_all_remote_branches(self):
        second = self.commit(self.a, 'second')
        self.ea.push('drive')
        self.a.call('branch', 'feature/ветка%25', second)
        self.ea.push('drive', 'feature/ветка%25')
        before = copy.deepcopy(self.store.data), list(self.store.uploads)
        target = self.root / 'clone'
        result = self.receive(target, branch='main', expected_id=self.identity)
        git = Git.discover(target)
        self.assertEqual((git.branch(), git.oid('HEAD')), ('main', second))
        self.assertEqual(git.oid('refs/remotes/drive/main'), second)
        self.assertEqual(git.oid('refs/remotes/drive/feature/ветка%25'), second)
        self.assertTrue(git.ancestor(self.first, second))
        self.assertEqual((target / 'file.txt').read_text(), 'second\n')
        self.assertEqual(git.snapshot()[2], '')
        self.assertEqual(load(git)['remotes']['drive']['repository_id'], self.identity)
        self.assertEqual(result['branches'], ['feature/ветка%25', 'main'])
        self.assertEqual(before, (self.store.data, self.store.uploads))
        self.assert_no_partial_clone()

    def test_clone_single_branch_infers_destination_and_branch(self):
        with working_directory(self.root):
            result = clone('memory:project', transport_factory=lambda url: self.store)
        self.assertEqual(result['destination'], str(self.root / 'project'))
        self.assertEqual((result['branch'], result['head']), ('main', self.first))
        self.assertEqual(Git.discover(self.root / 'project').oid('HEAD'), self.first)

    def test_clone_accepts_an_existing_empty_directory(self):
        target = self.root / 'clone'
        target.mkdir()
        self.receive(target)
        self.assertEqual(Git.discover(target).oid('HEAD'), self.first)

    def test_clone_refuses_replacing_the_calling_shell_current_directory(self):
        target = self.root / 'clone'
        target.mkdir()
        inode = target.stat().st_ino
        with working_directory(target), self.assertRaisesRegex(GdiError, 'current directory'):
            self.receive('.')
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(list(target.iterdir()), [])

    def test_multiple_branches_require_explicit_selection_without_leaving_checkout(self):
        self.a.call('branch', 'other', self.first)
        self.ea.push('drive', 'other')
        for exists in (False, True):
            target = self.root / ('existing' if exists else 'absent')
            if exists:
                target.mkdir()
            with self.subTest(exists=exists), self.assertRaisesRegex(GdiError, 'choose --branch'):
                self.receive(target)
            self.assertEqual(target.exists(), exists)
            if exists:
                self.assertEqual(list(target.iterdir()), [])
            self.assert_no_partial_clone()

    def test_wrong_identity_unknown_branch_and_invalid_branch_leave_no_checkout(self):
        for options, message in (({'expected_id': '0' * 32}, 'repository ID mismatch'),
                                 ({'branch': 'absent'}, 'no completed publication'),
                                 ({'branch': '../bad'}, 'git failed')):
            target = self.root / 'clone'
            with self.subTest(options=options), self.assertRaisesRegex(GdiError, message):
                self.receive(target, **options)
            self.assertFalse(target.exists())
            self.assert_no_partial_clone()

    def test_occupied_or_symlink_destination_is_preserved_before_network_access(self):
        target = self.root / 'occupied'
        target.mkdir()
        sentinel = target / 'keep'
        sentinel.write_text('original')
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        for path in (target, sentinel, link):
            with self.subTest(path=path), patch.object(self.store, 'read', side_effect=AssertionError('no reads')), \
                    self.assertRaisesRegex(GdiError, 'destination'):
                self.receive(path)
        self.assertEqual(sentinel.read_text(), 'original')
        self.assertTrue(link.is_symlink())

    def test_corrupt_bundle_preserves_empty_target_and_remote_files(self):
        target = self.root / 'clone'
        target.mkdir()
        path = next(name for name in self.store.data if name.startswith('bundles/'))
        self.store.data[path] += b'corrupt'
        before = copy.deepcopy(self.store.data)
        with self.assertRaisesRegex(GdiError, 'SHA256'):
            self.receive(target)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.store.data, before)
        self.assert_no_partial_clone()

    def test_interrupted_download_leaves_destination_absent(self):
        def interrupt():
            raise KeyboardInterrupt()
        self.store.after_download = interrupt
        target = self.root / 'clone'
        with self.assertRaises(KeyboardInterrupt):
            self.receive(target)
        self.assertFalse(target.exists())
        self.assert_no_partial_clone()

    def test_identity_change_during_download_is_rejected_before_checkout_is_published(self):
        def replace_identity():
            value = decode(self.store.data['repository.json'])
            value['repository_id'] = '0' * 32
            self.store.data['repository.json'] = encode(value)
        self.store.after_download = replace_identity
        target = self.root / 'clone'
        with self.assertRaisesRegex(GdiError, 'repository ID mismatch'):
            self.receive(target)
        self.assertFalse(target.exists())
        self.assert_no_partial_clone()

    def test_concurrent_destination_file_is_not_overwritten(self):
        target = self.root / 'clone'
        target.mkdir()
        self.store.after_download = lambda: (target / 'keep').write_text('concurrent data')
        with self.assertRaisesRegex(GdiError, 'destination'):
            self.receive(target)
        self.assertEqual((target / 'keep').read_text(), 'concurrent data')
        self.assert_no_partial_clone()

    def test_empty_remote_can_be_cloned_without_creating_remote_files(self):
        self.store.data = {'repository.json': self.store.data['repository.json']}
        before = copy.deepcopy(self.store.data)
        target = self.root / 'clone'
        result = self.receive(target)
        self.assertIsNone(result['head'])
        self.assertEqual(result['branches'], [])
        self.assertEqual(Git.discover(target).snapshot()[2], '')
        self.assertEqual(self.store.data, before)

    def test_clone_rejects_a_project_ignore_override_exposing_local_gdi(self):
        (self.a.path / '.gitignore').write_text('!/.gdi/\n')
        self.a.call('add', '.gitignore')
        self.a.call('commit', '-qm', 'project ignore override')
        self.ea.push('drive')
        target = self.root / 'clone'
        with self.assertRaisesRegex(GdiError, 'ignore rules reinclude'):
            self.receive(target)
        self.assertFalse(target.exists())
        self.assert_no_partial_clone()

    def test_cli_clone_runs_outside_a_git_repository(self):
        target = self.root / 'clone'
        with working_directory(self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')}), \
                patch('gdi.rc_transport.TransportSession') as session, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            session.return_value.__enter__.return_value = lambda url: self.store
            self.assertEqual(main(['clone', 'memory:project', str(target), '--repository-id', self.identity]), 0)
        self.assertIn('Cloned memory:project', output.getvalue())
        self.assertEqual(Git.discover(target).oid('HEAD'), self.first)

    def test_remote_add_only_reads_and_create_refuses_existing_remote(self):
        before = copy.deepcopy(self.store.data), list(self.store.uploads)
        self.ea.add('backup', 'memory:project', expected_id=self.identity)
        self.assertEqual(before, (self.store.data, self.store.uploads))
        with self.assertRaisesRegex(GdiError, 'empty dedicated folder'):
            self.ea.create('new', 'memory:project')
        self.assertEqual(before, (self.store.data, self.store.uploads))
        self.assertNotIn('new', load(self.a)['remotes'])

    def test_push_missing_remote_does_not_create_files_and_suggests_remote_create(self):
        self.store.data.clear()
        self.store.directories = {''}
        with patch.object(self.store, 'read_optional', return_value=None, create=True), \
                self.assertRaisesRegex(GdiError, 'gdi remote create'):
            self.ea.push('drive')
        self.assertEqual(self.store.data, {})
        self.assertEqual(self.store.directories, {''})

    def test_create_does_not_mutate_remote_when_local_gdi_is_tracked(self):
        self.a.call('add', '-f', '.gdi/config.json')
        empty = MemoryTransport()
        exchange = Exchange(self.a, lambda url: empty)
        with self.assertRaisesRegex(GdiError, 'must not be tracked'):
            exchange.create('new', 'memory:new')
        self.assertEqual(empty.data, {})
        self.assertEqual(empty.directories, {''})


@unittest.skipUnless(shutil.which('rclone'), 'rclone required for local backend integration')
class RepositoryCliIntegrationTests(unittest.TestCase):
    def test_init_create_push_clone_roundtrip_with_real_cli_and_local_backend(self):
        self.roundtrip('cli')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'native RC requires Linux parent-death protection')
    def test_init_create_push_clone_roundtrip_with_native_rc(self):
        self.roundtrip('rc')

    def roundtrip(self, mode):
        with tempfile.TemporaryDirectory(prefix='gdi-repository-cli-') as temporary:
            root = Path(temporary)
            source, target, remote = root / 'source', root / 'target', root / 'remote'
            environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                           'PYTHONDONTWRITEBYTECODE': '1', 'XDG_CONFIG_HOME': str(root / 'config'),
                           'GDI_TRANSPORT': mode, 'RCLONE_CONFIG': '/dev/null',
                           'RCLONE_CONFIG_GDIREPOTEST_TYPE': 'local'}
            def cli(cwd, *arguments, expected=0):
                result = subprocess.run([sys.executable, '-m', 'gdi', '--no-progress', *arguments],
                                        cwd=cwd, env=environment, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result.stdout + result.stderr
            cli(root, 'init', str(source), '-b', 'main')
            git = Git.discover(source)
            (source / 'hello.txt').write_text('hello\n')
            git.call('add', '.')
            git.call('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'first')
            self.assertEqual(git.text('ls-files'), 'hello.txt')
            url = 'gdirepotest:' + str(remote)
            cli(source, 'remote', 'create', 'drive', url)
            cli(source, 'push')
            before = {path.relative_to(remote): path.read_bytes() for path in remote.rglob('*') if path.is_file()}
            cli(root, 'clone', url, str(target))
            received = Git.discover(target)
            self.assertEqual(received.oid('HEAD'), git.oid('HEAD'))
            self.assertEqual((target / 'hello.txt').read_text(), 'hello\n')
            self.assertEqual(before, {path.relative_to(remote): path.read_bytes()
                                      for path in remote.rglob('*') if path.is_file()})
            self.assertIn('Already published', cli(target, 'push'))
            self.assertIn('unrecognized arguments: --init',
                          cli(target, 'remote', 'add', 'other', url, '--init', expected=2))
