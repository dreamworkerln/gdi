"""CI tool cache reuse, version changes and incomplete installations."""

from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


spec = importlib.util.spec_from_file_location('gdi_ci_tools', Path(__file__).resolve().parents[1] / 'scripts/ci_tools.py')
ci_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_tools)


class CiToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-ci-tools-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, name, version):
        data = f'#!/bin/sh\nprintf "%s\\n" "{name} version {version}"\n'.encode()
        if name == 'rclone':
            data = f'#!/bin/sh\nprintf "%s\\n" "rclone v{version}"\n'.encode()
        result = io.BytesIO()
        if name == 'act':
            with tarfile.open(fileobj=result, mode='w:gz') as archive:
                member = tarfile.TarInfo('act')
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        else:
            with zipfile.ZipFile(result, 'w') as archive:
                archive.writestr(f'rclone-v{version}-linux-amd64/rclone', data)
        return result.getvalue()

    def test_cold_install_and_warm_reuse_never_download_again(self):
        for name, version in (('act', '0.2.89'), ('rclone', '1.75.1')):
            with self.subTest(name=name), redirect_stdout(io.StringIO()):
                with patch.object(ci_tools, 'urlopen', return_value=io.BytesIO(self.archive(name, version))) as download:
                    target = ci_tools.install(name, version, self.root, 'amd64')
                    self.assertEqual(download.call_count, 1)
                with patch.object(ci_tools, 'urlopen', side_effect=AssertionError('unexpected network access')):
                    self.assertEqual(ci_tools.install(name, version, self.root, 'amd64'), target)
                expected = ('act version ' if name == 'act' else 'rclone v') + version
                self.assertTrue(ci_tools.matches(target, expected))

    def test_new_version_uses_its_own_cache_entry(self):
        with redirect_stdout(io.StringIO()):
            for version in ('0.2.89', '0.2.90'):
                with patch.object(ci_tools, 'urlopen', return_value=io.BytesIO(self.archive('act', version))):
                    ci_tools.install('act', version, self.root, 'amd64')
        self.assertTrue(ci_tools.matches(self.root / 'act-0.2.89/act', 'act version 0.2.89'))
        self.assertTrue(ci_tools.matches(self.root / 'act-0.2.90/act', 'act version 0.2.90'))

    def test_failed_download_or_wrong_version_is_not_published(self):
        for error in ('interrupted', 'wrong-version'):
            with self.subTest(error=error), redirect_stdout(io.StringIO()):
                options = ({'side_effect': OSError('connection lost')} if error == 'interrupted' else
                           {'return_value': io.BytesIO(self.archive('act', '0.2.88'))})
                with patch.object(ci_tools, 'urlopen', **options), self.assertRaises((OSError, ValueError)):
                    ci_tools.install('act', '0.2.89', self.root, 'amd64')
                self.assertFalse((self.root / 'act-0.2.89/act').exists())
                self.assertFalse(list((self.root / 'act-0.2.89').glob('.install-*')))

    def test_main_exports_cached_tools_to_github_steps(self):
        environment = {'GDI_CI_ACT_VERSION': '0.2.89', 'GDI_CI_RCLONE_VERSION': '1.75.1',
                       'GITHUB_PATH': str(self.root / 'path'), 'GITHUB_ENV': str(self.root / 'env')}
        with patch.dict(os.environ, environment), patch.object(ci_tools.Path, 'home', return_value=self.root), \
                patch.object(ci_tools.platform, 'system', return_value='Linux'), \
                patch.object(ci_tools.platform, 'machine', return_value='x86_64'), \
                patch.object(ci_tools, 'install', side_effect=lambda name, version, root, arch: root / (name + '-' + version) / name):
            ci_tools.main()
        self.assertIn('/act-0.2.89\n', (self.root / 'path').read_text())
        self.assertIn('/rclone-1.75.1\n', (self.root / 'path').read_text())
        self.assertIn('/act-0.2.89/act\n', (self.root / 'env').read_text())
