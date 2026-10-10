import copy
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from gdi import agent
from gdi.cli import main
from gdi.exchange import decode, encode, digest
from gdi.git import GdiError, Git
from gdi.snapshot import Snapshot
from tests.snapshots import export_snapshot
from tests.test_exchange import ExchangeTestCase


class AgentTests(ExchangeTestCase):
    def snapshot(self, name='snapshot', *, bundles=False):
        return export_snapshot(self.root / name, self.store,
                               include=['', 'branches', 'branches/main'] + (['bundles'] if bundles else []))

    def transfer(self, value, *names):
        for name in names:
            item = value['files'][name]
            target = item['target_folder'] + '/' + item['target_name']
            if name != 'notification':
                target = target.removeprefix('project/')
                self.store.mkdir(target.rpartition('/')[0])
            self.store.upload(item['local_path'], target)

    def test_prepare_retry_check_accept_and_native_pull_without_rclone(self):
        self.ea.push('drive')
        head = self.commit(self.a, 'agent change')
        source = self.snapshot('before')
        output = self.root / 'plan'
        with patch('gdi.transport.Rclone.call', side_effect=AssertionError('no rclone')), \
                patch('gdi.rc_transport.RcServer.__enter__', side_effect=AssertionError('no RC')):
            value = agent.prepare(self.a, source, self.identity, output)
            self.assertTrue(value['safe_to_upload_bundle'])
            self.assertFalse(value['safe_to_upload_manifest'])
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            self.assertEqual(agent.prepare(self.a, source, self.identity, output), value)
            self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})
            self.transfer(value, 'bundle')
            checked = agent.check(output, self.snapshot('bundle', bundles=True), self.identity)
            self.assertTrue(checked['safe_to_upload_manifest'])
            self.transfer(checked, 'manifest')
            accepted = agent.accept(output, self.snapshot('manifest', bundles=True), self.identity)
            self.assertEqual(accepted['state'], 'accepted')
            self.assertTrue(accepted['safe_to_upload_notification'])
            self.assertEqual(agent.accept(output, self.snapshot('retry', bundles=True), self.identity)['state'], 'accepted')
        self.assertEqual(self.eb.fetch('drive')[0], head)
        self.assertEqual(self.b.oid('HEAD'), self.first)
        self.assertEqual(self.eb.pull('drive')[0], head)
        self.assertEqual(self.eb.git.path.joinpath('file.txt').read_text(), 'agent change\n')

    def test_initial_empty_chain_and_clone_full_and_incremental(self):
        value = agent.prepare(self.a, self.snapshot('empty'), self.identity, self.root / 'plan')
        self.assertIsNone(value['expected_previous_publication'])
        self.transfer(value, 'bundle')
        agent.check(self.root / 'plan', self.snapshot('bundle', bundles=True), self.identity)
        self.transfer(value, 'manifest')
        agent.accept(self.root / 'plan', self.snapshot('full', bundles=True), self.identity)
        self.commit(self.a, 'second')
        self.ea.push('drive')
        snap = self.snapshot('incremental', bundles=True)
        result = agent.clone(snap, self.identity, self.root / 'clone')
        cloned = Git.discover(result['destination'])
        self.assertEqual(cloned.oid('HEAD'), self.a.oid('HEAD'))
        self.assertEqual(cloned.branch(), 'main')
        self.assertEqual((cloned.path / 'file.txt').read_text(), 'second\n')
        with self.assertRaisesRegex(GdiError, 'must not exist'):
            agent.clone(snap, self.identity, self.root / 'clone')

    def test_small_delta_is_self_contained_and_accepted_by_native_pull(self):
        import os, shutil
        (self.a.path / 'large.bin').write_bytes(os.urandom(256 * 1024))
        self.a.call('add', 'large.bin')
        self.a.call('-c', 'user.name=Gdi Test', '-c', 'user.email=gdi@example.invalid',
                    'commit', '-qm', 'large base')
        self.ea.push('drive')
        base_head = self.a.oid('HEAD')
        head = self.commit(self.a, 'small TODO change')
        before = self.snapshot('before')
        output = self.root / 'delta-plan'
        value = agent.prepare(self.a, before, self.identity, output)
        full = agent.prepare(self.a, before, self.identity, self.root / 'full-plan', full=True)
        metadata = decode((output / 'publication.json').read_bytes())
        self.assertEqual(value['bundle_kind'], 'incremental')
        self.assertEqual(metadata['base_head'], base_head)
        self.assertEqual(metadata['base_publication'], value['expected_previous_publication'])
        self.assertTrue(metadata['prerequisites'])
        self.assertLess(value['files']['bundle']['bytes'], full['files']['bundle']['bytes'] // 20)
        self.assertEqual(agent.prepare(self.a, before, self.identity, output, full=True), value)
        self.assertNotIn('local_base', value['files'])
        # check/accept must survive removal of the source and its verified cache.
        shutil.rmtree(self.a.path)
        self.transfer(value, 'bundle')
        bundle_path = 'bundles/' + metadata['bundle_sha256'] + '.bundle'
        def downloaded_snapshot(name):
            manifests = [path for path in self.store.data if path.startswith('branches/main/')]
            return export_snapshot(self.root / name, self.store,
                include=['', 'branches', 'branches/main', 'bundles'],
                downloads=['repository.json', bundle_path] + manifests)
        checked = agent.check(output, downloaded_snapshot('bundle'), self.identity)
        self.assertTrue(checked['safe_to_upload_manifest'])
        self.transfer(value, 'manifest')
        self.assertEqual(agent.accept(output, downloaded_snapshot('manifest'), self.identity)['state'], 'accepted')
        self.assertEqual(self.eb.pull('drive')[0], head)
        self.assertEqual((self.b.path / 'large.bin').stat().st_size, 256 * 1024)

    def test_incremental_base_damage_and_wrong_base_do_not_grant_permission(self):
        self.ea.push('drive')
        self.commit(self.a, 'second')
        output = self.root / 'plan'
        value = agent.prepare(self.a, self.snapshot('before'), self.identity, output)
        self.transfer(value, 'bundle')
        snap = self.snapshot('bundle', bundles=True)
        base = output / 'base.bundle'
        raw = base.read_bytes()
        damaged = raw[:-1] + bytes([raw[-1] ^ 1])
        base.write_bytes(damaged)
        with self.assertRaisesRegex(GdiError, 'base bundle checksum'):
            agent.check(output, snap, self.identity)
        # Even a rewritten local checksum cannot replace the declared base with another ref/HEAD.
        self.a.call('bundle', 'create', str(self.root / 'wrong.bundle'), 'refs/heads/main')
        base.write_bytes((self.root / 'wrong.bundle').read_bytes())
        plan = decode((output / 'plan.json').read_bytes())
        saved = copy.deepcopy(plan)
        plan['local_base'].update(sha256=digest(base.read_bytes()), bytes=base.stat().st_size)
        (output / 'plan.json').write_bytes(encode(plan))
        with self.assertRaisesRegex(GdiError, 'exact HEAD'):
            agent.check(output, snap, self.identity)
        self.assertEqual(decode((output / 'plan.json').read_bytes())['state'], 'prepared')
        base.write_bytes(raw)
        (output / 'plan.json').write_bytes(encode(saved))
        self.assertTrue(agent.check(output, snap, self.identity)['safe_to_upload_manifest'])

    def test_checkpoint_cadence_forced_full_and_existing_full_plan_retry(self):
        self.ea.push('drive')
        for number, expected in enumerate(('incremental', 'full', 'incremental')):
            self.commit(self.a, f'change {number}')
            output = self.root / f'plan-{number}'
            before = self.snapshot(f'before-{number}')
            value = agent.prepare(self.a, before, self.identity, output, checkpoint_every=2)
            self.assertEqual(value['bundle_kind'], expected)
            self.transfer(value, 'bundle', 'manifest')
            snap = self.snapshot(f'after-{number}', bundles=True)
            self.assertEqual(agent.accept(output, snap, self.identity)['state'], 'accepted')
            self.assertEqual(self.eb.fetch('drive')[0], self.a.oid('HEAD'))
        # Promote an already-published incremental tip to a full checkpoint, like push --full.
        output = self.root / 'forced'
        value = agent.prepare(self.a, self.snapshot('force'), self.identity, output, full=True)
        self.assertEqual(value['bundle_kind'], 'full')
        self.assertNotIn('local_base', decode((output / 'plan.json').read_bytes()))
        self.assertEqual(agent.prepare(self.a, self.snapshot('force-retry'), self.identity, output), value)
        self.transfer(value, 'bundle')
        self.assertTrue(agent.check(output, self.snapshot('forced-bundle', bundles=True), self.identity)['safe_to_upload_manifest'])
        self.transfer(value, 'manifest')
        self.assertEqual(agent.accept(output, self.snapshot('forced-manifest', bundles=True), self.identity)['state'], 'accepted')
        with self.assertRaisesRegex(GdiError, 'positive integer'):
            agent.prepare(self.a, self.snapshot('invalid'), self.identity, self.root / 'invalid', checkpoint_every=0)

    def test_extra_objects_in_local_base_cannot_hide_an_incomplete_delta(self):
        self.ea.push('drive')
        head = self.commit(self.a, 'second')
        before = self.snapshot('before')
        output = self.root / 'delta'
        value = agent.prepare(self.a, before, self.identity, output)
        full = agent.prepare(self.a, before, self.identity, self.root / 'full', full=True)
        base = output / 'base.bundle'
        base_pack = base.read_bytes().split(b'\n\n', 1)[1]
        # A full pack may contain valid but unreachable objects beyond its advertised HEAD.
        full_bytes = Path(full['files']['bundle']['local_path']).read_bytes()
        base.write_bytes(full_bytes.replace((head + ' refs/heads/main').encode(),
                                           (value['base_head'] + ' refs/heads/main').encode(), 1))
        value['local_base'].update(sha256=digest(base.read_bytes()), bytes=base.stat().st_size)
        # Advertise the new HEAD, but omit all its new objects from the delta pack.
        delta_header = Path(value['files']['bundle']['local_path']).read_bytes().split(b'\n\n', 1)[0]
        incomplete = self.root / 'incomplete.bundle'
        incomplete.write_bytes(delta_header + b'\n\n' + base_pack)
        data = decode((output / 'publication.json').read_bytes())
        data.update(bundle_sha256=digest(incomplete.read_bytes()), bundle_bytes=incomplete.stat().st_size)
        with self.assertRaises(GdiError):
            with agent.verify_planned_bundle(output, value, data, incomplete):
                self.fail('an incomplete delta borrowed unpublished objects from its base')

    def test_cli_is_json_and_can_run_from_source_against_another_repo(self):
        stream = io.StringIO()
        with patch.dict('os.environ', {'GDI_TRANSPORT': 'invalid-but-unused'}), contextlib.redirect_stdout(stream):
            code = main(['agent', 'prepare', '--repo', str(self.a.path), '--snapshot', str(self.snapshot()),
                         '--repository-id', self.identity, '--output', str(self.root / 'plan')])
        self.assertEqual(code, 0)
        self.assertEqual(decode(stream.getvalue())['next_step'], 'upload_bundle')

    def test_download_corruption_and_missing_manifest_do_not_advance_plan(self):
        value = agent.prepare(self.a, self.snapshot('empty'), self.identity, self.root / 'plan')
        self.transfer(value, 'bundle')
        snap = self.snapshot('bundle', bundles=True)
        doc = decode(snap.read_bytes())
        bundle = next(item for item in doc['files'] if item['local_path'].endswith('.bundle'))
        file = snap.parent / bundle['local_path']
        raw = file.read_bytes()
        file.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaisesRegex(GdiError, 'checksum'):
            agent.check(self.root / 'plan', snap, self.identity)
        self.assertEqual(decode((self.root / 'plan/plan.json').read_bytes())['state'], 'prepared')
        file.write_bytes(raw)
        agent.check(self.root / 'plan', snap, self.identity)
        with self.assertRaisesRegex(GdiError, 'missing'):
            agent.accept(self.root / 'plan', snap, self.identity)

    def test_changed_identity_concurrent_publication_and_mismatched_ref(self):
        self.ea.push('drive')
        self.commit(self.a, 'second')
        value = agent.prepare(self.a, self.snapshot('old'), self.identity, self.root / 'plan')
        self.transfer(value, 'bundle')
        self.ea.push('drive')
        with self.assertRaisesRegex(GdiError, 'remote changed'):
            agent.check(self.root / 'plan', self.snapshot('new', bundles=True), self.identity)
        self.transfer(value, 'manifest')
        with self.assertRaisesRegex(GdiError, 'conflicting'):
            agent.accept(self.root / 'plan', self.snapshot('conflict', bundles=True), self.identity)
        self.store.data['repository.json'] = encode({'version': 3, 'repository_id': 'f' * 32, 'object_format': 'sha1'})
        with self.assertRaisesRegex(GdiError, 'repository ID mismatch'):
            Snapshot(self.snapshot('replaced'), self.identity)

    def test_pages_duplicates_missing_download_and_path_escape(self):
        self.ea.push('drive')
        snap = self.snapshot()
        original = decode(snap.read_bytes())
        cases = []
        incomplete = copy.deepcopy(original)
        incomplete['listings'][0]['pages'][-1]['next_page_token'] = 'missing-page'
        cases.append((incomplete, 'incomplete'))
        duplicate = copy.deepcopy(original)
        row = copy.deepcopy(duplicate['listings'][0]['pages'][0]['entries'][0])
        row['id'] += '-duplicate'
        duplicate['listings'][0]['pages'][0]['entries'].append(row)
        cases.append((duplicate, 'duplicate'))
        missing = copy.deepcopy(original)
        missing['files'].pop()
        cases.append((missing, 'download is missing'))
        escaping = copy.deepcopy(original)
        escaping['files'][0]['local_path'] = '../secret'
        cases.append((escaping, 'local path'))
        for value, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(GdiError, error):
                snap.write_bytes(encode(value))
                Snapshot(snap, self.identity)
        snap.write_bytes(encode(original))
        self.assertEqual(len(Snapshot(snap, self.identity).chain), 1)

    def test_metadata_hash_tamper_symlink_download_and_saved_artifact_tamper(self):
        self.ea.push('drive')
        snap = self.snapshot()
        doc = decode(snap.read_bytes())
        manifest = next(item for item in doc['files'] if '/branches/' in item['local_path'])
        path = snap.parent / manifest['local_path']
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"nonce":"', b'"nonce":"', 1)[:-1] + b' ')
        with self.assertRaisesRegex(GdiError, 'checksum'):
            Snapshot(snap, self.identity)
        path.unlink(); path.symlink_to(self.root / 'anything')
        with self.assertRaisesRegex(GdiError, 'symlinks'):
            Snapshot(snap, self.identity)
        path.unlink(); path.write_bytes(raw)
        self.commit(self.a, 'second')
        value = agent.prepare(self.a, snap, self.identity, self.root / 'plan')
        Path(value['files']['manifest']['local_path']).write_bytes(b'{}\n')
        with self.assertRaisesRegex(GdiError, 'checksum'):
            agent.check(self.root / 'plan', snap, self.identity)

    def test_incomplete_preparation_is_not_replaced_and_failed_fsync_grants_no_permission(self):
        snap = self.snapshot()
        output = self.root / 'partial'
        output.mkdir(); (output / 'source.bundle').write_bytes(b'keep')
        with self.assertRaisesRegex(GdiError, 'already exists'):
            agent.prepare(self.a, snap, self.identity, output)
        self.assertEqual((output / 'source.bundle').read_bytes(), b'keep')
        with patch('gdi.agent.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                agent.prepare(self.a, snap, self.identity, self.root / 'failed')
        self.assertFalse((self.root / 'failed').exists())
        self.assertEqual(agent.prepare(self.a, snap, self.identity, self.root / 'failed')['state'], 'prepared')

    def test_already_published_requires_no_new_upload_and_changed_head_requires_new_plan(self):
        self.ea.push('drive')
        self.assertEqual(agent.prepare(self.a, self.snapshot(), self.identity, self.root / 'noop')['state'], 'already_published')
        self.commit(self.a, 'second')
        agent.prepare(self.a, self.snapshot('base'), self.identity, self.root / 'plan')
        self.commit(self.a, 'third')
        with self.assertRaisesRegex(GdiError, 'HEAD changed'):
            agent.prepare(self.a, self.snapshot('same'), self.identity, self.root / 'plan')

    def test_unicode_percent_branch_and_other_branch_listing_are_supported(self):
        self.ea.push('drive')
        branch = 'feature/ветка%25'
        self.a.call('checkout', '-q', '-b', branch)
        self.commit(self.a, 'unicode branch')
        from gdi.branches import branch_directory
        snap = export_snapshot(self.root / 'unicode', self.store, ref='refs/heads/' + branch,
                               include=['', 'branches', 'branches/' + branch_directory('refs/heads/' + branch)])
        value = agent.prepare(self.a, snap, self.identity, self.root / 'unicode-plan')
        self.assertIn('feature%2Fветка%2525', value['files']['manifest']['target_folder'])
        self.transfer(value, 'bundle', 'manifest')
        accepted_snapshot = export_snapshot(self.root / 'unicode-after', self.store, ref='refs/heads/' + branch,
                                           include=['', 'branches', 'branches/' + branch_directory('refs/heads/' + branch), 'bundles'])
        self.assertEqual(agent.accept(self.root / 'unicode-plan', accepted_snapshot, self.identity)['state'], 'accepted')
        self.assertEqual(self.eb.fetch('drive', branch)[0], self.a.oid('HEAD'))

    def test_lost_local_plan_rename_ack_reuses_same_nonce_and_bytes(self):
        snap = self.snapshot()
        from gdi import agent as module
        original = module.os.fsync
        hit = []
        def fail(fd):
            # The final directory has already been renamed; simulate only that ack.
            if (self.root / 'plan/plan.json').exists() and not hit:
                hit.append(fd)
                raise OSError('directory fsync ack lost')
            return original(fd)
        with patch('gdi.agent.os.fsync', side_effect=fail), self.assertRaisesRegex(OSError, 'ack lost'):
            agent.prepare(self.a, snap, self.identity, self.root / 'plan')
        before = {path.name: path.read_bytes() for path in (self.root / 'plan').iterdir()}
        self.assertEqual(agent.prepare(self.a, snap, self.identity, self.root / 'plan')['state'], 'prepared')
        self.assertEqual(before, {path.name: path.read_bytes() for path in (self.root / 'plan').iterdir()})

    def test_snapshot_reports_the_shared_restore_order_and_source_launch_needs_no_packages(self):
        self.ea.push('drive')
        self.commit(self.a, 'second'); self.ea.push('drive')
        snapshot = Snapshot(self.snapshot(), self.identity)
        self.assertEqual([item['bundle_kind'] for item in snapshot.required_bundles()], ['full', 'incremental'])
        self.assertEqual(snapshot.required_bundles()[-1]['publication_id'], snapshot.chain[-1][0])
        self.commit(self.a, 'third')
        path = self.snapshot('before-third')
        tools = self.root / 'only-git'; tools.mkdir()
        import os, shutil, subprocess, sys
        (tools / 'git').symlink_to(shutil.which('git'))
        self.assertIsNone(shutil.which('rclone', path=str(tools)))
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-S', '-m', 'gdi', 'agent', 'prepare', '--repo', str(self.a.path),
                                 '--snapshot', str(path), '--repository-id', self.identity,
                                 '--output', str(self.root / 'no-packages')], cwd=project,
                                env={**os.environ, 'PATH': str(tools), 'GDI_TRANSPORT': 'invalid-unused'},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(decode(result.stdout)['next_step'], 'upload_bundle')
