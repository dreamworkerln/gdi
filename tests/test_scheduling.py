"""Timeout migration, liveness and offline connector coordination acceptance tests."""

import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from gdi import agent_ci
from gdi.ci import CiClient
from gdi.ci_protocol import atomic_write
from gdi.exchange import decode, digest, encode
from gdi.git import GdiError
from gdi.scheduling import (Supervisor, choose, discover, inspect_workers, policy, timestamp, worker_snapshot)
from tests.snapshots import export_snapshot, export_workers
from tests import test_agent_ci
from tests.test_inbox import InboxTestCase


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class SchedulingPolicyTests(unittest.TestCase):
    def test_invalid_policies_and_no_implicit_repeatability(self):
        self.assertEqual(policy()['retry_mode'], 'confirmed_stop')
        for value in ({'max_attempts': True}, {'backoff_seconds': float('nan')}, {'queue_timeout_seconds': 0},
                      {'required_labels': [[]]}, {'selection': 'preferred'}, {'retry_mode': 'unsafe'}, {'unknown': 1}):
            with self.subTest(value=value), self.assertRaises(GdiError):
                policy(value)

    def test_load_round_robin_preference_and_pinning(self):
        workers = [dict(worker_id=name, available=True, busy=busy, queue_length=queue)
                   for name, busy, queue in [('a', True, 0), ('b', False, 3), ('c', False, 0), ('d', False, 0)]]
        self.assertEqual(choose(workers, policy())['worker_id'], 'c')
        self.assertEqual(choose(workers, policy(), previous='c')['worker_id'], 'd')
        self.assertEqual(choose(workers, policy({'selection': 'round_robin'}), previous='d')['worker_id'], 'a')
        for mode in ('preferred', 'current'):
            settings = policy({'selection': mode, 'preferred_worker': 'a'})
            self.assertEqual(choose(workers, settings)['worker_id'], 'a')
            self.assertEqual(choose(workers, settings, exclude=('a',))['worker_id'], 'c')
        self.assertIsNone(choose(workers, policy({'pinned_worker': 'offline'})))
        self.assertEqual(choose(workers, policy({'pinned_worker': 'a'}))['worker_id'], 'a')


