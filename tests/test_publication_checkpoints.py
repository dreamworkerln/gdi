"""Checkpoint rotation, interrupted archival, bounded reads and historical identity."""

from unittest.mock import patch

from gdi import agent
from gdi.ci import publication
from gdi.git import GdiError
from gdi.publication import PreparedPublication, decode, digest, encode, prepare_publication
from gdi import publication_checkpoints as checkpoints
from gdi.snapshot import Snapshot
from tests.snapshots import export_snapshot
from tests.test_exchange import ExchangeTestCase


class PublicationCheckpointTests(ExchangeTestCase):
    def setUp(self):
        super().setUp()
        checkpoints.enable(self.ea, 'drive', quiescent=True, all_clients_updated=True)

    def test_5000_publications_require_only_the_active_metadata_to_read_tip(self):
        ref, branch = 'refs/heads/main', 'main/'
        active, archives, marker, previous = {}, {}, None, None
        chain = checkpoints.PublicationChain()
        for number in range(1, 5001):
            full = not chain or len(chain) >= 5
            data = {'version': 3, 'repository_id': self.identity, 'ref': ref,
                    'head': f'{number:040x}', 'nonce': f'{number:032x}',
                    'bundle_sha256': digest(str(number).encode()), 'bundle_bytes': 100,
                    'bundle_kind': 'full' if full else 'incremental',
                    'previous': previous, 'base_publication': previous if not full else None,
                    'base_head': chain[-1][1]['head'] if not full else None,
                    'prerequisites': [chain[-1][1]['head']] if not full else []}
            raw = encode(data)
            identity = digest(raw)
            if full:
                prepared = PreparedPublication(self.root / 'unused', self.root / 'manifest', raw)
                marker, blobs, _ = checkpoints.prepare(chain, prepared,
                    lambda path: active[path.removeprefix('branches/')], self.root)
                archives.update({path: source.read_bytes() for path, source in blobs.items()})
                active.clear()
            active[branch + identity + '.json'] = raw
            listing = [{'Path': path, 'IsDir': False} for path in active]
            chain = checkpoints.live_chain(self.identity, ref, listing, active, marker,
                                           lambda _: self.fail('live reader fetched an archive'))
            previous = identity
            if number in (50, 500, 5000):
                self.assertLessEqual(len(active), 5)
                self.assertEqual(chain[-1][1]['head'], f'{number:040x}')
        full = checkpoints.history_chain(chain, archives.__getitem__)
        self.assertEqual(len(full), 5000)

    def test_activation_reuses_existing_full_publication_and_head(self):
        self.commit(self.a, 'first full')
        before = self.ea.push('drive')[0:2]
        marker = decode(self.store.read('branches/main/checkpoint.json'))
        self.assertEqual(marker['root'], before[1])
        self.assertEqual(self.ea.push('drive', full=True)[0:2], before)
        self.assertEqual(self.a.oid('HEAD'), before[0])

    def test_rotation_bounds_live_metadata_and_preserves_full_history(self):
        publications = []
        for number in range(20):
            self.commit(self.a, str(number))
            head, identity, _ = self.ea.push('drive', checkpoint_every=3)
            publications.append((head, identity))
            live = self.ea.publications(self.store, self.identity, 'refs/heads/main')
            self.assertLessEqual(len(live), 3)
            self.assertLessEqual(len(self.store.list('branches/main')), 4)
        old, history = publication(self.ea, self.store, self.identity, publications[0][1])
        self.assertEqual(old['head'], publications[0][0])
        self.assertEqual([identity for identity, _ in history], [identity for _, identity in publications])
        self.assertEqual(self.eb.fetch('drive')[0], self.a.oid('HEAD'))

    def test_live_reader_does_not_read_the_archive_after_cleanup(self):
        self.ea.push('drive')
        self.commit(self.a, 'next')
        self.ea.push('drive', full=True)
        original = self.store.read
        def no_archive(path):
            if path.startswith(checkpoints.ARCHIVE):
                raise AssertionError('ordinary restore must not read historical archives')
            return original(path)
        with patch.object(self.store, 'read', side_effect=no_archive):
            self.assertEqual(self.eb.fetch('drive')[0], self.a.oid('HEAD'))

    def test_interrupted_marker_update_and_partial_cleanup_are_resumable(self):
        self.ea.push('drive')
        self.commit(self.a, 'delta'); self.ea.push('drive')
        self.commit(self.a, 'full')
        original = self.store.update_checkpoint
        def lost_ack(source, path):
            original(source, path)
            raise GdiError('lost checkpoint acknowledgement')
        with patch.object(self.store, 'update_checkpoint', side_effect=lost_ack):
            with self.assertRaises(GdiError):
                self.ea.push('drive', full=True)
        marker = self.store.read('branches/main/checkpoint.json')
        self.assertEqual(self.eb.fetch('drive')[0], self.a.oid('HEAD'))
        self.ea.push('drive', full=True)
        self.assertEqual(self.store.read('branches/main/checkpoint.json'), marker)
        self.assertEqual(len(self.store.list('branches/main')), 2)
        self.commit(self.a, 'again')
        delete = self.store.delete_publication
        def lost_delete(path):
            delete(path)
            raise GdiError('lost delete acknowledgement')
        with patch.object(self.store, 'delete_publication', side_effect=lost_delete):
            with self.assertRaises(GdiError):
                self.ea.push('drive', full=True)
        self.ea.push('drive')
        self.assertEqual(self.eb.fetch('drive')[0], self.a.oid('HEAD'))

    def test_missing_archive_before_activation_keeps_old_chain_readable(self):
        self.ea.push('drive')
        self.commit(self.a, 'next')
        old_marker = self.store.read('branches/main/checkpoint.json')
        self.store.fail = checkpoints.ARCHIVE
        with self.assertRaises(GdiError):
            self.ea.push('drive', full=True)
        self.store.fail = None
        self.assertEqual(self.store.read('branches/main/checkpoint.json'), old_marker)
        self.assertEqual(self.eb.fetch('drive')[0], self.first)

    def test_stale_successor_is_not_hidden_by_checkpoint(self):
        self.ea.push('drive')
        before = self.ea.publications(self.store, self.identity, 'refs/heads/main')
        self.commit(self.a, 'stale')
        prepared = prepare_publication(self.a, self.identity, 'refs/heads/main',
                                       self.a.oid('HEAD'), self.root / 'stale', previous=before[-1])
        self.ea.push('drive', full=True)
        self.store.upload(prepared.manifest, prepared.manifest_relative)
        with self.assertRaisesRegex(GdiError, 'conflict|unarchived'):
            self.eb.fetch('drive')

    def test_archive_hash_mismatch_blocks_cleanup_and_history(self):
        self.ea.push('drive')
        self.commit(self.a, 'next')
        with patch.object(self.store, 'delete_publication', side_effect=GdiError('pause cleanup')):
            with self.assertRaises(GdiError):
                self.ea.push('drive', full=True)
        marker = decode(self.store.read('branches/main/checkpoint.json'))
        path = checkpoints.archive_path(marker['history'])
        self.store.data[path] = b'{}\n'
        with self.assertRaisesRegex(GdiError, 'SHA256'):
            self.ea.push('drive')
        self.assertGreater(len(self.store.list('branches/main')), 2)

    def test_gc_refuses_a_checkpoint_change_during_bundle_deletion(self):
        for number in range(6):
            self.commit(self.a, 'gc ' + str(number))
            self.ea.push('drive', checkpoint_every=2)
        marker_path = 'branches/main/checkpoint.json'
        original_marker = self.store.read(marker_path)
        delete = self.store.delete_bundle
        def change_checkpoint(path):
            delete(path)
            marker = decode(original_marker)
            marker['previous_head'] = 'f' * 40
            self.store.data[marker_path] = encode(marker)
        with patch.object(self.store, 'delete_bundle', side_effect=change_checkpoint):
            with self.assertRaisesRegex(GdiError, 'remote changed or deletion was not reflected'):
                self.ea.gc('drive', apply=True, quiescent=True, keep_checkpoints=1,
                           bundle_only=True, report=lambda _: None)
        self.assertNotEqual(self.store.read(marker_path), original_marker)

    def test_existing_legacy_chain_rotates_without_changing_repository_or_commits(self):
        repository = decode(self.store.read('repository.json'))
        repository.pop(checkpoints.CAPABILITY)
        self.store.data['repository.json'] = encode(repository)
        ids = []
        for number in range(4):
            self.commit(self.a, 'legacy ' + str(number))
            ids.append(self.ea.push('drive')[1])
        checkpoints.enable(self.ea, 'drive', quiescent=True, all_clients_updated=True)
        self.ea.push('drive', full=True)
        history = self.ea.publications(self.store, self.identity, 'refs/heads/main', history=True)
        self.assertEqual([identity for identity, _ in history[:4]], ids)
        self.assertEqual(decode(self.store.read('repository.json'))['repository_id'], self.identity)
        self.assertEqual(self.eb.fetch('drive')[0], self.a.oid('HEAD'))

    def test_offline_plan_archives_then_commits_checkpoint_then_cleans_up(self):
        self.ea.push('drive')
        self.commit(self.a, 'offline')
        snapshot = export_snapshot(self.root / 'before', self.store)
        output = self.root / 'plan'
        plan = agent.prepare(self.a, snapshot, self.identity, output, full=True)
        self.assertEqual(plan['next_step'], 'upload_archives_and_bundle')
        def upload(key, mutable=False):
            item = plan['files'][key]
            target = item['target_folder'].removeprefix('project/') + '/' + item['target_name']
            (self.store.update_checkpoint if mutable else self.store.upload)(item['local_path'], target)
        for key in plan['files']:
            if key.startswith('archive_'):
                upload(key)
        upload('bundle')
        checked = export_snapshot(self.root / 'checked', self.store, include=['', 'branches', 'branches/main', 'bundles'])
        self.assertTrue(agent.check(output, checked, self.identity)['safe_to_upload_manifest'])
        upload('manifest')
        first = export_snapshot(self.root / 'manifest', self.store, include=['', 'branches', 'branches/main', 'bundles'])
        self.assertTrue(agent.accept(output, first, self.identity)['safe_to_upload_checkpoint'])
        upload('checkpoint', mutable=True)
        second = export_snapshot(self.root / 'checkpoint', self.store, include=['', 'branches', 'branches/main', 'bundles'])
        result = agent.accept(output, second, self.identity)
        self.assertEqual(result['state'], 'checkpoint_accepted')
        for path in result['cleanup_paths']:
            self.store.delete_publication(path)
        final = export_snapshot(self.root / 'accepted', self.store, include=['', 'branches', 'branches/main', 'bundles'])
        self.assertEqual(agent.accept(output, final, self.identity)['state'], 'accepted')
