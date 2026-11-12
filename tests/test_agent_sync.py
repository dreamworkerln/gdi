"""One-file connector contract; no external Drive or CI execution."""

from pathlib import Path
import contextlib
import io
from unittest.mock import patch
import zipfile

from gdi.legacy_agent_receiver import receive
from gdi.legacy_agent_sync import BASE_FILE, export_name, pull, push, read_package, receipt_path
from gdi.git import GdiError, Git
from gdi.publication import encode
from tests.test_inbox import InboxTestCase


class AgentExchangeTests(InboxTestCase):
    def export(self, name='export.zip'):
        self.ea.publisher_worker_id = 'user-host'
        _, publication, _ = self.ea.push('drive')
        path = self.root / name
        path.write_bytes(self.store.data['repos/a/agent/' + export_name(self.identity, 'refs/heads/main')])
        return path, publication

    def checkout(self, name='agent'):
        package, publication = self.export()
        destination = self.root / name
        pull(package, self.identity, destination=destination)
        return Git.discover(destination, isolated=True), publication

    def prepare(self):
        git, base = self.checkout()
        head = self.commit(git, 'agent change')
        output = self.root / 'push-plan'
        plan = push(git, self.identity, output)
        return git, output, plan, base, head

    def upload(self, plan):
        item = plan['files']['package']
        self.store.upload(item['local_path'], 'inbox/' + item['target_name'])
        return item['target_name']

    def test_pull_is_offline_and_does_not_need_connector_filename(self):
        package, _ = self.export('export.zip.bin')
        before = list(self.store.downloads), list(self.store.uploads)
        self.store.fail = 'read'
        with patch('gdi.transport.Rclone', side_effect=AssertionError('network used')):
            result = pull(package, self.identity, destination=self.root / 'cloud')
        git = Git.discover(self.root / 'cloud', isolated=True)
        self.assertEqual(result['state'], 'PULLED')
        self.assertEqual(git.oid('HEAD'), self.a.oid('HEAD'))
        self.assertEqual(git.branch(), 'main')
        self.assertEqual(git.snapshot()[2], '')
        self.assertEqual(result['worker_id'], 'user-host')
        self.assertEqual(before, (self.store.downloads, self.store.uploads))

    def test_push_has_one_upload_no_readback_and_stable_retry(self):
        git, output, plan, _, head = self.prepare()
        self.assertEqual(plan['connector_actions'], ['upload_package'])
        self.assertFalse(plan['read_back_required'])
        self.assertEqual(set(plan['files']), {'package'})
        package = Path(plan['files']['package']['local_path'])
        raw = package.read_bytes()
        before = list(self.store.downloads), list(self.store.uploads)
        self.store.fail = 'read'
        self.assertEqual(push(git, self.identity, output), plan)
        self.assertEqual(package.read_bytes(), raw)
        self.assertEqual(before, (self.store.downloads, self.store.uploads))
        with read_package(package, self.identity, 'push') as (value, _):
            self.assertEqual(value['head'], head)
            self.assertEqual(value['worker_id'], 'user-host')
        self.commit(git, 'next change')
        with self.assertRaisesRegex(GdiError, 'HEAD or branch changed'):
            push(git, self.identity, output)

    def test_pull_fast_forwards_but_preserves_local_ahead_and_rejects_divergence(self):
        git, _ = self.checkout()
        head = self.commit(self.a, 'remote update')
        package, _ = self.export('next.zip')
        pull(package, self.identity, repo=git.path)
        self.assertEqual(git.oid('HEAD'), head)
        local = self.commit(git, 'local update')
        pull(package, self.identity, repo=git.path)
        self.assertEqual(git.oid('HEAD'), local)
        self.commit(self.a, 'other remote update')
        package, _ = self.export('diverged.zip')
        with self.assertRaisesRegex(GdiError, 'diverged'):
            pull(package, self.identity, repo=git.path)
        self.assertEqual(git.oid('HEAD'), local)

    def test_dirty_checkout_and_branch_change_are_preserved(self):
        git, _ = self.checkout()
        package = self.root / 'export.zip'
        extra = git.path / 'uncommitted.txt'
        extra.write_text('preserve me')
        with self.assertRaisesRegex(GdiError, 'dirty'):
            pull(package, self.identity, repo=git.path)
        self.assertEqual(extra.read_text(), 'preserve me')
        extra.unlink()
        git.call('checkout', '-qb', 'selected')
        with self.assertRaisesRegex(GdiError, 'does not switch branches'):
            pull(package, self.identity, repo=git.path)
        self.assertEqual(git.branch(), 'selected')

    def test_wrong_identity_or_bad_zip_never_creates_checkout(self):
        package, _ = self.export()
        destination = self.root / 'cloud'
        with self.assertRaises(GdiError):
            pull(package, 'f' * 32, destination=destination)
        self.assertFalse(destination.exists())
        with zipfile.ZipFile(package, 'a') as archive:
            archive.writestr('../outside', b'extra')
        with self.assertRaisesRegex(GdiError, 'exactly'):
            pull(package, self.identity, destination=destination)
        self.assertFalse(destination.exists())

    def test_package_context_preserves_consumer_runtime_error(self):
        package, _ = self.export()
        error = RuntimeError('failure outside ZIP decoding')
        with self.assertRaises(RuntimeError) as caught:
            with read_package(package, self.identity, 'export'):
                raise error
        self.assertIs(caught.exception, error)

    def test_zip_runtime_error_is_reported_as_invalid_package(self):
        package, _ = self.export()
        error = RuntimeError('ZIP reader failure')
        with patch('gdi.legacy_agent_sync.zipfile.ZipFile', side_effect=error):
            with self.assertRaisesRegex(GdiError, 'invalid or incomplete agent package') as caught:
                with read_package(package, self.identity, 'export'):
                    self.fail('invalid ZIP must not yield')
        self.assertIs(caught.exception.__cause__, error)

    def test_worker_publishes_same_commits_receipt_and_export_without_ci(self):
        git, output, plan, _, head = self.prepare()
        worker = self.worker()
        operator = self.a.snapshot()
        name = self.upload(plan)
        result = receive(worker, self.factory('memory:hub'), name)
        self.assertEqual(result['state'], 'PUBLISHED')
        self.assertEqual(result['head'], head)
        self.assertEqual(self.a.snapshot(), operator)
        self.assertFalse(self.counter.exists())
        self.assertEqual(worker.ledger.pending(), [])
        self.assertNotIn('inbox/' + name, self.store.data)
        receipt = self.root / 'receipt.json'
        receipt.write_bytes(self.store.data['repos/a/' + receipt_path(self.identity, plan['request_id'])])
        self.assertEqual(push(git, self.identity, output, receipt=receipt), result)
        self.assertEqual(push(git, self.identity, output, receipt=receipt), result)
        self.assertEqual(push(git, self.identity, self.root / 'empty')['state'], 'ALREADY_PUBLISHED')
        export = self.root / 'published.zip'
        export.write_bytes(self.store.data['repos/a/agent/' + export_name(self.identity, 'refs/heads/main')])
        with read_package(export, self.identity, 'export') as (value, _):
            self.assertEqual(value['head'], head)
            self.assertEqual(value['worker_id'], worker.worker_id)

    def test_stale_base_is_a_conflict_without_overwriting_remote(self):
        git, output, plan, _, _ = self.prepare()
        advanced = self.commit(self.a, 'other author')
        _, latest, _ = self.ea.push('drive')
        worker = self.worker()
        result = receive(worker, self.factory('memory:hub'), self.upload(plan))
        self.assertEqual(result['state'], 'CONFLICT')
        self.assertEqual((result['current_head'], result['current_publication']), (advanced, latest))
        self.assertEqual(self.ea.publications(self.factory('memory:hub/repos/a'), self.identity,
                                            'refs/heads/main')[-1][0], latest)
        before = (git.gdi_dir() / BASE_FILE).read_bytes()
        receipt = self.root / 'conflict.json'
        receipt.write_bytes(encode(result))
        self.assertEqual(push(git, self.identity, output, receipt=receipt)['state'], 'CONFLICT')
        self.assertEqual((git.gdi_dir() / BASE_FILE).read_bytes(), before)

    def test_lost_acknowledgement_retries_without_republishing(self):
        _, _, plan, _, _ = self.prepare()
        worker = self.worker()
        name = self.upload(plan)
        self.store.fail = 'delete-notification'
        with self.assertRaisesRegex(GdiError, 'acknowledgement'):
            receive(worker, self.factory('memory:hub'), name)
        receipt = self.store.data['repos/a/' + receipt_path(self.identity, plan['request_id'])]
        manifests = {key: raw for key, raw in self.store.data.items() if key.startswith('repos/a/branches/')}
        self.store.fail = None
        result = receive(worker, self.factory('memory:hub'), name)
        self.assertEqual(encode(result), receipt)
        self.assertEqual({key: raw for key, raw in self.store.data.items()
                          if key.startswith('repos/a/branches/')}, manifests)
        self.assertNotIn('inbox/' + name, self.store.data)

    def test_recovery_after_publication_before_journal_receipt(self):
        _, _, plan, _, head = self.prepare()
        worker = self.worker()
        name = self.upload(plan)
        self.store.fail = 'repos/a/agent/'
        with self.assertRaises(GdiError):
            receive(worker, self.factory('memory:hub'), name)
        chain = self.ea.publications(self.factory('memory:hub/repos/a'), self.identity, 'refs/heads/main')
        self.assertEqual(chain[-1][1]['head'], head)
        published = chain[-1][0]
        self.store.fail = None
        result = receive(worker, self.factory('memory:hub'), name)
        self.assertEqual(result['publication_id'], published)
        self.assertEqual(self.ea.publications(self.factory('memory:hub/repos/a'), self.identity,
                                            'refs/heads/main')[-1][0], published)

    def test_other_worker_ignores_the_packet_and_gc_is_blocked(self):
        _, _, plan, _, _ = self.prepare()
        config = self.config()
        config['worker_id'] = 'other-host'
        worker = self.worker(config)
        name = self.upload(plan)
        worker.discover()
        self.assertIn('inbox/' + name, self.store.data)
        self.assertFalse(self.counter.exists())
        from gdi.gc import ci_guard
        with self.assertRaisesRegex(GdiError, 'pending agent push'):
            ci_guard(self.ea, self.factory('memory:hub/repos/a'), self.identity, inbox_root='memory:hub')

    def test_changed_package_and_foreign_receipt_are_rejected(self):
        git, output, plan, _, _ = self.prepare()
        package = Path(plan['files']['package']['local_path'])
        original = package.read_bytes()
        package.write_bytes(original + b'changed')
        with self.assertRaisesRegex(GdiError, 'package changed'):
            push(git, self.identity, output)
        package.write_bytes(original)
        receipt = self.root / 'foreign.json'
        receipt.write_bytes(encode({'state': 'PUBLISHED', 'request_id': '0' * 32}))
        with self.assertRaisesRegex(GdiError, 'does not match'):
            push(git, self.identity, output, receipt=receipt)

    def test_full_request_promotes_unchanged_incremental_head(self):
        self.export()
        head = self.commit(self.a, 'incremental')
        git, previous = self.checkout()
        before = self.ea.publications(self.factory('memory:hub/repos/a'), self.identity, 'refs/heads/main')
        self.assertEqual(before[-1][1]['bundle_kind'], 'incremental')
        plan = push(git, self.identity, self.root / 'force-plan', full=True)
        worker = self.worker()
        result = receive(worker, self.factory('memory:hub'), self.upload(plan))
        after = self.ea.publications(self.factory('memory:hub/repos/a'), self.identity, 'refs/heads/main')
        self.assertEqual(result['state'], 'PUBLISHED')
        self.assertEqual(after[-1][1]['head'], head)
        self.assertEqual(after[-1][1]['bundle_kind'], 'full')
        self.assertNotEqual(result['publication_id'], previous)
        self.assertEqual(after[-1][0], result['publication_id'])

    def test_full_request_recovers_checkpoint_after_manifest_committed(self):
        self.export()
        self.commit(self.a, 'delta before full')
        git, _ = self.checkout()
        plan = push(git, self.identity, self.root / 'force-plan', full=True)
        worker = self.worker()
        name = self.upload(plan)
        self.store.fail = 'repos/a/branches/main/checkpoint.json'
        with self.assertRaises(GdiError):
            receive(worker, self.factory('memory:hub'), name)
        self.store.fail = None
        result = receive(worker, self.factory('memory:hub'), name)
        from gdi.publication import decode
        checkpoint = decode(self.store.data['repos/a/branches/main/checkpoint.json'])
        self.assertEqual(result['state'], 'PUBLISHED')
        self.assertEqual(checkpoint['root'], result['publication_id'])

    def test_published_receipt_cannot_regress_a_newly_pulled_base(self):
        git, output, plan, _, _ = self.prepare()
        worker = self.worker()
        result = receive(worker, self.factory('memory:hub'), self.upload(plan))
        receipt = self.root / 'old-receipt.json'
        receipt.write_bytes(encode(result))
        push(git, self.identity, output, receipt=receipt)
        # A later packet advances this same branch and is received via pull.
        self.commit(git, 'later change')
        next_plan = push(git, self.identity, self.root / 'next-plan')
        receive(worker, self.factory('memory:hub'), self.upload(next_plan))
        export = self.root / 'later.zip'
        export.write_bytes(self.store.data['repos/a/agent/' + export_name(self.identity, 'refs/heads/main')])
        pull(export, self.identity, repo=git.path)
        base = (git.gdi_dir() / BASE_FILE).read_bytes()
        with self.assertRaisesRegex(GdiError, 'base or branch changed'):
            push(git, self.identity, output, receipt=receipt)
        self.assertEqual((git.gdi_dir() / BASE_FILE).read_bytes(), base)
