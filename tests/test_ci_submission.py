"""Single-file CI submission, before registration and across delivery failures."""

import copy
from unittest.mock import patch

from gdi.ci import CiClient
from gdi.exchange import decode, encode
from gdi.git import GdiError
from gdi.inbox import filename
from tests.test_inbox import InboxTestCase
from tests import test_agent_ci


class SingleSubmissionTests(InboxTestCase):
    def test_one_upload_is_complete_and_worker_archives_it_before_execution(self):
        worker = self.worker()
        _, publication, _ = self.ea.push('drive')
        self.store.uploads.clear()
        client = CiClient(self.ea, 'drive')
        req = client.submit(publication, 'user-host')
        self.assertEqual(len(self.store.uploads), 1)
        self.assertTrue(self.store.uploads[0].startswith('inbox/'))
        event = decode(self.store.data[self.store.uploads[0]])
        self.assertEqual(event['inbox_version'], 2)
        self.assertEqual(event['request'], req)
        self.assertFalse(any('/ci/jobs/' in path for path in self.store.data))
        self.assertEqual(client.status(req['job_id'])['state'], 'QUEUED')
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        prefix = 'repos/a/ci/jobs/' + req['job_id']
        self.assertEqual(decode(self.store.data[prefix + '/submission.json']), event)
        self.assertNotIn(prefix + '/request.json', self.store.data)
        self.assertNotIn(prefix + '/request.ready', self.store.data)
        self.assertEqual(self.counter.read_text(), 'x')

    def test_other_checkout_reads_queued_request_from_one_inbox_file(self):
        worker = self.worker()
        _, req = self.submit(worker)
        other = CiClient(self.eb, 'drive')
        self.assertEqual(other.status(req['job_id'])['state'], 'QUEUED')
        self.assertEqual(other.load_request(req['job_id'])[0], req)

    def test_gc_is_blocked_by_unregistered_submission(self):
        worker = self.worker()
        self.submit(worker)
        with self.assertRaisesRegex(GdiError, 'CI'):
            self.ea.gc('drive', apply=True, quiescent=True, report=lambda value: None)

    def test_cancel_before_worker_registration_never_executes(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.assertEqual(client.cancel(req['job_id'])['state'], 'CANCEL_REQUESTED')
        worker.tick()
        self.assertFalse(self.counter.exists())
        result = client.status(req['job_id'])
        self.assertEqual((result['state'], result['verified']), ('CANCELLED', True))

    def test_nested_request_tamper_and_wrong_worker_are_not_executed(self):
        worker = self.worker()
        _, req = self.submit(worker)
        path, original = next(iter(self.ci_events().items()))
        changed = copy.deepcopy(original)
        changed['request']['head'] = 'f' * 40
        self.store.data[path] = encode(changed)
        worker.tick()
        self.assertIsNone(worker.ledger.get(req['job_id']))
        self.assertFalse(self.counter.exists())
        self.store.data[path] = encode(original)
        worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')

    def test_lost_submission_upload_ack_reuses_same_job_and_bytes(self):
        worker = self.worker()
        _, publication, _ = self.ea.push('drive')
        client = CiClient(self.ea, 'drive')
        upload = self.store.upload
        lost = []
        def fail(source, path):
            upload(source, path)
            if path.startswith('inbox/') and not lost:
                lost.append(path)
                raise GdiError('lost upload acknowledgement')
        with patch.object(self.store, 'upload', side_effect=fail):
            with self.assertRaisesRegex(GdiError, 'acknowledgement'):
                client.submit(publication, 'user-host')
        req = client.submit(publication, 'user-host')
        self.assertEqual(next(iter(self.ci_events())), lost[0])
        worker.tick()
        self.assertEqual(client.submit(publication, 'user-host'), req)
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertFalse(self.ci_events())

    def test_archive_failure_does_not_execute_and_retry_recovers(self):
        worker = self.worker()
        client, req = self.submit(worker)
        self.store.fail = 'repos/a/ci/jobs/' + req['job_id'] + '/submission.json'
        worker.tick()
        self.assertFalse(self.counter.exists())
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'DISCOVERED')
        self.store.fail = None
        worker.tick()
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
        self.assertEqual(self.counter.read_text(), 'x')

    def test_conflicting_packets_for_one_job_are_rejected_before_discovery(self):
        from gdi.inbox import notification, publish
        worker = self.worker()
        _, req = self.submit(worker)
        changed = copy.deepcopy(req)
        changed['workflow']['inputs']['MODE'] = 'different'
        packet = notification(self.identity, 'repos/a', changed, changed['publication_id'],
                              changed, encode(changed))
        publish(self.factory('memory:hub'), packet)
        worker.tick()
        self.assertFalse(self.counter.exists())
        self.assertIsNone(worker.ledger.get(req['job_id']))
        del self.store.data['inbox/' + filename(packet)]
        worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')

    def test_conflicting_archived_submission_is_preserved_without_execution(self):
        worker = self.worker()
        _, req = self.submit(worker)
        path = 'repos/a/ci/jobs/' + req['job_id'] + '/submission.json'
        self.store.data[path] = b'conflicting archive'
        worker.tick()
        self.assertFalse(self.counter.exists())
        self.assertEqual(self.store.data[path], b'conflicting archive')
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'DISCOVERED')

    def test_older_worker_is_rejected_before_any_submission_upload(self):
        worker = self.worker()
        key = 'ci/workers/user-host/capabilities.json'
        caps = decode(self.store.data[key])
        del caps['ci_submission_version']
        self.store.data[key] = encode(caps)
        _, publication, _ = self.ea.push('drive')
        self.store.uploads.clear()
        with self.assertRaisesRegex(GdiError, 'update worker'):
            CiClient(self.ea, 'drive').submit(publication, 'user-host')
        self.assertEqual(self.store.uploads, [])

    def test_oversized_request_is_rejected_before_remote_upload(self):
        self.worker()
        _, publication, _ = self.ea.push('drive')
        self.store.uploads.clear()
        with self.assertRaisesRegex(GdiError, 'metadata'):
            CiClient(self.ea, 'drive').submit(publication, 'user-host',
                workflow={'inputs': {'BLOB': 'x' * (1024 * 1024)}})
        self.assertEqual(self.store.uploads, [])

    def test_missing_global_capabilities_do_not_fall_back_to_registered_worker(self):
        self.worker()
        key = 'ci/workers/user-host/capabilities.json'
        self.store.data['repos/a/' + key] = self.store.data.pop(key)
        _, publication, _ = self.ea.push('drive')
        self.store.uploads.clear()
        with self.assertRaisesRegex(GdiError, 'update worker'):
            CiClient(self.ea, 'drive').submit(publication, 'user-host')
        self.assertEqual(self.store.uploads, [])

    def test_old_multifile_ci_notification_is_rejected(self):
        from gdi.inbox import validate
        worker = self.worker()
        _, req = self.submit(worker)
        packet = next(iter(self.ci_events().values()))
        packet['inbox_version'] = 1
        del packet['request']
        with self.assertRaisesRegex(GdiError, 'self-contained v2'):
            validate(packet)
        self.assertIsNone(worker.ledger.get(req['job_id']))


