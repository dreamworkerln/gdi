"""Exercise the standalone runtime and publication failures without Google Drive."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tests.snapshots import export_snapshot
from tests.test_exchange import MemoryTransport


class DeployTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-deploy-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'source with spaces'
        self.repo.mkdir()
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(source / 'gdi', self.repo / 'gdi', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(source / 'scripts', self.repo / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('LICENSE', '.gitignore'):
            shutil.copyfile(source / name, self.repo / name)
        self.output = self.root / 'runtime with spaces'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GIT_', 'GDI_')) and key not in ('PYTHONPATH', 'PYTHONHOME')}
        self.env.update(XDG_CONFIG_HOME=str(self.root / 'config'), PYTHONDONTWRITEBYTECODE='1',
                        GDI_TRANSPORT='cli', RCLONE_CONFIG=str(self.root / 'rclone.conf'),
                        RCLONE_CONFIG_GDIDEPLOYTEST_TYPE='local')
        self.git('init', '-q', '-b', 'main', '--template=')
        self.git('config', 'user.name', 'GDI Runtime Test')
        self.git('config', 'user.email', 'gdi-runtime@example.invalid')
        self.commit()

    def run_command(self, args, *, cwd=None, expected=0):
        result = subprocess.run(args, cwd=cwd or self.repo, env=self.env, capture_output=True, text=True, timeout=90)
        if expected is not None:
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def git(self, *args):
        return self.run_command(['git', *args]).stdout.strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'runtime fixture')
        self.head = self.git('rev-parse', 'HEAD')

    def cli(self, *args):
        return self.run_command([sys.executable, '-m', 'gdi', '--no-progress', *args])

    def deploy(self, *args, expected=0):
        return self.run_command(['bash', str(self.repo / 'scripts/deploy.sh'), '--output', str(self.output), *args],
                                cwd=self.root, expected=expected)

    def initialize_remote(self):
        self.store = self.root / 'drive/project'
        self.url = 'gdideploytest:' + str(self.store)
        self.runtime_url = self.url + '/runtime'
        self.cli('remote', 'add', 'drive', self.url, '--init')
        self.identity = json.loads((self.store / 'repository.json').read_text())['repository_id']

    def snapshot(self):
        transport = MemoryTransport()
        for path in self.store.rglob('*'):
            relative = path.relative_to(self.store).as_posix()
            if path.is_dir():
                transport.mkdir(relative)
            else:
                transport.data[relative] = path.read_bytes()
        return export_snapshot(self.root / 'snapshot', transport,
                               include=['', 'branches', 'branches/main', 'bundles'])

    def test_reproducible_runtime_runs_after_sources_are_removed(self):
        self.deploy('--build-only')
        data = (self.output / 'gdi.pyz').read_bytes()
        metadata = json.loads((self.output / 'runtime.json').read_text())
        self.assertEqual(metadata['head'], self.head)
        self.assertEqual(metadata['ref'], 'refs/heads/main')
        self.assertEqual(metadata['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(metadata['bytes'], len(data))
        with zipfile.ZipFile(self.output / 'gdi.pyz') as archive:
            self.assertIn('LICENSE', archive.namelist())
            self.assertEqual(json.loads(archive.read('GDI_RUNTIME.json'))['head'], self.head)
            self.assertFalse(any('__pycache__' in name or name.startswith(('.git/', 'tests/', 'scripts/'))
                                 for name in archive.namelist()))
        self.deploy('--build-only')
        self.assertEqual((self.output / 'gdi.pyz').read_bytes(), data)
        shutil.rmtree(self.repo)
        self.env['PYTHONPATH'] = '/no/such/source'
        for args in (('--help',), ('--version',), ('agent', 'snapshot', '--help'), ('agent', 'clone', '--help')):
            self.run_command([sys.executable, '-I', str(self.output / 'gdi.pyz'), *args], cwd=self.root)

    @unittest.skipUnless(shutil.which('rclone'), 'requires rclone with local backend')
    def test_dirty_sources_and_untracked_files_are_not_deployed(self):
        self.initialize_remote()
        for name in ('gdi/cli.py', 'untracked.txt'):
            with self.subTest(name=name):
                path = self.repo / name
                original = path.read_bytes() if path.exists() else None
                path.write_text('dirty\n')
                result = self.deploy('--runtime-dir', self.runtime_url, expected=1)
                self.assertIn('commit all changes', result.stderr)
                self.assertFalse((self.store / 'runtime').exists())
                self.assertFalse((self.output / 'gdi.pyz').exists())
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_build_refuses_output_in_source_tree(self):
        result = self.run_command([sys.executable, '-I', str(self.repo / 'scripts/build_runtime.py'),
                                   '--output', str(self.repo / 'gdi')], expected=1)
        self.assertIn('must be git-ignored', result.stderr)
        self.assertFalse((self.repo / 'gdi/gdi.pyz').exists())
        self.assertEqual(self.git('status', '--porcelain'), '')

    @unittest.skipUnless(shutil.which('rclone'), 'requires rclone with local backend')
    def test_broken_runtime_is_rejected_before_publication(self):
        self.initialize_remote()
        with (self.repo / 'gdi/cli.py').open('a') as handle:
            handle.write('\nraise RuntimeError("broken runtime fixture")\n')
        self.commit()
        result = self.deploy('--runtime-dir', self.runtime_url, expected=1)
        self.assertIn('runtime smoke checks', result.stderr)
        self.assertFalse(list((self.store / 'branches').rglob('*.json')))
        self.assertFalse((self.store / 'runtime').exists())

    @unittest.skipUnless(shutil.which('rclone'), 'requires rclone with local backend')
    def test_full_deploy_retry_update_gc_and_clone_using_only_zipapp(self):
        self.initialize_remote()
        self.deploy('--runtime-dir', self.runtime_url)
        runtime = self.store / 'runtime'
        first_data = (runtime / 'gdi.pyz').read_bytes()
        manifests = list((self.store / 'branches/main').glob('*.json'))
        self.assertEqual(len(manifests), 1)
        self.deploy('--runtime-dir', self.runtime_url)
        self.assertEqual(list((self.store / 'branches/main').glob('*.json')), manifests)
        self.assertEqual((runtime / 'gdi.pyz').read_bytes(), first_data)
        (self.repo / 'example.txt').write_text('new committed contents\n')
        self.commit()
        self.cli('push', 'drive')  # A delta first; deploy must still publish a full checkpoint.
        self.deploy('--runtime-dir', self.runtime_url)
        metadata = json.loads((runtime / 'runtime.json').read_text())
        self.assertEqual(metadata['head'], self.head)
        self.assertNotEqual((runtime / 'gdi.pyz').read_bytes(), first_data)
        before = {path.name: path.read_bytes() for path in runtime.iterdir()}
        self.cli('gc', 'drive', '--apply', '--quiescent', '--keep-checkpoints', '1')
        self.assertEqual({path.name: path.read_bytes() for path in runtime.iterdir()}, before)
        snapshot = self.snapshot()  # Includes runtime in root listing; no runtime listing/downloads needed.
        shutil.rmtree(self.repo)
        executable = [sys.executable, '-I', str(self.output / 'gdi.pyz'), 'agent']
        result = self.run_command([*executable, 'snapshot', '--snapshot', str(snapshot),
                                   '--repository-id', self.identity], cwd=self.root)
        self.assertEqual(json.loads(result.stdout)['head'], self.head)
        destination = self.root / 'restored'
        result = self.run_command([*executable, 'clone', '--snapshot', str(snapshot), '--repository-id', self.identity,
                                   '--destination', str(destination)], cwd=self.root)
        self.assertEqual(json.loads(result.stdout)['head'], self.head)
        self.assertEqual((destination / 'example.txt').read_text(), 'new committed contents\n')
        self.assertEqual(self.run_command(['git', 'rev-parse', 'HEAD'], cwd=destination).stdout.strip(), self.head)

    @unittest.skipUnless(shutil.which('rclone'), 'requires rclone with local backend')
    def test_failed_publication_does_not_upload_runtime(self):
        self.initialize_remote()
        repository = json.loads((self.store / 'repository.json').read_text())
        repository['repository_id'] = 'f' * 32
        (self.store / 'repository.json').write_text(json.dumps(repository))
        result = self.deploy('--runtime-dir', self.runtime_url, expected=1)
        self.assertIn('failed during full GDI publication', result.stderr)
        self.assertTrue((self.output / 'gdi.pyz').exists())
        self.assertFalse((self.store / 'runtime').exists())

    @unittest.skipUnless(shutil.which('rclone'), 'requires rclone with local backend')
    def test_upload_failure_and_corrupt_download_are_reported_and_retry_keeps_publication(self):
        self.initialize_remote()
        real_rclone = shutil.which('rclone')
        original_path = self.env['PATH']
        tools = self.root / 'tools'
        tools.mkdir()
        wrapper = tools / 'rclone'
        wrapper.write_text(f'''#!{sys.executable}
import pathlib, subprocess, sys
args = sys.argv[1:]
if args[0] == 'copyto' and args[1].endswith('/gdi.pyz') and not args[1].startswith('gdideploytest:'):
    raise SystemExit(23)
raise SystemExit(subprocess.call([{real_rclone!r}, *args]))
''')
        wrapper.chmod(0o755)
        self.env['PATH'] = str(tools) + os.pathsep + original_path
        result = self.deploy('--runtime-dir', self.runtime_url, expected=23)
        self.assertIn('failed during runtime upload', result.stderr)
        publications = list((self.store / 'branches/main').glob('*.json'))
        self.assertEqual(len(publications), 1)
        self.assertFalse((self.store / 'runtime/runtime.json').exists())
        wrapper.write_text(f'''#!{sys.executable}
import pathlib, subprocess, sys
args = sys.argv[1:]
code = subprocess.call([{real_rclone!r}, *args])
if code == 0 and args[0] == 'copyto' and args[1].startswith('gdideploytest:') and args[1].endswith('/gdi.pyz'):
    with pathlib.Path(args[2]).open('ab') as handle:
        handle.write(b'corrupted download')
raise SystemExit(code)
''')
        result = self.deploy('--runtime-dir', self.runtime_url, expected=1)
        self.assertIn('failed during runtime download verification', result.stderr)
        self.assertNotIn('deploy: complete', result.stdout)
        self.env['PATH'] = original_path
        self.deploy('--runtime-dir', self.runtime_url)
        self.assertEqual(list((self.store / 'branches/main').glob('*.json')), publications)
        self.assertEqual((self.store / 'runtime/gdi.pyz').read_bytes(), (self.output / 'gdi.pyz').read_bytes())
