import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from gdi import agent_ci
from gdi.exchange import decode, encode
from gdi.git import GdiError
from gdi.inbox import filename
from tests.snapshots import export_snapshot
from tests.test_inbox import InboxTestCase


class AgentCiTests(InboxTestCase):
    def snapshot(self, name='snapshot', job=None, result=False):
        transport = self.factory('memory:hub/repos/a')
        include = ['', 'branches', 'branches/main']
        if job:
            include.extend(['ci', 'ci/jobs', 'ci/jobs/' + job])
            if result:
                include.extend(['ci/jobs/' + job + '/artifacts', 'ci/jobs/' + job + '/log-chunks'])
            if 'cancelled' in {item['Path'] for item in transport.list('ci/jobs/' + job) if item['IsDir']}:
                include.append('ci/jobs/' + job + '/cancelled')
                if result:
                    include.extend(['ci/jobs/' + job + '/cancelled/artifacts',
                                    'ci/jobs/' + job + '/cancelled/log-chunks'])
        return export_snapshot(self.root / name, transport, repository_path='repos/a', include=include)

    def capabilities(self):
        target = self.root / 'capabilities.json'
        target.write_bytes(self.store.data['ci/workers/user-host/capabilities.json'])
        return target

    def prepare(self, worker):
        _, publication, _ = self.ea.push('drive')
        self.plan = self.root / 'ci-plan'
        value = agent_ci.prepare(self.a, self.snapshot('prepare'), self.identity, self.plan,
                                 publication, 'user-host', 'full', self.capabilities())
        return value

    def transfer(self, value, *names):
        for name in names:
            item = value['files'][name]
            path = item['target_folder'] + '/' + item['target_name']
            self.store.mkdir(item['target_folder'])
            self.store.upload(item['local_path'], path)

    def proof(self, value):
        item = value['files']['inbox']
        folder = self.root / 'proof'
        folder.mkdir(exist_ok=True)
        local = folder / 'inbox.json'
        self.store.download('inbox/' + item['target_name'], local)
        entries = [{'id': 'id-' + row['Path'], 'name': row['Path'], 'is_dir': False,
                    'bytes': len(self.store.data['inbox/' + row['Path']])} for row in self.store.list('inbox')]
        proof = folder / 'proof.json'
        proof.write_bytes(encode({'proof_version': 1, 'folder_id': 'inbox-folder',
                                 'pages': [{'page_token': None, 'next_page_token': None, 'entries': entries}],
                                 'file_id': 'id-' + item['target_name'], 'local_path': 'inbox.json'}))
        return proof

    def test_request_order_retry_worker_and_offline_verified_result(self):
        worker = self.worker()
        with patch('gdi.transport.Rclone.call', side_effect=AssertionError('no rclone')), \
                patch('gdi.rc_transport.RcServer.__enter__', side_effect=AssertionError('no RC')):
            value = self.prepare(worker)
            self.assertFalse(value['safe_to_upload_inbox'])
            raw = (self.plan / 'request.json').read_bytes()
            replay = agent_ci.prepare(self.a, self.snapshot('retry'), self.identity, self.plan,
                                      value['publication_id'], 'user-host', 'full', self.capabilities())
            self.assertEqual(replay, value)
            self.assertEqual((self.plan / 'request.json').read_bytes(), raw)
            self.transfer(value, 'request')
            with self.assertRaisesRegex(GdiError, 'download is missing'):
                agent_ci.check(self.a, self.plan, self.snapshot('partial', value['job_id']), self.identity,
                               capabilities_path=self.capabilities())
            worker.tick()
            self.assertFalse(self.counter.exists())
            self.transfer(value, 'ready')
            checked = agent_ci.check(self.a, self.plan, self.snapshot('ready', value['job_id']), self.identity,
                                     capabilities_path=self.capabilities())
            self.assertTrue(checked['safe_to_upload_inbox'])
            worker.tick()
            self.assertFalse(self.counter.exists())
            self.transfer(value, 'inbox')
            accepted = agent_ci.accept(self.a, self.plan, self.snapshot('accept', value['job_id']), self.identity,
                                      capabilities_path=self.capabilities(), inbox_proof=self.proof(value))
            self.assertEqual(accepted['state'], 'accepted')
            worker.tick()
            self.assertEqual(self.counter.read_text(), 'x')
            result = agent_ci.result(self.a, self.plan, self.snapshot('result', value['job_id'], True), self.identity)
            self.assertTrue(result['verified'])
            self.assertEqual(result['state'], 'PASS')
            self.assertEqual(result['head'], value['head'])
            self.assertTrue((Path(result['local_artifacts']) / 'build.log').exists())
            worker.tick()
            self.assertEqual(self.counter.read_text(), 'x')

    def test_stale_capabilities_and_exact_request_ready_bytes(self):
        worker = self.worker()
        value = self.prepare(worker)
        self.transfer(value, 'request', 'ready')
        path = 'repos/a/ci/jobs/' + value['job_id'] + '/request.ready'
        original = self.store.data[path]
        self.store.data[path] = original.replace(b'\n', b' ')
        with self.assertRaisesRegex(GdiError, 'checksum'):
            agent_ci.check(self.a, self.plan, self.snapshot('bad-ready', value['job_id']), self.identity,
                           capabilities_path=self.capabilities())
        self.store.data[path] = original
        caps = decode(self.capabilities().read_bytes()); caps['profiles']['full'] = 'f' * 64
        self.capabilities().write_bytes(encode(caps))
        with self.assertRaisesRegex(GdiError, 'revision changed'):
            agent_ci.check(self.a, self.plan, self.snapshot('ready', value['job_id']), self.identity,
                           capabilities_path=self.root / 'capabilities.json')
        self.assertEqual(decode((self.plan / 'plan.json').read_bytes())['state'], 'prepared')

    def test_inbox_duplicate_names_and_download_tamper_rejected(self):
        worker = self.worker(); value = self.prepare(worker)
        self.transfer(value, 'request', 'ready', 'inbox')
        snap = self.snapshot('accept', value['job_id']); proof = self.proof(value)
        original = decode(proof.read_bytes())
        duplicate = copy.deepcopy(original)
        row = copy.deepcopy(next(row for row in duplicate['pages'][0]['entries'] if row['name'] == value['files']['inbox']['target_name']))
        row['id'] += '-duplicate'; duplicate['pages'][0]['entries'].append(row)
        proof.write_bytes(encode(duplicate))
        with self.assertRaisesRegex(GdiError, 'duplicate'):
            agent_ci.accept(self.a, self.plan, snap, self.identity, capabilities_path=self.capabilities(), inbox_proof=proof)
        proof.write_bytes(encode(original))
        local = proof.parent / 'inbox.json'
        raw = local.read_bytes(); local.write_bytes(raw[:-1] + b' ')
        with self.assertRaisesRegex(GdiError, 'checksum'):
            agent_ci.accept(self.a, self.plan, snap, self.identity, capabilities_path=self.capabilities(), inbox_proof=proof)

    def test_result_missing_artifact_corrupt_chunk_and_identity_are_rejected(self):
        worker = self.worker(); value = self.prepare(worker)
        self.transfer(value, 'request', 'ready', 'inbox'); worker.tick()
        snap = self.snapshot('result', value['job_id'], True)
        doc = decode(snap.read_bytes())
        chunk = next(item for item in doc['files'] if '/log-chunks/' in item['local_path'])
        path = snap.parent / chunk['local_path']; raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaisesRegex(GdiError, 'checksum'):
            agent_ci.result(self.a, self.plan, snap, self.identity)
        path.write_bytes(raw)
        self.assertEqual(agent_ci.result(self.a, self.plan, snap, self.identity)['state'], 'PASS')
        missing = copy.deepcopy(doc)
        missing['files'] = [item for item in missing['files'] if not item['local_path'].endswith('final-status.json')]
        snap.write_bytes(encode(missing))
        with self.assertRaisesRegex(GdiError, 'download is missing'):
            agent_ci.result(self.a, self.plan, snap, self.identity)
        snap.write_bytes(encode(doc))
        result_file = next(item for item in doc['files'] if item['local_path'].endswith('result.json'))
        path = snap.parent / result_file['local_path']; result = decode(path.read_bytes())
        result['head'] = 'f' * 40; path.write_bytes(encode(result))
        with self.assertRaisesRegex(GdiError, 'identity'):
            agent_ci.result(self.a, self.plan, snap, self.identity)

    def test_no_completed_result_is_pending_and_ci_is_explicit(self):
        worker = self.worker(); value = self.prepare(worker)
        self.assertFalse(self.ci_events())
        self.transfer(value, 'request', 'ready')
        self.assertEqual(agent_ci.result(self.a, self.plan, self.snapshot('pending', value['job_id']), self.identity),
                         {'job_id': value['job_id'], 'state': 'PENDING', 'verified': False,
                          'heartbeat': {'updated_at': None, 'age_seconds': None, 'timeout_seconds': 600, 'state': 'missing'}})

    def test_explicit_retry_requires_complete_terminal_result_and_creates_new_fixed_id(self):
        worker = self.worker(); value = self.prepare(worker)
        self.transfer(value, 'request', 'ready')
        with self.assertRaisesRegex(GdiError, 'active job'):
            agent_ci.prepare(self.a, self.snapshot('active', value['job_id']), self.identity, self.root / 'retry-plan',
                             value['publication_id'], 'user-host', 'full', self.capabilities(), retry_of=value['job_id'])
        self.transfer(value, 'inbox'); worker.tick()
        snapshot = self.snapshot('terminal', value['job_id'], True)
        retry = agent_ci.prepare(self.a, snapshot, self.identity, self.root / 'retry-plan',
                                 value['publication_id'], 'user-host', 'full', self.capabilities(), retry_of=value['job_id'])
        self.assertNotEqual(retry['job_id'], value['job_id'])
        raw = decode((self.root / 'retry-plan/request.json').read_bytes())
        self.assertEqual(raw['retry_of'], value['job_id'])
        self.assertEqual(agent_ci.prepare(self.a, snapshot, self.identity, self.root / 'retry-plan',
                                         value['publication_id'], 'user-host', 'full', self.capabilities(), retry_of=value['job_id']), retry)

    def test_offline_cancel_plan_and_replacement_worker_use_existing_tools(self):
        worker = self.worker()
        value = self.prepare(worker)
        self.transfer(value, 'request', 'ready', 'inbox')
        worker.discover()
        config = worker.config
        old_capabilities = self.root / 'old-capabilities.json'
        old_capabilities.write_bytes(self.capabilities().read_bytes())
        worker.close();self._cleanups.pop()
        other_config = self.config();other_config['worker_id'] = 'other-host'
        other = self.worker(other_config)
        next_caps = self.root / 'next-capabilities.json'
        next_caps.write_bytes(self.store.data['ci/workers/other-host/capabilities.json'])
        cancel_plan = self.root / 'cancel-plan'
        with patch('gdi.transport.Rclone.call', side_effect=AssertionError('no rclone')), \
                patch('gdi.rc_transport.RcServer.__enter__', side_effect=AssertionError('no RC')):
            cancel = agent_ci.cancel(self.a, self.snapshot('cancel', value['job_id']), self.identity,
                                     cancel_plan, value['job_id'], old_capabilities)
            self.assertTrue(cancel['safe_to_upload_cancel'])
            self.assertEqual(cancel['upload_order'], ['cancel'])
            self.assertEqual(agent_ci.cancel(self.a, self.snapshot('cancel-replay', value['job_id']), self.identity,
                                             cancel_plan, value['job_id'], old_capabilities), cancel)
            with self.assertRaisesRegex(GdiError, 'download is missing'):
                agent_ci.cancel_check(self.a, cancel_plan, self.snapshot('before-upload', value['job_id']), self.identity)
            self.transfer(cancel, 'cancel')
            checked = agent_ci.cancel_check(self.a, cancel_plan, self.snapshot('cancel-proof', value['job_id']), self.identity)
            self.assertEqual(checked['state'], 'accepted')
            self.assertTrue(checked['cancellation_requested'])
            self.assertFalse(checked['verified'])
            pending = agent_ci.result(self.a, self.plan, self.snapshot('cancel-pending', value['job_id']), self.identity)
            self.assertEqual(pending['state'], 'CANCEL_REQUESTED')
            retry_plan = self.root / 'replacement-plan'
            replacement = agent_ci.prepare(self.a, self.snapshot('replace', value['job_id']), self.identity,
                                           retry_plan, value['publication_id'], 'other-host', 'full', next_caps,
                                           retry_of=value['job_id'])
            replacement_raw = decode((retry_plan/'request.json').read_bytes())
            self.assertNotEqual(replacement['job_id'], value['job_id'])
            self.assertEqual(replacement_raw['retry_of'], value['job_id'])
            self.assertEqual(replacement['head'], value['head'])
            self.transfer(replacement, 'request', 'ready', 'inbox')
            other.tick()
            self.assertEqual(self.counter.read_text(), 'x')
            resumed = self.worker(config)
            resumed.tick()
            result = agent_ci.result(self.a, self.plan, self.snapshot('cancel-result', value['job_id'], True), self.identity)
            self.assertEqual(result['state'], 'CANCELLED')
            self.assertTrue(result['verified'])
            self.assertEqual(self.counter.read_text(), 'x')

    def test_late_pass_is_fenced_and_cancelled_result_verified_offline(self):
        worker = self.worker()
        value = self.prepare(worker)
        self.transfer(value, 'request', 'ready', 'inbox')
        prefix = 'repos/a/ci/jobs/' + value['job_id']
        self.store.fail = prefix + '/result.json'
        worker.tick()
        self.store.fail = None
        cancel = agent_ci.cancel(self.a, self.snapshot('late-cancel', value['job_id']), self.identity,
                                 self.root / 'late-cancel-plan', value['job_id'], self.capabilities())
        self.transfer(cancel, 'cancel')
        # Simulate an original result upload already in flight when cancellation arrived.
        self.store.upload(worker.spool(value['job_id']) / 'result.json', prefix + '/result.json')
        with patch('gdi.transport.Rclone.call', side_effect=AssertionError('no rclone')):
            pending = agent_ci.result(self.a, self.plan, self.snapshot('late-pass', value['job_id']), self.identity)
        self.assertEqual(pending['state'], 'CANCEL_REQUESTED')
        self.assertFalse(pending['verified'])
        worker.tick()
        snapshot = self.snapshot('cancelled-delivered', value['job_id'], True)
        # Original PASS artifacts are historical; only the cancelled result is authoritative.
        document = decode(snapshot.read_bytes())
        document['files'] = [item for item in document['files']
                             if '/cancelled/' in item['local_path']
                             or not any(item['local_path'].endswith('/' + name) for name in
                                        ('result.json', 'build.log', 'final-status.json'))]
        snapshot.write_bytes(encode(document))
        with patch('gdi.transport.Rclone.call', side_effect=AssertionError('no rclone')):
            completed = agent_ci.result(self.a, self.plan, snapshot, self.identity)
        self.assertEqual(completed['state'], 'CANCELLED')
        self.assertTrue(completed['verified'])
        self.assertEqual(self.counter.read_text(), 'x')

    def test_cancel_plan_rejects_unsupported_worker_and_remote_marker_tamper(self):
        worker = self.worker();value = self.prepare(worker)
        self.transfer(value, 'request', 'ready', 'inbox')
        path=self.capabilities()
        caps=decode(path.read_bytes());caps.pop('cancel_version');path.write_bytes(encode(caps))
        with self.assertRaisesRegex(GdiError, 'does not advertise cancellation'):
            agent_ci.cancel(self.a, self.snapshot('unsupported', value['job_id']), self.identity,
                            self.root/'cancel-plan', value['job_id'], path)
        worker.advertise()
        cancel = agent_ci.cancel(self.a, self.snapshot('supported', value['job_id']), self.identity,
                                 self.root/'cancel-plan', value['job_id'], self.capabilities())
        self.transfer(cancel, 'cancel')
        target='repos/a/ci/jobs/'+value['job_id']+'/cancel.json'
        self.store.data[target]=self.store.data[target].replace(b'user-host', b'bad-host-')
        with self.assertRaisesRegex(GdiError, 'checksum'):
            agent_ci.cancel_check(self.a, self.root/'cancel-plan', self.snapshot('tampered', value['job_id']), self.identity)
