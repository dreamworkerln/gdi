"""Offline publication artifacts interoperate with the ordinary GDI reader."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gdi.cache import VerifiedCache
from gdi.exchange import Exchange
from gdi.git import GdiError, Git
from gdi.publication import encode, prepare_publication, verify_bundle
from tests.test_exchange import MemoryTransport


class PublicationCoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gdi-publication-core-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.branch = 'feature/ветка%25'
        self.ref = 'refs/heads/' + self.branch
        self.git = self.repo('source')
        self.identity = '1' * 32
        self.first = self.commit('first')

    def repo(self, name):
        path = self.root / name
        path.mkdir()
        git = Git(path)
        git.call('init', '-q', '-b', self.branch)
        return Git.discover(path)

    def commit(self, text):
        (self.git.path / 'file.txt').write_text(text + '\n')
        self.git.call('add', 'file.txt')
        self.git.call('-c', 'user.name=GDI Core Test', '-c', 'user.email=test@example.invalid',
                      'commit', '-qm', text)
        return self.git.oid('HEAD')

    def prepare(self, name, *, previous=None, incremental=False, nonce='a' * 32):
        return prepare_publication(self.git, self.identity, self.ref, self.git.oid('HEAD'),
                                   self.root / name, previous=previous,
                                   incremental=incremental, nonce=nonce)

    def receive(self, publications):
        store = MemoryTransport()
        store.data['repository.json'] = encode({'version': 3, 'repository_id': self.identity,
                                               'object_format': 'sha1'})
        store.mkdir('bundles')
        store.mkdir('branches/feature%2Fветка%2525')
        for prepared in publications:
            store.data[prepared.bundle_relative] = prepared.bundle.read_bytes()
            store.data[prepared.manifest_relative] = prepared.manifest.read_bytes()
        receiver = self.repo('receiver')
        exchange = Exchange(receiver, lambda url: store)
        exchange.add('drive', 'memory:project', expected_id=self.identity)
        head, _, _ = exchange.pull('drive')
        self.assertEqual(head, self.git.oid('HEAD'))
        self.assertEqual((receiver.path / 'file.txt').read_bytes(),
                         (self.git.path / 'file.txt').read_bytes())

    def test_offline_full_bundle_is_accepted_by_ordinary_pull(self):
        before = self.git.snapshot()
        refs = self.git.text('show-ref')
        with patch('gdi.transport.Rclone.call', side_effect=AssertionError('no rclone')), \
                patch('gdi.rc_transport.RcServer.__enter__', side_effect=AssertionError('no RC')):
            prepared = self.prepare('full')
            with verify_bundle(prepared.bundle, prepared.data, self.ref) as quarantine:
                self.assertEqual(Git(quarantine, isolated=True).oid('refs/heads/incoming'), self.first)
            self.assertFalse(quarantine.exists())
        self.assertEqual(self.git.snapshot(), before)
        self.assertEqual(self.git.text('show-ref'), refs)
        self.assertFalse((self.git.path / '.gdi').exists())
        self.assertEqual(prepared.manifest.read_bytes(), prepared.manifest_bytes)
        self.assertEqual(prepared.publication_id, hashlib.sha256(prepared.manifest_bytes).hexdigest())
        self.assertEqual(prepared.data['nonce'], 'a' * 32)
        self.assertEqual(prepared.data['prerequisites'], [])
        self.assertTrue(prepared.manifest_relative.startswith('branches/feature%2Fветка%2525/'))
        self.receive([prepared])

    def test_incremental_bundle_uses_verified_base_without_accepting_new_cache_ref(self):
        full = self.prepare('full')
        cache = VerifiedCache(self.git, self.identity)
        with verify_bundle(full.bundle, full.data, self.ref) as quarantine:
            cache.accept(full.publication_id, self.first, quarantine)
        second = self.commit('second')
        incremental = self.prepare('incremental', previous=(full.publication_id, full.data),
                                   incremental=True, nonce='b' * 32)
        with verify_bundle(incremental.bundle, incremental.data, self.ref, cache, full.data) as quarantine:
            verified = Git(quarantine, isolated=True)
            self.assertEqual(verified.oid('refs/heads/incoming'), second)
            self.assertTrue(verified.ancestor(self.first, second))
        self.assertFalse(cache.contains(incremental.publication_id, second))
        self.assertEqual(incremental.data['base_publication'], full.publication_id)
        with self.assertRaisesRegex(GdiError, 'verified base cache'):
            with verify_bundle(incremental.bundle, incremental.data, self.ref):
                self.fail('incremental verification must require a base')
        self.receive([full, incremental])

    def test_modified_bundle_fails_quarantine_before_acceptance(self):
        prepared = self.prepare('full')
        content = bytearray(prepared.bundle.read_bytes())
        content[-1] ^= 1
        prepared.bundle.write_bytes(content)
        with self.assertRaisesRegex(GdiError, 'SHA256 mismatch'):
            with verify_bundle(prepared.bundle, prepared.data, self.ref):
                self.fail('corrupt bundle must not be accepted')
        self.assertEqual(self.git.oid('HEAD'), self.first)
        self.assertFalse((self.git.path / '.gdi').exists())

    def test_wrong_base_identity_or_divergent_history_is_rejected(self):
        full = self.prepare('full')
        for field, value in (('repository_id', '2' * 32), ('ref', 'refs/heads/other')):
            base = {**full.data, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(GdiError, 'identity'):
                self.prepare('bad-base', previous=(full.publication_id, base))
        second = self.commit('second')
        later = self.prepare('later', previous=(full.publication_id, full.data))
        self.git.call('reset', '--hard', self.first)
        self.commit('divergent')
        with self.assertRaisesRegex(GdiError, 'not a fast-forward'):
            self.prepare('divergent', previous=(later.publication_id, later.data))
        self.assertFalse((self.root / 'divergent').exists())
        self.assertNotEqual(self.git.oid('HEAD'), second)

    def test_stale_source_head_and_existing_artifacts_are_not_overwritten(self):
        prepared = self.prepare('full')
        bundle, manifest = prepared.bundle.read_bytes(), prepared.manifest.read_bytes()
        with self.assertRaisesRegex(GdiError, 'output already exists'):
            self.prepare('full', nonce='b' * 32)
        self.assertEqual(prepared.bundle.read_bytes(), bundle)
        self.assertEqual(prepared.manifest.read_bytes(), manifest)
        self.commit('second')
        with self.assertRaisesRegex(GdiError, 'branch changed'):
            prepare_publication(self.git, self.identity, self.ref, self.first, self.root / 'stale')
        self.assertFalse((self.root / 'stale').exists())


if __name__ == '__main__':
    unittest.main()
