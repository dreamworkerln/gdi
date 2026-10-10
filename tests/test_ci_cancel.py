"""Cancellation across offline hosts, local queues and live execution."""
import copy
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from gdi.ci_protocol import cancellation, validate_cancellation
from gdi.exchange import decode, digest, encode
from gdi.git import GdiError
from gdi.worker import Worker
from tests.test_ci import CiTestCase
from tests.test_inbox import InboxTestCase


class LegacyCancellationTests(CiTestCase):
    def test_withdraw_unsupported_worker_keeps_history_and_blocks_unconfirmed_retry(self):
        worker = self.worker()
        client, req = self.submit(worker)
        caps_path = 'ci/workers/user-host/capabilities.json'
        caps = decode(self.store.data[caps_path]); caps.pop('cancel_version')
        self.store.data[caps_path] = encode(caps)
        with self.assertRaisesRegex(GdiError, 'does not advertise cancellation'):
            client.cancel(req['job_id'])
        value = client.cancel(req['job_id'], withdraw=True)
        self.assertTrue(value['queue_withdrawn'])
        self.assertFalse(value['worker_cancellation_supported'])
        prefix = 'ci/jobs/' + req['job_id']
        self.assertIn(prefix + '/request.json', self.store.data)
        self.assertIn(prefix + '/request.ready', self.store.data)
        self.assertIn(prefix + '/cancel.json', self.store.data)
        self.assertNotIn('ci/queue/' + req['job_id'] + '.json', self.store.data)
        self.assertEqual(client.cancel(req['job_id'], withdraw=True), value)
        with self.assertRaisesRegex(GdiError, 'does not confirm execution cancellation'):
            client.retry(req['job_id'])

    def test_cancel_before_start_is_verified_without_checkout_or_execution(self):
        worker = self.worker()
        client, req = self.submit(worker)
        value = client.cancel(req['job_id'])
        self.assertEqual(value['state'], 'CANCEL_REQUESTED')
        self.assertFalse(value['verified'])
        with patch.object(worker, 'checkout', side_effect=AssertionError('cancelled job must not restore')):
            worker.tick()
        value = client.status(req['job_id'])
        self.assertEqual(value['state'], 'CANCELLED')
        self.assertTrue(value['verified'])
        self.assertEqual(value['exit_code'], 130)
        self.assertEqual(value['stages'], [])

    def test_running_cancel_stops_process_and_does_not_run_next_stage(self):
        marker = self.root / 'running'
        next_stage = self.root / 'next-stage'
        config = self.config([sys.executable, '-c',
                             f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); print('START',flush=True); time.sleep(20)"])
        config['poll_active_seconds'] = .05
        profile = config['repositories'][0]['profiles']['full']
        profile['stages'].append({**profile['stages'][0], 'name': 'next',
                                 'argv': [sys.executable, '-c', f"open({str(next_stage)!r},'w').close()"]})
        worker = self.worker(config)
        client, req = self.submit(worker)
        errors = []
        def cancel_when_running():
            try:
                deadline = time.monotonic() + 5
                while not marker.exists():
                    if time.monotonic() > deadline:raise AssertionError('stage did not start')
                    time.sleep(.01)
                client.cancel(req['job_id'])
            except BaseException as exc:errors.append(exc)
        observer = threading.Thread(target=cancel_when_running)
        observer.start()
        worker.tick()
        observer.join(6)
        self.assertFalse(observer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(next_stage.exists())
        value = client.status(req['job_id'])
        self.assertEqual(value['state'], 'CANCELLED')
        self.assertTrue(value['verified'])
        self.assertIn(b'START', (client.root/'results'/req['job_id']/'build.log').read_bytes())


class SharedCancellationTests(InboxTestCase):
    def test_saved_pass_cancelled_before_restart_preserves_partial_uploads(self):
        worker = self.worker()
        client, req = self.submit(worker)
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        self.store.fail = prefix + '/result.json'
        worker.tick()
        spool = worker.spool(req['job_id'])
        original_result = (spool / 'result.json').read_bytes()
        original_files = {path: data for path, data in self.store.data.items()
                          if path.startswith(prefix + '/')}
        self.assertEqual(decode(original_result)['state'], 'PASS')
        self.assertEqual(client.cancel(req['job_id'])['state'], 'CANCEL_REQUESTED')
        config = worker.config
        worker.close(); self._cleanups.pop()
        self.store.fail = None
        resumed = self.worker(config)
        with patch.object(resumed, 'checkout', side_effect=AssertionError('do not execute again')):
            resumed.tick()
        self.assertEqual((spool / 'result.json').read_bytes(), original_result)
        for path, data in original_files.items():
            if not path.endswith('/status.json'):
                self.assertEqual(self.store.data[path], data)
        self.assertNotIn(prefix + '/result.json', self.store.data)
        self.assertIn(prefix + '/cancelled/result.json', self.store.data)
        value = client.status(req['job_id'])
        self.assertEqual(value['state'], 'CANCELLED')
        self.assertTrue(value['verified'])
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(resumed.ledger.pending(), [])

    def cancel_during_upload(self, suffix, *, lost_ack=False):
        worker = self.worker()
        client, req = self.submit(worker)
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        upload = self.store.upload
        triggered = []
        def race(source, path):
            if path == prefix + suffix and not triggered:
                triggered.append(True)
                self.assertEqual(client.cancel(req['job_id'])['state'], 'CANCEL_REQUESTED')
                upload(source, path)
                if lost_ack:
                    raise GdiError('lost upload response after cancellation')
                return
            upload(source, path)
        with patch.object(self.store, 'upload', side_effect=race):
            worker.tick()
        self.assertEqual(triggered, [True])
        return worker, client, req, prefix

    def test_cancel_during_artifact_delivery_never_commits_original_pass(self):
        worker, client, req, prefix = self.cancel_during_upload('/build.log')
        self.assertNotIn(prefix + '/result.json', self.store.data)
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCELLED')
        self.assertEqual(self.counter.read_text(), 'x')

    def test_cancel_during_result_upload_suppresses_pass_already_in_flight(self):
        worker, client, req, prefix = self.cancel_during_upload('/result.json')
        self.assertEqual(decode(self.store.data[prefix + '/result.json'])['state'], 'PASS')
        value = client.status(req['job_id'])
        self.assertEqual(value['state'], 'CANCELLED')
        self.assertTrue(value['verified'])
        self.assertEqual(self.counter.read_text(), 'x')

    def test_lost_result_upload_ack_with_cancel_resumes_only_cancelled_delivery(self):
        from gdi.gc import ci_guard
        worker, client, req, prefix = self.cancel_during_upload('/result.json', lost_ack=True)
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCEL_REQUESTED')
        with self.assertRaisesRegex(GdiError, 'unconfirmed CI cancellation'):
            ci_guard(self.ea, client.transport, self.identity)
        config = worker.config
        worker.close(); self._cleanups.pop()
        resumed = self.worker(config)
        resumed.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCELLED')
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(decode(self.store.data[prefix + '/result.json'])['state'], 'PASS')
        self.assertTrue(ci_guard(self.ea, client.transport, self.identity))

    def test_cancelled_delivery_lost_ack_is_immutable_and_restores_marker(self):
        worker = self.worker()
        client, req = self.submit(worker)
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        self.store.fail = prefix + '/result.json'
        worker.tick()
        self.store.fail = None
        client.cancel(req['job_id'])
        upload = self.store.upload
        def lose_ack(source, path):
            upload(source, path)
            if path == prefix + '/cancelled/result.json':
                raise GdiError('lost cancellation result acknowledgement')
        with patch.object(self.store, 'upload', side_effect=lose_ack):
            worker.tick()
        saved = self.store.data[prefix + '/cancelled/result.json']
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'UPLOAD_PENDING')
        self.store.data.pop(prefix + '/cancel.json')
        config = worker.config
        worker.close(); self._cleanups.pop()
        resumed = self.worker(config)
        resumed.tick()
        self.assertEqual(self.store.data[prefix + '/cancelled/result.json'], saved)
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCELLED')
        self.assertEqual(resumed.ledger.pending(), [])
        self.assertEqual(self.counter.read_text(), 'x')

    def test_cancellation_arriving_during_client_verification_fences_pass(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.tick()
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        raw = self.store.data[prefix + '/request.json']
        download = self.store.download
        def cancel_after_download(path, target):
            download(path, target)
            if path == prefix + '/build.log':
                self.store.data[prefix + '/cancel.json'] = encode(cancellation(req, raw))
        with patch.object(self.store, 'download', side_effect=cancel_after_download):
            value = client.status(req['job_id'])
        self.assertEqual(value['state'], 'CANCEL_REQUESTED')
        self.assertFalse(value['verified'])
        with self.assertRaisesRegex(GdiError, 'verified PASS'):
            client.pull_passed(req['job_id'], 'full')

    def test_observed_cancellation_cannot_be_revoked_by_missing_remote_marker(self):
        worker, client, req, prefix = self.cancel_during_upload('/result.json', lost_ack=True)
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCEL_REQUESTED')
        self.store.data.pop(prefix + '/cancel.json')
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCEL_REQUESTED')
        with self.assertRaisesRegex(GdiError, 'verified PASS'):
            client.pull_passed(req['job_id'], 'full')

    def test_undelivered_local_cancellation_is_not_proof_for_retry(self):
        worker = self.worker()
        client, req = self.submit(worker)
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        self.store.fail = prefix + '/cancel.json'
        with self.assertRaisesRegex(GdiError, 'network unavailable'):
            client.cancel(req['job_id'])
        with self.assertRaisesRegex(GdiError, 'active job'):
            client.retry(req['job_id'])
        self.store.fail = None
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_cancelled_delivery_requires_complete_log_and_matching_claim(self):
        worker, client, req, prefix = self.cancel_during_upload('/result.json')
        path = prefix + '/cancelled/build.log'
        saved = self.store.data[path]
        self.store.data[path] = saved + b'corrupt'
        with self.assertRaisesRegex(GdiError, 'checksum'):
            client.status(req['job_id'])
        self.store.data[path] = saved
        claim_path = prefix + '/worker.running.json'
        claim = decode(self.store.data[claim_path])
        claim['run_id'] = 'f' * 32
        self.store.data[claim_path] = encode(claim)
        with self.assertRaisesRegex(GdiError, 'execution claim'):
            client.status(req['job_id'])

    def test_cancelled_delivery_recovers_lost_inbox_deletion_after_published(self):
        worker = self.worker()
        client, req = self.submit(worker)
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        self.store.fail = prefix + '/result.json'
        worker.tick()
        self.store.fail = None
        client.cancel(req['job_id'])
        self.store.fail = 'delete-notification'
        worker.tick()
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'PUBLISHED')
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCELLED')
        config = worker.config
        worker.close(); self._cleanups.pop()
        self.store.fail = None
        resumed = self.worker(config)
        with patch.object(resumed, 'checkout', side_effect=AssertionError('do not execute again')):
            resumed.tick()
        self.assertNotIn(req['job_id'], {event['job_id'] for event in self.ci_events().values()})
        self.assertEqual(self.counter.read_text(), 'x')

    def test_withdraw_removes_only_matching_inbox_event(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.commit(self.a, 'another request')
        _, other_publication, _ = self.ea.push('drive')
        other = client.submit(other_publication, 'user-host')
        caps_path = 'ci/workers/user-host/capabilities.json'
        caps = decode(self.store.data[caps_path]); caps.pop('cancel_version')
        self.store.data[caps_path] = encode(caps)
        value = client.cancel(req['job_id'], withdraw=True)
        self.assertTrue(value['queue_withdrawn'])
        self.assertFalse(value['worker_cancellation_supported'])
        self.assertEqual({e['job_id'] for e in self.ci_events().values()}, {other['job_id']})
        self.assertIn('repos/a/ci/jobs/'+req['job_id']+'/cancel.json', self.store.data)
        self.assertEqual(client.cancel(req['job_id'], withdraw=True), value)

    def test_offline_discovered_job_cancelled_and_replaced_on_other_host(self):
        worker_a = self.worker()
        client, req = self.submit(worker_a)
        worker_a.discover()
        self.assertEqual(worker_a.ledger.get(req['job_id'])['state'], 'DISCOVERED')
        config_a = copy.deepcopy(worker_a.config)
        worker_a.close()
        self._cleanups.pop()
        config_b = self.config();config_b['worker_id'] = 'other-host'
        worker_b = self.worker(config_b)
        self.assertEqual(client.cancel(req['job_id'])['state'], 'CANCEL_REQUESTED')
        replacement = client.retry(req['job_id'], worker_id='other-host')
        self.assertNotEqual(replacement['job_id'], req['job_id'])
        self.assertEqual(replacement['retry_of'], req['job_id'])
        self.assertEqual(replacement['head'], req['head'])
        worker_b.tick()
        self.assertEqual(client.status(replacement['job_id'])['state'], 'PASS')
        self.assertEqual(self.counter.read_text(), 'x')
        resumed_a = self.worker(config_a)
        # Still cancelled even if the inbox entry disappeared: ledger already owns it.
        for path,event in self.ci_events().items():
            if event['job_id']==req['job_id']:self.store.delete_queue(path)
        with patch.object(resumed_a, 'checkout', side_effect=AssertionError('old job must not execute')):
            resumed_a.tick()
        result = client.status(req['job_id'])
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertTrue(result['verified'])
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(client.retry(req['job_id'], worker_id='other-host'), replacement)

    def test_invalid_cancellation_never_starts_queued_job(self):
        worker = self.worker()
        client, req = self.submit(worker)
        raw = self.store.data['repos/a/ci/jobs/'+req['job_id']+'/request.json']
        marker = cancellation(req, raw)
        marker['request_sha256'] = 'f'*64
        self.store.data['repos/a/ci/jobs/'+req['job_id']+'/cancel.json'] = encode(marker)
        with patch.object(worker, 'checkout', side_effect=AssertionError('must not restore')):
            worker.tick()
        self.assertFalse(self.counter.exists())
        self.assertNotEqual(worker.ledger.get(req['job_id'])['state'], 'PUBLISHED')
        with self.assertRaisesRegex(GdiError, 'cancellation/request identity'):
            client.status(req['job_id'])

    def test_lost_cancellation_upload_ack_is_reused_with_same_bytes(self):
        worker = self.worker()
        client, req = self.submit(worker)
        target = 'repos/a/ci/jobs/'+req['job_id']+'/cancel.json'
        upload = self.store.upload
        def lost_ack(source, path):
            upload(source, path)
            if path==target:raise GdiError('lost cancellation acknowledgement')
        with patch.object(self.store, 'upload', side_effect=lost_ack):
            with self.assertRaisesRegex(GdiError, 'acknowledgement'):
                client.cancel(req['job_id'])
        saved = self.store.data[target]
        self.assertEqual(client.cancel(req['job_id'])['state'], 'CANCEL_REQUESTED')
        self.assertEqual(self.store.data[target], saved)
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCELLED')
        self.assertFalse(self.counter.exists())

    def test_completed_result_wins_over_late_cancel(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.tick()
        previous = client.status(req['job_id'])
        self.assertEqual(client.cancel(req['job_id']), previous)
        self.assertNotIn('repos/a/ci/jobs/'+req['job_id']+'/cancel.json', self.store.data)

    def test_cancellation_delivery_restart_never_runs_original_job(self):
        worker = self.worker()
        client, req = self.submit(worker)
        client.cancel(req['job_id'])
        self.store.fail='repos/a/ci/jobs/'+req['job_id']+'/result.json'
        worker.tick()
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'UPLOAD_PENDING')
        config = worker.config
        worker.close();self._cleanups.pop()
        self.store.fail=None
        self.store.data.pop('repos/a/ci/jobs/'+req['job_id']+'/cancel.json')
        resumed = self.worker(config)
        with patch.object(resumed, 'checkout', side_effect=AssertionError('cancelled job must not execute')):
            resumed.tick()
        result = client.status(req['job_id'])
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertTrue(result['verified'])
        self.assertFalse(self.counter.exists())

    def test_foreign_claim_does_not_produce_false_stopped_confirmation(self):
        worker = self.worker()
        client, req = self.submit(worker)
        client.cancel(req['job_id'])
        raw = self.store.data['repos/a/ci/jobs/'+req['job_id']+'/request.json']
        self.store.data['repos/a/ci/jobs/'+req['job_id']+'/worker.running.json']=encode({
            'ci_version':1,'job_id':req['job_id'],'run_id':'f'*32,
            'worker_id':req['worker_id'],'request_sha256':digest(raw)})
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'CANCEL_REQUESTED')
        self.assertFalse(self.counter.exists())
        self.assertFalse((worker.spool(req['job_id'])/'result.json').exists())
