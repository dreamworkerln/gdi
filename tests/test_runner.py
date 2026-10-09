import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gdi.exchange import encode
from gdi.git import GdiError
from gdi.runner import cleanup, resolve_profile
from gdi.worker_config import workflow_config
from gdi.transport import Rclone
import os
import shutil


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='gdi-runner-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.act = self.root / 'act'
        self.act.write_text('fake executable')
        self.act.chmod(0o700)
        self.profile = {'workflow': workflow_config({'executable': str(self.act),
                        'platforms': {'ubuntu-latest': 'example:latest'}}),
                        'env': {}, 'artifacts': [], 'timeout_seconds': 10, 'revision': 'a' * 64}

    def test_actual_binary_and_images_change_revision_and_repeated_resolution_is_stable(self):
        image = ['sha256:' + '1' * 64]
        def command(argv, **kwargs):
            if argv[-1] == '--version':
                return 'act version 0.2.89'
            if argv[1] == 'info':
                return '{"ID":"daemon", "ServerVersion":"28.0"}'
            if argv[1] == 'context':
                return 'unix:///run/docker.sock'
            return image[0]
        with patch('gdi.runner.command', side_effect=command):
            first = resolve_profile(copy.deepcopy(self.profile), self.root)
            self.assertEqual(first['workflow']['platforms']['ubuntu-latest'], image[0])
            self.assertEqual(resolve_profile(first, self.root)['revision'], first['revision'])
            image[0] = 'sha256:' + '2' * 64
            second = resolve_profile(first, self.root)
            self.assertNotEqual(second['revision'], first['revision'])
            self.act.write_text('new binary of same version')
            self.assertNotEqual(resolve_profile(first, self.root)['revision'], second['revision'])

    def test_wrong_act_version_is_rejected(self):
        with patch('gdi.runner.command', return_value='act version 0.2.90'), self.assertRaisesRegex(GdiError, 'version mismatch'):
            resolve_profile(self.profile, self.root)

    def owner(self):
        checkout = self.root / 'checkout'
        checkout.mkdir()
        (self.root / 'docker-owner.json').write_bytes(encode({'checkout': str(checkout),
             'executable': 'docker', 'daemon': {'id': 'original', 'version': '28.0'}}))
        return checkout

    def test_cleanup_removes_only_act_containers_with_the_owned_working_directory(self):
        checkout = self.owner()
        removed = []
        containers = {'1' * 64: ('/act-owned', str(checkout)),
                      '2' * 64: ('/act-other', '/another/checkout'),
                      '3' * 64: ('/user-container', str(checkout))}
        import json
        def command(argv, **kwargs):
            if argv[1] == 'info':
                return '{"ID":"original", "ServerVersion":"28.1"}'
            if argv[1] == 'ps':
                return '\n'.join(containers)
            if argv[1] == 'inspect':
                return ' '.join(json.dumps(v) for v in containers[argv[-1]])
            removed.append(argv[-1])
            return ''
        with patch('gdi.runner.command', side_effect=command):
            cleanup(self.root)
        self.assertEqual(removed, ['1' * 64])

    def test_changed_daemon_blocks_cleanup_and_retains_ownership(self):
        self.owner()
        with patch('gdi.runner.command', return_value='{"ID":"other", "ServerVersion":"28.0"}'), \
                self.assertRaisesRegex(GdiError, 'original Docker daemon'):
            cleanup(self.root)
        self.assertTrue((self.root / 'docker-owner.json').exists())

    @unittest.skipUnless(shutil.which('rclone'), 'requires rclone')
    def test_optional_rclone_read_distinguishes_absence_empty_file_and_directory(self):
        with patch.dict(os.environ, {'RCLONE_CONFIG': os.devnull, 'RCLONE_CONFIG_GDIOPTIONAL_TYPE': 'local'}):
            transport = Rclone('gdioptional:' + str(self.root))
            self.assertIsNone(transport.read_optional('missing.json'))
            (self.root / 'empty.json').touch()
            self.assertEqual(transport.read_optional('empty.json'), b'')
            (self.root / 'directory.json').mkdir()
            with self.assertRaisesRegex(GdiError, 'regular remote metadata'):
                transport.read_optional('directory.json')