class SchedulingTests(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.settings = policy({'queue_timeout_seconds': 10, 'heartbeat_timeout_seconds': 20,
                                'worker_fresh_seconds': 30, 'backoff_seconds': 5, 'max_attempts': 2,
                                'retry_mode': 'repeatable'})

    def pair(self):
        old = self.worker()
        config = self.config(); config['worker_id'] = 'other-host'
        other = self.worker(config)
        client, req = self.submit(old)
        self.at = timestamp(req['created_at'])
        return old, other, client, req

    def fresh(self, worker, at, **changes):
        path = 'ci/workers/' + worker.worker_id + '/status.json'
        status = decode(self.store.data[path]);status.update(updated_at=iso(at), **changes)
        self.store.data[path] = encode(status)

    def step(self, client, req, at, settings=None):
        with patch('gdi.scheduling.clock', return_value=at):
            return client.supervise(req['job_id'], settings or self.settings)

    def claim(self, worker, req, at):
        worker.discover()
        row = worker.ledger.get(req['job_id'])
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        claim = {'ci_version': 1, 'job_id': req['job_id'], 'run_id': row['run_id'],
                 'worker_id': req['worker_id'], 'request_sha256': digest(row['raw'])}
        self.store.data[prefix + '/worker.running.json'] = encode(claim)
        status = {**claim, 'state': 'RUNNING', 'updated_at': iso(at), 'stage': 'quiet-build', 'sequence': 1}
        self.store.data[prefix + '/status.json'] = encode(status)
        return row

    def test_off_before_start_migrates_to_b_and_a_returns_without_execution(self):
        old, other, client, req = self.pair()
        self.fresh(other, self.at + 11)
        self.assertEqual(self.step(client, req, self.at + 11)['state'], 'BACKOFF')
        self.assertIn('repos/a/ci/jobs/' + req['job_id'] + '/cancel.json', self.store.data)
        self.assertEqual(self.step(client, req, self.at + 15)['state'], 'BACKOFF')
        moved = self.step(CiClient(self.ea, 'drive'), req, self.at + 17)
        self.assertEqual(moved['state'], 'REASSIGNED')
        self.assertEqual(moved['worker_id'], 'other-host')
        next_req, _ = client.load_request(moved['job_id'])
        for key in ('head', 'publication_id', 'profile_id', 'workflow', 'ref'):
            self.assertEqual(next_req[key], req[key])
        self.assertEqual(next_req['retry_of'], req['job_id'])
        other.tick();old.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(client.result(req['job_id'])['state'], 'CANCELLED')
        done = self.step(client, req, self.at + 18)
        self.assertTrue(done['verified'])
        self.assertEqual(done['state'], 'PASS')
        self.assertEqual(len(done['attempts']), 2)

    def test_a_disappears_after_claim_and_b_takes_over(self):
        old, other, client, req = self.pair()
        self.claim(old, req, self.at + 1)
        self.assertEqual(self.step(client, req, self.at + 2)['state'], 'MONITORING')
        self.fresh(other, self.at + 23)
        self.assertEqual(self.step(client, req, self.at + 23)['state'], 'BACKOFF')
        self.assertEqual(self.step(client, req, self.at + 29)['state'], 'REASSIGNED')
        other.tick();old.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(client.result(req['job_id'])['state'], 'CANCELLED')

    def test_quiet_logs_with_fresh_heartbeat_are_not_a_timeout(self):
        old, other, client, req = self.pair()
        self.claim(old, req, self.at + 1)
        self.step(client, req, self.at + 2)
        path = 'repos/a/ci/jobs/' + req['job_id'] + '/status.json'
        status = decode(self.store.data[path]);status['updated_at'] = iso(self.at + 99)
        self.store.data[path] = encode(status)
        self.assertEqual(self.step(client, req, self.at + 100)['state'], 'MONITORING')
        self.assertNotIn('repos/a/ci/jobs/' + req['job_id'] + '/cancel.json', self.store.data)

    def test_status_reads_request_ready_once_and_rechecks_between_steps(self):
        old, other, client, req = self.pair()
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        with patch.object(self.store, 'read', wraps=self.store.read) as reads:
            self.assertEqual(client.status(req['job_id'])['state'], 'QUEUED')
        paths = [call.args[0] for call in reads.call_args_list]
        self.assertEqual(paths.count(prefix + '/request.json'), 1)
        self.assertEqual(paths.count(prefix + '/request.ready'), 1)
        original = self.store.data[prefix + '/request.ready']
        self.store.data[prefix + '/request.ready'] = original.replace(digest(encode(req)).encode(), b'f' * 64)
        with self.assertRaises(GdiError):client.status(req['job_id'])

    def test_claim_without_status_gets_grace_then_heartbeat_timeout(self):
        old, other, client, req = self.pair()
        self.claim(old, req, self.at + 1)
        del self.store.data['repos/a/ci/jobs/' + req['job_id'] + '/status.json']
        self.assertEqual(self.step(client, req, self.at + 100)['state'], 'MONITORING')
        self.fresh(other, self.at + 121)
        self.assertEqual(self.step(client, req, self.at + 121)['state'], 'BACKOFF')

    def test_a_finishes_without_network_and_late_pass_is_fenced(self):
        old, other, client, req = self.pair()
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        self.store.fail = prefix + '/result.json';old.tick();self.store.fail = None
        self.assertEqual(self.counter.read_text(), 'x')
        self.step(client, req, self.at + 1)
        self.fresh(other, self.at + 25)
        self.assertEqual(self.step(client, req, self.at + 25)['state'], 'BACKOFF')
        self.assertEqual(self.step(client, req, self.at + 31)['state'], 'REASSIGNED')
        other.tick()
        self.store.upload(old.spool(req['job_id']) / 'result.json', prefix + '/result.json')
        self.assertIsNone(client.result(req['job_id']))
        old.tick()
        self.assertEqual(client.result(req['job_id'])['state'], 'CANCELLED')
        self.assertEqual(self.counter.read_text(), 'xx')

    def test_confirmed_stop_and_pinned_resources_never_overlap(self):
        old, other, client, req = self.pair()
        settings = policy({**self.settings, 'retry_mode': 'confirmed_stop', 'pinned_worker': 'user-host'})
        self.assertEqual(self.step(client, req, self.at + 11, settings)['state'], 'WAITING_FOR_STOP')
        self.assertEqual(self.step(client, req, self.at + 50, settings)['state'], 'WAITING_FOR_STOP')
        old.tick();self.fresh(old, self.at + 51)
        result = self.step(client, req, self.at + 51, settings)
        self.assertEqual(result['state'], 'REASSIGNED')
        self.assertEqual(result['worker_id'], 'user-host')
        self.assertFalse(self.counter.exists())

    def test_terminal_result_is_never_cancelled_retroactively(self):
        old, other, client, req = self.pair()
        old.tick()
        result = self.step(client, req, self.at + 1000)
        self.assertTrue(result['verified']);self.assertEqual(result['state'], 'PASS')
        self.assertNotIn('repos/a/ci/jobs/' + req['job_id'] + '/cancel.json', self.store.data)

    def test_network_failure_and_missing_job_do_not_prove_cancellation(self):
        old, other, client, req = self.pair()
        self.store.fail = 'read'
        with self.assertRaises(GdiError):self.step(client, req, self.at + 1000)
        self.store.fail = None
        self.store.data.pop('repos/a/ci/jobs/' + req['job_id'] + '/request.ready')
        with self.assertRaises(GdiError):self.step(client, req, self.at + 1000)
        self.assertNotIn('repos/a/ci/jobs/' + req['job_id'] + '/cancel.json', self.store.data)

    def test_attempt_limit_and_backoff_survive_command_restart(self):
        old, other, client, req = self.pair()
        self.fresh(other, self.at + 11)
        self.step(client, req, self.at + 11)
        moved = self.step(client, req, self.at + 17)
        next_req, _ = client.load_request(moved['job_id'])
        next_at = timestamp(next_req['created_at'])
        expired = self.step(CiClient(self.ea, 'drive'), req, max(self.at + 100, next_at + 11))
        self.assertEqual(expired['state'], 'ATTEMPTS_EXHAUSTED')
        self.assertEqual(len(expired['attempts']), 2)
        self.assertNotIn('repos/a/ci/jobs/' + next_req['job_id'] + '/successor.json', self.store.data)

    def test_backoff_doubles_for_the_second_replacement(self):
        old, other, client, req = self.pair()
        settings = policy({**self.settings, 'max_attempts': 3})
        self.fresh(other, self.at + 11)
        self.step(client, req, self.at + 11, settings)
        moved = self.step(client, req, self.at + 17, settings)
        self.assertEqual(moved['worker_id'], 'other-host')
        self.fresh(old, self.at + 100)
        self.assertEqual(self.step(client, req, self.at + 100, settings)['state'], 'BACKOFF')
        self.assertEqual(self.step(client, req, self.at + 109, settings)['state'], 'BACKOFF')
        next_move = self.step(client, req, self.at + 111, settings)
        self.assertEqual(next_move['state'], 'REASSIGNED')
        self.assertEqual(next_move['worker_id'], 'user-host')
        self.assertEqual(len(next_move['attempts']), 3)

    def test_never_policy_cancels_without_creating_a_replacement(self):
        old, other, client, req = self.pair()
        settings = policy({**self.settings, 'retry_mode': 'never'})
        requested = self.step(client, req, self.at + 11, settings)
        self.assertEqual(requested['state'], 'CANCEL_REQUESTED')
        self.assertTrue(requested['cancellation_requested'])
        self.assertFalse(requested['stop_confirmed'])
        old.tick()
        stopped = self.step(client, req, self.at + 17, settings)
        self.assertTrue(stopped['verified']);self.assertTrue(stopped['stop_confirmed'])
        self.assertEqual(stopped['state'], 'CANCELLED')
        self.assertEqual(stopped['attempts'], [req['job_id']])
        self.assertFalse(self.counter.exists())

    def test_two_live_workers_run_only_the_addressed_job(self):
        old, other, client, req = self.pair()
        self.assertEqual(self.step(client, req, self.at + 1)['state'], 'MONITORING')
        other.tick();self.assertFalse(self.counter.exists())
        old.tick();other.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(self.step(client, req, self.at + 2)['state'], 'PASS')

    def test_invalid_or_regressing_heartbeat_does_not_trigger_cancel(self):
        old, other, client, req = self.pair()
        self.claim(old, req, self.at + 1);self.step(client, req, self.at + 2)
        path = 'repos/a/ci/jobs/' + req['job_id'] + '/status.json'
        original = decode(self.store.data[path])
        for change in ({'updated_at': iso(self.at)}, {'updated_at': iso(self.at + 1000)}, {'worker_id': 'foreign-host'}):
            self.store.data[path] = encode({**original, **change})
            with self.subTest(change=change), self.assertRaises(GdiError):
                self.step(client, req, self.at + 3)
        self.assertNotIn('repos/a/ci/jobs/' + req['job_id'] + '/cancel.json', self.store.data)

    def test_watch_uses_persisted_session_and_returns_verified_result(self):
        old, other, client, req = self.pair()
        calls = []
        def tick(interval):
            calls.append(interval);old.tick()
        with patch('gdi.ci.time.sleep', side_effect=tick), patch('sys.stderr'):
            result, code = client.supervise_wait(req['job_id'], self.settings, watch=True)
        self.assertEqual(code, 0);self.assertTrue(result['verified'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.counter.read_text(), 'x')

    def test_failed_retry_delivery_reuses_exact_successor_bytes(self):
        old, other, client, req = self.pair()
        self.fresh(other, self.at + 11);self.step(client, req, self.at + 11)
        self.store.fail = 'inbox/'
        with self.assertRaises(GdiError):self.step(client, req, self.at + 17)
        successor_path = 'repos/a/ci/jobs/' + req['job_id'] + '/successor.json'
        raw = self.store.data[successor_path]
        self.store.fail = None
        moved = self.step(CiClient(self.ea, 'drive'), req, self.at + 18)
        self.assertEqual(moved['state'], 'REASSIGNED')
        self.assertEqual(self.store.data[successor_path], raw)
        self.assertEqual(moved['job_id'], decode(raw)['request']['job_id'])

    def test_competing_successor_stops_delivery_and_worker_execution(self):
        old, other, client, req = self.pair()
        self.fresh(other, self.at + 11);self.step(client, req, self.at + 11)
        self.store.fail = 'inbox/'
        with self.assertRaises(GdiError):self.step(client, req, self.at + 17)
        self.store.fail = None
        path = 'repos/a/ci/jobs/' + req['job_id'] + '/successor.json'
        successor = decode(self.store.data[path]);successor['policy_sha256'] = 'f' * 64
        self.store.data[path] = encode(successor)
        with self.assertRaisesRegex(GdiError, 'conflicting automatic successor'):
            self.step(client, req, self.at + 18)
        successor['policy_sha256'] = digest(encode(self.settings));self.store.data[path] = encode(successor)
        moved = self.step(client, req, self.at + 19)
        successor['request']['worker_id'] = 'foreign-host';self.store.data[path] = encode(successor)
        other.tick()
        self.assertFalse(self.counter.exists())
        self.assertIsNone(client.result(moved['job_id']))

    def test_worker_registry_freshness_capability_binding_labels_and_load(self):
        old, other, client, req = self.pair()
        old.tick();self.fresh(old, self.at + 1)
        workers = inspect_workers(discover(self.factory('memory:hub')), 'full', self.settings, at=self.at + 100)
        self.assertTrue(all(not row['available'] for row in workers))
        self.fresh(other, self.at + 100)
        caps_path = 'ci/workers/other-host/capabilities.json'
        caps = decode(self.store.data[caps_path]);caps['profiles']['full'] = 'f' * 64
        self.store.data[caps_path] = encode(caps)
        workers = inspect_workers(discover(self.factory('memory:hub')), 'full', self.settings, at=self.at + 100)
        self.assertIn('does not match', next(row for row in workers if row['worker_id'] == 'other-host')['reason'])
        other.advertise();self.fresh(other, self.at + 100)
        settings = policy({**self.settings, 'required_labels': ['hardware']})
        workers = inspect_workers(discover(self.factory('memory:hub')), 'full', settings, at=self.at + 100)
        self.assertTrue(all(not row['available'] for row in workers))
        settings = policy({**self.settings, 'required_platforms': ['windows-latest']})
        workers = inspect_workers(discover(self.factory('memory:hub')), 'full', settings, at=self.at + 100)
        self.assertIn('platforms are missing', next(row for row in workers if row['worker_id'] == 'other-host')['reason'])

    def test_initial_dispatch_delivery_failure_preserves_selected_host(self):
        old, other, client, req = self.pair()
        self.store.fail = 'inbox/'
        with self.assertRaises(GdiError):client.dispatch(req['publication_id'])
        self.store.fail = None
        saved = next((client.root / 'dispatch').glob('*/request.json'))
        prepared = decode(saved.read_bytes())
        self.assertEqual(prepared['worker_id'], 'other-host')
        path = 'ci/workers/other-host/status.json'
        status = decode(self.store.data[path]);status.update(busy=True, state='BUSY', queue_length=50)
        self.store.data[path] = encode(status)
        delivered = CiClient(self.ea, 'drive').dispatch(req['publication_id'])
        self.assertEqual(delivered, prepared)

    def test_automatic_initial_assignment_round_robin_and_cli(self):
        old, other, client, req = self.pair()
        settings = policy({'selection': 'round_robin'})
        first = client.dispatch(req['publication_id'], settings=settings)
        self.assertEqual(client.dispatch(req['publication_id'], settings=settings), first)
        self.commit(self.a, 'another-publication')
        _, publication, _ = self.ea.push('drive')
        second = client.dispatch(publication, settings=settings)
        self.assertNotEqual(first['worker_id'], second['worker_id'])
        from gdi.cli import execute, parser
        args = parser().parse_args(['ci', 'workers', 'drive', '--json'])
        with patch('gdi.cli.Git.discover', return_value=self.a), patch('sys.stdout'):
            self.assertEqual(execute(args, transport_factory=self.factory), 0)

    def test_idle_heartbeat_tracks_queue_and_labels_change_revision(self):
        old, other, client, req = self.pair()
        old.tick()
        old_status = decode(self.store.data['ci/workers/user-host/status.json'])
        self.assertFalse(old_status['busy']);self.assertEqual(old_status['queue_length'], 0)
        old.tick()
        new_status = decode(self.store.data['ci/workers/user-host/status.json'])
        self.assertGreater(timestamp(new_status['updated_at']), timestamp(old_status['updated_at']))
        config = self.config();config['worker_id'] = 'labeled-host';config['labels'] = ['hardware']
        config['execution_profile']['worker_labels'] = config['labels']
        labeled = self.worker(config)
        caps = decode(self.store.data['ci/workers/labeled-host/capabilities.json'])
        self.assertEqual(caps['labels'], ['hardware'])
        self.assertNotEqual(caps['profiles']['full'], req['profile_revision'])


class AgentSchedulingTests(InboxTestCase):
    snapshot = test_agent_ci.AgentCiTests.snapshot
    capabilities = test_agent_ci.AgentCiTests.capabilities
    prepare = test_agent_ci.AgentCiTests.prepare
    transfer = test_agent_ci.AgentCiTests.transfer
    proof = test_agent_ci.AgentCiTests.proof
    def complete_snapshot(self, name):
        transport = self.factory('memory:hub/repos/a')
        include = {'', 'branches', 'branches/main', 'ci', 'ci/jobs'}
        for row in transport.list('ci/jobs', recursive=True):
            if row['IsDir']:
                include.add('ci/jobs/' + row['Path'])
            else:
                parts = row['Path'].split('/')[:-1]
                include.update('ci/jobs/' + '/'.join(parts[:index]) for index in range(1, len(parts) + 1))
        return export_snapshot(self.root / name, transport, repository_path='repos/a', include=include)

    def test_offline_supervisor_cancel_successor_retry_and_verified_result(self):
        old = self.worker();value = self.prepare(old)
        self.transfer(value, 'request', 'ready', 'inbox')
        config = self.config();config['worker_id'] = 'other-host';other = self.worker(config)
        policy_path = self.root / 'policy.json'
        settings = policy({'queue_timeout_seconds': 10, 'heartbeat_timeout_seconds': 20,
                           'worker_fresh_seconds': 30, 'backoff_seconds': 5, 'retry_mode': 'repeatable'})
        policy_path.write_bytes(encode(settings))
        req = decode((self.plan / 'request.json').read_bytes());at = timestamp(req['created_at'])
        status_path = 'ci/workers/other-host/status.json'
        status = decode(self.store.data[status_path]);status['updated_at'] = iso(at + 11)
        self.store.data[status_path] = encode(status)
        session = self.root / 'supervisor'
        def step(number, now, proof=None):
            with patch('gdi.scheduling.clock', return_value=now), \
                    patch('gdi.transport.Rclone.call', side_effect=AssertionError('no rclone')):
                return agent_ci.supervise(self.a, self.complete_snapshot('super-' + str(number)), self.identity,
                                           session, req['job_id'], export_workers(self.root / 'registry', self.store),
                                           policy_path, inbox_proof=proof)
        cancel = step(1, at + 11)
        self.assertEqual(cancel['state'], 'CANCEL_PREPARED')
        self.transfer(cancel['plan'], 'cancel')
        self.assertEqual(step(2, at + 11)['state'], 'BACKOFF')
        successor = step(3, at + 17)
        self.assertEqual(successor['state'], 'SUCCESSOR_PREPARED')
        self.transfer(successor, 'successor')
        retry = step(4, at + 18)
        self.assertEqual(retry['state'], 'RETRY_PREPARED')
        self.assertFalse(retry['plan']['safe_to_upload_inbox'])
        frozen = retry['plan']['job_id']
        self.transfer(retry['plan'], 'request', 'ready')
        ready = step(5, at + 19)
        self.assertTrue(ready['plan']['safe_to_upload_inbox'])
        self.transfer(ready['plan'], 'inbox')
        moved = step(6, at + 20, self.proof(ready['plan']))
        self.assertEqual(moved['state'], 'REASSIGNED')
        self.assertEqual(moved['job_id'], frozen)
        other.tick();old.tick()
        result = step(7, at + 21)
        self.assertTrue(result['verified']);self.assertEqual(result['state'], 'PASS')
        self.assertEqual(self.counter.read_text(), 'x')

    def test_registry_snapshot_rejects_duplicates_missing_pages_and_downloads(self):
        self.worker();path = export_workers(self.root / 'registry', self.store)
        original = decode(path.read_bytes())
        self.assertIn('user-host', worker_snapshot(path))
        for transform in ('duplicate', 'missing_download', 'missing_worker', 'missing_page'):
            value = copy.deepcopy(original)
            if transform == 'duplicate':
                entry = copy.deepcopy(value['pages'][0]['entries'][0]);entry['id'] += '-duplicate'
                value['pages'][0]['entries'].append(entry)
            elif transform == 'missing_download':value['workers'][0]['files'] = []
            elif transform == 'missing_worker':value['workers'] = []
            else:value['workers'][0]['pages'][0]['next_page_token'] = 'missing'
            path.write_bytes(encode(value))
            with self.subTest(transform=transform), self.assertRaises(GdiError):worker_snapshot(path)
        path.write_bytes(encode(original))

    def test_agent_automatic_initial_selection_freezes_plan_worker(self):
        worker = self.worker();_, publication, _ = self.ea.push('drive')
        registry = export_workers(self.root / 'registry', self.store)
        snapshot = self.snapshot('dispatch')
        value = agent_ci.dispatch(self.a, snapshot, self.identity, self.root / 'automatic', publication, 'full', registry)
        self.assertEqual(value['worker_id'], 'user-host')
        replay = agent_ci.dispatch(self.a, snapshot, self.identity, self.root / 'automatic', publication, 'full', registry)
        self.assertEqual(replay, value)