class OfflineSingleSubmissionTests(InboxTestCase):
    snapshot = test_agent_ci.AgentCiTests.snapshot
    capabilities = test_agent_ci.AgentCiTests.capabilities
    prepare = test_agent_ci.AgentCiTests.prepare
    transfer = test_agent_ci.AgentCiTests.transfer
    proof = test_agent_ci.AgentCiTests.proof
    def test_offline_accept_needs_one_inbox_proof_and_no_job_snapshot(self):
        from gdi import agent_ci
        worker = self.worker()
        value = self.prepare(worker)
        self.assertEqual(set(value['files']), {'inbox'})
        self.assertEqual(value['upload_order'], ['inbox'])
        self.assertTrue(value['safe_to_upload_inbox'])
        self.transfer(value, 'inbox')
        source = self.root / 'prepare' / 'snapshot.json'
        accepted = agent_ci.accept(self.a, self.plan, source, self.identity,
                                   capabilities_path=self.capabilities(), inbox_proof=self.proof(value))
        self.assertEqual(accepted['state'], 'accepted')
        pending = agent_ci.result(self.a, self.plan, self.snapshot('pending', value['job_id']), self.identity)
        self.assertEqual((pending['state'], pending['verified']), ('PENDING', False))
        worker.tick()
        snapshot = self.snapshot('complete', value['job_id'], True)
        document = decode(snapshot.read_bytes())
        document['files'] = [item for item in document['files'] if not item['local_path'].endswith('/submission.json')]
        snapshot.write_bytes(encode(document))
        result = agent_ci.result(self.a, self.plan, snapshot, self.identity)
        self.assertEqual((result['state'], result['verified']), ('PASS', True))
