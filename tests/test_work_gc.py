"""Imported work retirement, real Git restoration, partial deletion and stale writers."""

import copy
from pathlib import Path
from unittest.mock import patch

from gdi import agent, agent_work, work, work_import, work_lifecycle
from gdi.branches import branch_directory
from gdi.exchange import Exchange
from gdi.git import GdiError
from gdi.publication import decode, encode, validate_repository
from tests.snapshots import export_snapshot
from tests.test_exchange import ExchangeTestCase
from tests import test_work as work_tests


class WorkGcTests(ExchangeTestCase):
    start = work_tests.WorkTests.start
    part = work_tests.WorkTests.part

    def setUp(self):
        super().setUp()
        work_lifecycle.enable(self.ea, 'drive', quiescent=True, all_clients_updated=True)

    def snapshot(self, name, ref='refs/heads/main', bundles=False):
        return export_snapshot(self.root / name, self.store, ref=ref,
                               include=['', 'branches', 'branches/' + branch_directory(ref),
                                        'work', 'work/imports', 'work/retired'] + (['bundles'] if bundles else []))

    def imported(self, ident):
        if self.a.branch() != 'main':
            self.a.call('switch', 'main')
        if any(item['Path'] == 'main' for item in self.store.list('branches')):
            self.ea.pull('drive')
        self.start(ident)
        final = self.part(int(ident), final=True)
        self.ea.push('drive')
        available = work_import.candidates(self.eb, 'drive')
        selected = next(item for item in available['choices'] if item['work'] == ident)
        plan = work_import.prepare(self.eb, 'drive', selected, available['target_head'])
        result = work_import.apply(self.eb, plan['plan_path'])
        work_import.push(self.eb, 'drive')
        return final, result

    def collect(self, keep=1, apply=False):
        return self.eb.gc('drive', keep_imported=keep, apply=apply, quiescent=apply, report=lambda line: None)

    def test_activation_preserves_identity_history_and_requires_explicit_pause_upgrade(self):
        before = decode(self.store.data['repository.json'])
        self.assertEqual((before['repository_id'], before['work_version']), (self.identity, 1))
        with self.assertRaisesRegex(GdiError, 'all-clients-updated'):
            work_lifecycle.enable(self.eb, 'drive', quiescent=True)
        self.assertEqual(work_lifecycle.enable(self.eb, 'drive', quiescent=True,
                                             all_clients_updated=True)['state'], 'already_enabled')
        for flag in (True, 2, '1'):
            with self.subTest(flag=flag), self.assertRaises(GdiError):
                validate_repository({**before, 'work_version': flag})

    def test_oldest_import_is_removed_with_its_drive_folder_and_only_unneeded_bundles(self):
        first, imported = self.imported('19')
        self.imported('3')
        # Порядок подтверждения, а не числовой Work-ID, определяет старейшую работу.
        before = copy.deepcopy(self.store.data)
        plan = self.collect()
        self.assertEqual(plan['retire'], ['19'])
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.store.deletions, [])
        self.collect(apply=True)
        directory = 'branches/' + branch_directory(first['ref'])
        self.assertNotIn(directory, self.store.directories)
        self.assertFalse(any(path.startswith(directory + '/') for path in self.store.data))
        self.assertIn('work/retired/19.json', self.store.data)
        retired = decode(self.store.data['work/retired/19.json'])
        self.assertFalse(retired['archive'])
        self.assertEqual(retired['squash'], imported['squash'])
        self.assertIn('branches/gdi-work%2F3', self.store.directories)
        self.assertEqual(self.collect(apply=True)['retire'], [])
        restored = self.repo('restored')
        exchange = Exchange(restored, lambda url: self.store)
        exchange.add('drive', 'memory:project', expected_id=self.identity)
        exchange.fetch('drive', 'main')
        self.assertTrue(restored.has_commit(imported['squash']))

    def test_zero_and_large_retention_boundaries(self):
        self.imported('1')
        self.assertEqual(self.collect(keep=10)['retire'], [])
        self.assertEqual(self.collect(keep=1)['retire'], [])
        self.assertEqual(self.collect(keep=0)['retire'], ['1'])
        self.collect(keep=0, apply=True)
        self.assertNotIn('branches/gdi-work%2F1', self.store.directories)

    def test_stale_online_and_connector_clients_cannot_resurrect_or_reuse_id(self):
        final, _ = self.imported('1')
        self.collect(keep=0, apply=True)
        with self.assertRaisesRegex(GdiError, 'retired'):
            self.ea.push('drive', 'gdi-work/1')
        snap = self.snapshot('retired', final['ref'], bundles=True)
        with self.assertRaisesRegex(GdiError, 'retired'):
            agent.prepare(self.a, snap, self.identity, self.root / 'stale-plan', 'gdi-work/1')
        self.a.call('switch', 'main')
        self.ea.pull('drive')
        with self.assertRaisesRegex(GdiError, 'retired'):
            agent_work.start(self.a, self.snapshot('reused'), self.identity, '1')
        self.assertNotIn('branches/gdi-work%2F1', self.store.directories)

    def test_partial_manifest_deletion_is_resumable(self):
        final, _ = self.imported('1')
        directory = 'branches/' + branch_directory(final['ref'])
        original = self.store.delete_work_manifest
        calls = []
        def fail(path):
            calls.append(path)
            if len(calls) == 2:
                raise GdiError('injected partial deletion')
            original(path)
        with patch.object(self.store, 'delete_work_manifest', side_effect=fail):
            with self.assertRaisesRegex(GdiError, 'injected'):
                self.collect(keep=0, apply=True)
        self.assertIn('work/retired/1.json', self.store.data)
        self.collect(keep=0, apply=True)
        self.assertNotIn(directory, self.store.directories)

    def test_empty_folder_deletion_failure_resumes_without_recursive_purge(self):
        self.imported('1')
        self.store.fail = 'rmdir'
        with self.assertRaisesRegex(GdiError, 'rmdir'):
            self.collect(keep=0, apply=True)
        self.assertIn('branches/gdi-work%2F1', self.store.directories)
        self.store.fail = None
        self.collect(keep=0, apply=True)
        self.assertNotIn('branches/gdi-work%2F1', self.store.directories)

    def test_pending_unpublished_and_ready_works_are_never_retired(self):
        self.start('1')
        self.part(final=True)
        self.ea.push('drive')
        available = work_import.candidates(self.eb, 'drive')
        plan = work_import.prepare(self.eb, 'drive', available['choices'][0], available['target_head'])
        self.assertEqual(self.collect(keep=0)['retire'], [])
        work_import.apply(self.eb, plan['plan_path'])
        self.assertEqual(self.collect(keep=0)['retire'], [])
        work_import.push(self.eb, 'drive')
        self.assertEqual(self.collect(keep=0)['retire'], ['1'])

    def test_target_that_lost_squash_is_protected(self):
        self.imported('1')
        state = copy.deepcopy(self.store.data)
        original = self.b.ancestor
        def missing(old, new):
            if old != self.first:
                return False
            return original(old, new)
        with patch.object(self.b, 'ancestor', side_effect=missing):
            plan = self.collect(keep=0)
        self.assertEqual(plan['retire'], [])
        self.assertTrue(any('no longer contains squash' in item['reason'] for item in plan['blocked']))
        self.assertEqual(self.store.data, state)

    def test_apply_requires_quiescence_before_any_remote_access(self):
        with patch.object(self.eb, 'connect', side_effect=AssertionError('no remote access')):
            with self.assertRaisesRegex(GdiError, 'requires --quiescent'):
                self.eb.gc('drive', keep_imported=0, apply=True)

    def test_active_ci_blocks_retirement_even_during_declared_pause(self):
        self.imported('1')
        self.store.mkdir('ci/queue')
        self.store.data['ci/queue/' + 'a' * 32 + '.json'] = b'{}'
        with self.assertRaisesRegex(GdiError, 'CI queue'):
            self.collect(keep=0, apply=True)
        self.assertEqual(self.store.deletions, [])
        self.assertNotIn('work/retired/1.json', self.store.data)

    def test_import_metadata_change_during_verification_aborts_before_retirement(self):
        self.imported('1')
        from gdi import gc
        original = gc.verify_retained
        def mutate(*args, **kwargs):
            original(*args, **kwargs)
            receipt = decode(self.store.data['work/imports/1.json'])
            receipt['order'] = 99
            self.store.data['work/imports/1.json'] = encode(receipt)
        with patch('gdi.gc.verify_retained', side_effect=mutate):
            with self.assertRaisesRegex(GdiError, 'remote/CI changed'):
                self.collect(keep=0, apply=True)
        self.assertEqual(self.store.deletions, [])
        self.assertNotIn('work/retired/1.json', self.store.data)

    def test_native_transports_refuse_ordinary_or_recursive_deletion(self):
        from gdi.transport import validate_work_directory, validate_work_manifest_path
        for path in ('', 'branches', 'branches/main', 'ci/jobs/1', 'branches/gdi-work%2F1/extra'):
            with self.subTest(path=path), self.assertRaises(GdiError):
                validate_work_directory(path)
        validate_work_directory('branches/gdi-work%2F1')
        validate_work_manifest_path('branches/gdi-work%2F1/' + 'a' * 64 + '.json')
        with self.assertRaises(GdiError):
            validate_work_manifest_path('branches/main/' + 'a' * 64 + '.json')
