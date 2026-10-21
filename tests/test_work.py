"""Real Git work/checkpoint/import cases, using the existing in-memory exchange fixture."""

from pathlib import Path
from unittest.mock import patch

from gdi import agent, agent_work, work, work_import
from gdi.branches import branch_directory
from gdi.cli import parser
from gdi.exchange import Exchange
from gdi.git import GdiError, Git
from gdi.publication import decode
from tests.snapshots import export_snapshot
from tests.test_exchange import ExchangeTestCase


class WorkTests(ExchangeTestCase):
    def snapshot(self, name, ref='refs/heads/main', bundles=False):
        folder = 'branches/' + branch_directory(ref)
        return export_snapshot(self.root / name, self.store, ref=ref,
                               include=['', 'branches', folder] + (['bundles'] if bundles else []))

    def start(self, ident='7'):
        self.ea.push('drive')
        return agent_work.start(self.a, self.snapshot('start-' + ident), self.identity, ident)

    def part(self, number=1, final=False, title='feat: voice files', body='Result details.\nChecks: NOT RUN.'):
        path = self.a.path / 'voice.txt'
        path.write_text(str(number) + '\n')
        self.a.call('add', 'voice.txt')
        return agent_work.checkpoint(self.a, self.identity, title, body=body, final=final)

    def ready(self, ident='7'):
        self.start(ident)
        self.part(final=True)
        self.ea.push('drive')
        available = work_import.candidates(self.eb, 'drive')
        self.assertEqual(len(available['choices']), 1)
        return work_import.prepare(self.eb, 'drive', available['choices'][0], available['target_head'])

    def test_start_and_checkpoint_do_not_change_assigned_target(self):
        started = self.start()
        self.assertEqual(started['target'], 'refs/heads/main')
        self.assertEqual(self.a.branch(), 'gdi-work/7')
        for number in range(1, 12):
            value = self.part(number, final=number == 11)
            self.assertEqual(value['parts'], number)
        self.assertEqual(self.a.oid('refs/heads/main'), self.first)
        self.assertEqual(value['state'], 'ready')
        self.assertTrue(work.message(self.a, value['final']).startswith('[7.11!] feat: voice files\n'))
        with self.assertRaisesRegex(GdiError, 'final or imported'):
            self.part(12)

    def test_checkpoint_on_normal_branch_is_refused(self):
        before = self.a.snapshot()
        with self.assertRaisesRegex(GdiError, 'separate work branch'):
            work.checkpoint(self.a, 'wrong branch')
        self.assertEqual(before, self.a.snapshot())

    def test_work_binding_uses_actual_target_name(self):
        self.a.call('switch', '-c', 'feature/другая')
        self.ea.push('drive')
        started = agent_work.start(self.a, self.snapshot('named-start', 'refs/heads/feature/другая'), self.identity, '19')
        self.assertEqual(started['target'], 'refs/heads/feature/другая')
        self.part(final=True)
        self.ea.push('drive')
        available = work_import.candidates(self.eb, 'drive')
        self.assertEqual(available['choices'], [])
        self.assertIn('assigned to refs/heads/feature/другая', available['rejected'][0]['reason'])

    def test_start_requires_fresh_exact_target_and_unused_work_id(self):
        self.ea.push('drive')
        old = self.snapshot('old')
        self.commit(self.a, 'advanced target')
        with self.assertRaisesRegex(GdiError, 'exact assigned'):
            agent_work.start(self.a, old, self.identity, '1')
        self.ea.push('drive')
        agent_work.start(self.a, self.snapshot('fresh'), self.identity, '1')
        self.part()
        self.ea.push('drive')
        self.a.call('switch', 'main')
        with self.assertRaisesRegex(GdiError, 'already exists on Drive'):
            agent_work.start(self.a, self.snapshot('used'), self.identity, '1')
        for value in ('0', '-1', '01', 'a/b'):
            with self.subTest(value=value), self.assertRaises(GdiError):
                work.work_id(value)

    def test_unstaged_untracked_and_wrong_identity_are_refused(self):
        self.start()
        (self.a.path / 'untracked').write_text('do not silently omit')
        before = self.a.oid('HEAD')
        with self.assertRaisesRegex(GdiError, 'untracked'):
            work.checkpoint(self.a, 'partial')
        self.a.call('add', 'untracked')
        (self.a.path / 'file.txt').write_text('unstaged')
        with self.assertRaisesRegex(GdiError, 'unstaged'):
            work.checkpoint(self.a, 'partial')
        with self.assertRaisesRegex(GdiError, 'repository ID mismatch'):
            agent_work.status(self.a, 'f' * 32)
        self.assertEqual(before, self.a.oid('HEAD'))

    def test_missing_number_and_metadata_change_are_refused_before_upload(self):
        value = self.start()
        self.part()
        self.a.call('commit', '--allow-empty', '-qm', '[7.3] gap\n\n' + work.trailers(value))
        with self.assertRaisesRegex(GdiError, 'expected part 2, found 3'):
            self.ea.push('drive')
        self.assertFalse(any(path.startswith('branches/gdi-work') for path in self.store.data))

    def test_final_followed_by_foreign_commit_is_not_publishable(self):
        self.start()
        self.part(final=True)
        self.commit(self.a, 'unexpected post-final change')
        with self.assertRaisesRegex(GdiError, 'metadata|foreign commit'):
            self.ea.push('drive')

    def test_connector_publication_and_materialization_recovery_use_existing_protocol(self):
        self.start()
        first = self.part()
        ref = first['ref']
        output = self.root / 'publication-plan'
        prepared = agent.prepare(self.a, self.snapshot('before', ref), self.identity, output)
        for key in ('bundle', 'manifest'):
            spec = prepared['files'][key]
            relative = spec['target_folder'].removeprefix('project/') + '/' + spec['target_name']
            self.store.mkdir(relative.rpartition('/')[0])
            self.store.upload(spec['local_path'], relative)
            snapshot = self.snapshot('after-' + key, ref, bundles=True)
            if key == 'bundle':
                self.assertTrue(agent.check(output, snapshot, self.identity)['safe_to_upload_manifest'])
            else:
                self.assertEqual(agent.accept(output, snapshot, self.identity)['state'], 'accepted')
        import shutil
        shutil.rmtree(self.a.path)
        restored = agent.clone(self.snapshot('restore', ref, bundles=True), self.identity, self.root / 'restored')
        git = Git.discover(restored['destination'], isolated=True)
        git.call('config', 'user.name', 'Gdi Test')
        git.call('config', 'user.email', 'gdi@example.invalid')
        self.assertEqual(agent_work.status(git, self.identity)['next_part'], 2)
        git.call('add', 'voice.txt')
        final = agent_work.checkpoint(git, self.identity, 'feat: final result', final=True)
        self.assertEqual(final['parts'], 2)
        self.assertEqual(final['state'], 'ready')

    def test_squash_preserves_operator_commit_and_exact_final_message(self):
        self.start()
        self.part()
        final = self.part(2, final=True)
        self.ea.push('drive')
        operator = self.commit(self.b, 'operator addition')
        available = work_import.candidates(self.eb, 'drive')
        plan = work_import.prepare(self.eb, 'drive', available['choices'][0], available['target_head'])
        result = work_import.apply(self.eb, plan['plan_path'])
        squash = result['squash']
        self.assertEqual(work.parents(self.b, squash), [operator])
        self.assertEqual(work.message(self.b, squash), final['message'])
        self.assertEqual((self.b.path / 'file.txt').read_text(), 'operator addition\n')
        self.assertEqual((self.b.path / 'voice.txt').read_text(), '2\n')
        self.assertEqual(self.b.snapshot()[2], '')
        marker = work.validate(self.b, final['ref'], result['marker'], self.identity)
        self.assertEqual(marker['state'], 'imported')
        self.assertEqual(marker['receipt']['squash'], squash)
        self.assertEqual(work.parents(self.b, result['marker']), [final['final']])
        before = self.b.snapshot()
        repeated = work_import.apply(self.eb, plan['plan_path'])
        self.assertEqual(repeated['squash'], squash)
        self.assertEqual(before, self.b.snapshot())
        _, completed = work_import.push(self.eb, 'drive')
        self.assertEqual(self.eb.last_publication['ref'], 'refs/heads/main')
        self.assertEqual(self.eb.last_publication['head'], squash)
        self.assertEqual(len(completed), 1)
        self.assertEqual(work_import.candidates(self.eb, 'drive')['choices'], [])

    def test_target_branch_or_head_change_after_plan_is_refused(self):
        plan = self.ready()
        self.b.call('switch', '-c', 'unrelated')
        with self.assertRaisesRegex(GdiError, 'not the assigned'):
            work_import.apply(self.eb, plan['plan_path'])
        self.b.call('switch', 'main')
        self.commit(self.b, 'new operator commit')
        before = self.b.snapshot()
        with self.assertRaisesRegex(GdiError, 'target changed'):
            work_import.apply(self.eb, plan['plan_path'])
        self.assertEqual(before, self.b.snapshot())

    def test_source_change_after_plan_is_refused(self):
        plan = self.ready()
        value = work.validate(self.a, work.PREFIX + '7', self.a.oid('HEAD'), self.identity)
        marker = work.import_marker(self.a, value, self.first, self.first)
        self.a.call('update-ref', value['ref'], marker, value['head'])
        self.ea.push('drive')
        before = self.b.snapshot()
        with self.assertRaisesRegex(GdiError, 'source changed'):
            work_import.apply(self.eb, plan['plan_path'])
        self.assertEqual(before, self.b.snapshot())

    def test_conflict_is_isolated_from_operator_checkout(self):
        self.start()
        (self.a.path / 'file.txt').write_text('agent conflict\n')
        self.a.call('add', 'file.txt')
        work.checkpoint(self.a, 'feat: conflicts', final=True)
        self.ea.push('drive')
        self.commit(self.b, 'operator conflict')
        available = work_import.candidates(self.eb, 'drive')
        before = self.b.snapshot()
        with self.assertRaisesRegex(GdiError, 'conflicts; target was not changed'):
            work_import.prepare(self.eb, 'drive', available['choices'][0], available['target_head'])
        self.assertEqual(before, self.b.snapshot())
        self.assertIsNone(self.b.oid('MERGE_HEAD'))

    def test_crash_before_ref_update_rolls_back_and_retries_same_squash(self):
        plan = self.ready()
        original = self.b.call
        def fail(*args, **kwargs):
            if args[:2] == ('merge', '--ff-only'):
                raise GdiError('injected before ref update')
            return original(*args, **kwargs)
        before = self.b.snapshot()
        with patch.object(self.b, 'call', side_effect=fail):
            with self.assertRaisesRegex(GdiError, 'injected'):
                work_import.apply(self.eb, plan['plan_path'])
        self.assertEqual(before, self.b.snapshot())
        saved = decode(work_import.journal_path(self.b, plan).read_bytes())
        result = work_import.apply(self.eb, plan['plan_path'])
        self.assertEqual(result['squash'], saved['squash'])

    def test_crash_after_ref_update_does_not_create_second_commit(self):
        plan = self.ready()
        original = self.b.call
        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ('merge', '--ff-only'):
                raise KeyboardInterrupt()
            return result
        with patch.object(self.b, 'call', side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):
                work_import.apply(self.eb, plan['plan_path'])
        head = self.b.oid('HEAD')
        self.assertEqual(work_import.apply(self.eb, plan['plan_path'])['squash'], head)

    def test_marker_upload_failure_can_resume_after_operator_checkout_loss(self):
        plan = self.ready()
        imported = work_import.apply(self.eb, plan['plan_path'])
        self.store.fail = 'branches/gdi-work%2F7'
        with self.assertRaisesRegex(GdiError, 'network unavailable'):
            work_import.push(self.eb, 'drive')
        self.store.fail = None
        c = self.repo('recovered-operator', clone=self.b)
        exchange = Exchange(c, lambda url: self.store)
        exchange.add('drive', 'memory:project', expected_id=self.identity)
        available = work_import.candidates(exchange, 'drive')
        recovered = work_import.prepare(exchange, 'drive', available['choices'][0], available['target_head'])
        self.assertEqual(recovered['existing_squash'], imported['squash'])
        result = work_import.apply(exchange, recovered['plan_path'])
        self.assertEqual(result['squash'], imported['squash'])
        self.assertEqual(c.oid('HEAD'), imported['squash'])
        work_import.push(exchange, 'drive')
        self.assertEqual(work_import.candidates(exchange, 'drive')['choices'], [])

    def test_multiple_ready_works_are_separate_choices(self):
        self.start('1')
        self.part(final=True)
        self.ea.push('drive')
        self.a.call('switch', 'main')
        agent_work.start(self.a, self.snapshot('second-start'), self.identity, '2')
        self.part(2, final=True)
        self.ea.push('drive')
        available = work_import.candidates(self.eb, 'drive')
        self.assertEqual([value['work'] for value in available['choices']], ['1', '2'])

    def test_unapplied_plan_can_be_replaced_after_an_operator_commit(self):
        plan = self.ready()
        original = work_import.install
        def interleave(git, value, squash):
            self.commit(self.b, 'operator interleaved before apply')
            return original(git, value, squash)
        with patch('gdi.work_import.install', side_effect=interleave):
            with self.assertRaisesRegex(GdiError, 'target branch or HEAD changed'):
                work_import.apply(self.eb, plan['plan_path'])
        available = work_import.candidates(self.eb, 'drive')
        replacement = work_import.prepare(self.eb, 'drive', available['choices'][0], available['target_head'])
        before = self.b.oid('HEAD')
        result = work_import.apply(self.eb, replacement['plan_path'])
        self.assertEqual(work.parents(self.b, result['squash']), [before])
        self.assertEqual((self.b.path / 'file.txt').read_text(), 'operator interleaved before apply\n')
        self.assertTrue(list((work_import.journal_path(self.b, plan).parent / 'superseded').glob('*.json')))

    def test_agent_commands_have_a_dedicated_offline_interface(self):
        args = parser().parse_args(['agent', 'work', 'start', '19', '--repository-id', self.identity,
                                   '--snapshot', '/snapshot', '--repo', '/checkout'])
        self.assertEqual((args.command, args.operation, args.work_operation), ('agent', 'work', 'start'))
        self.assertTrue(args.json)
        args = parser().parse_args(['agent', 'work', 'checkpoint', '--repository-id', self.identity,
                                   '-m', 'final subject', '--final'])
        self.assertTrue(args.final)
