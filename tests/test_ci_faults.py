"""Faults around durability, execution and delivery must never silently rerun CI."""

from pathlib import Path
import os
import sys
from unittest.mock import patch

from gdi.ci_protocol import atomic_write
from gdi.exchange import decode
from gdi.git import GdiError
from gdi.worker import Worker
from tests.test_ci import CiTestCase


class Crash(BaseException):
    pass


class CiFaultTests(CiTestCase):
    def counted_worker(self):
        self.counter = self.root / 'executions'
        script = (f"from pathlib import Path; p=Path({str(self.counter)!r}); "
                  "p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
                  "Path('one.bin').write_bytes(b'one'); Path('two.bin').write_bytes(b'two'); print('LOG')")
        return self.worker(self.config([sys.executable, '-c', script], artifacts=[
            {'name': 'one.bin', 'path': 'one.bin', 'required': True},
            {'name': 'two.bin', 'path': 'two.bin', 'required': True}]))

    def restart(self, worker):
        config = worker.config
        worker.close()
        self._cleanups.pop()
        resumed = Worker(config, lambda url: self.store)
        self.addCleanup(resumed.close)
        return resumed

    def test_crashes_before_and_after_ledger_transitions(self):
        worker = self.counted_worker()
        for phase in ('before', 'after'):
            for state in ('CLAIMED', 'RESTORING', 'RUNNING', 'FINALIZING', 'RESULT_READY', 'UPLOAD_PENDING', 'PUBLISHED'):
                with self.subTest(phase=phase, state=state):
                    self.commit(self.a, phase + '-' + state)
                    client, req = self.submit(worker)
                    original = worker.ledger.update
                    hit = []
                    count = len(self.counter.read_text()) if self.counter.exists() else 0
                    def crash(jid, value, process=None):
                        if value == state and not hit:
                            hit.append(value)
                            if phase == 'after': original(jid, value, process)
                            raise Crash(state)
                        return original(jid, value, process)
                    with patch.object(worker.ledger, 'update', side_effect=crash):
                        with self.assertRaises(Crash): worker.tick()
                    self.assertTrue(hit)
                    committed_state = worker.ledger.get(req['job_id'])['state']
                    executed = len(self.counter.read_text()) if self.counter.exists() else 0
                    worker = self.restart(worker)
                    if committed_state in ('RUNNING', 'FINALIZING', 'RESULT_READY', 'UPLOAD_PENDING', 'PUBLISHED'):
                        with patch('gdi.worker.execute', side_effect=AssertionError('uncertain/completed run must not repeat')):
                            worker.tick()
                        self.assertEqual(len(self.counter.read_text()) if self.counter.exists() else 0, executed)
                    else:
                        worker.tick()
                    result = client.status(req['job_id'])
                    self.assertTrue(result['verified'])
                    self.assertIn(result['state'], ('PASS', 'INTERRUPTED'))
                    if result['state'] == 'PASS':
                        self.assertEqual(len(self.counter.read_text()) - count, 1)
                    self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'PUBLISHED')

    def test_discovery_commit_failure_keeps_queue_and_does_not_execute(self):
        worker = self.counted_worker(); client, req = self.submit(worker)
        original = worker.ledger.discover
        for after in (False, True):
            with self.subTest(after_commit=after):
                def fail(*args, **kwargs):
                    if after: original(*args, **kwargs)
                    raise OSError('ledger commit unavailable')
                with patch.object(worker.ledger, 'discover', side_effect=fail):
                    worker.discover()
                self.assertFalse(self.counter.exists())
                self.assertIn('ci/queue/' + req['job_id'] + '.json', self.store.data)
        worker = self.restart(worker); worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_artifact_and_result_upload_failures_before_or_after_remote_write(self):
        worker = self.counted_worker()
        for after in (False, True):
            for suffix in ('artifacts/one.bin', 'artifacts/two.bin', 'build.log', 'final-status.json', 'result.json'):
                with self.subTest(after_write=after, path=suffix):
                    self.commit(self.a, str(after) + suffix)
                    client, req = self.submit(worker)
                    path = 'ci/jobs/' + req['job_id'] + '/' + suffix
                    original = self.store.upload; hit = []
                    def fail(source, target):
                        if target == path and not hit:
                            hit.append(target)
                            if after: original(source, target)
                            raise GdiError('upload response lost' if after else 'upload unavailable')
                        original(source, target)
                    with patch.object(self.store, 'upload', side_effect=fail): worker.tick()
                    self.assertTrue(hit)
                    executed = self.counter.read_text()
                    worker = self.restart(worker)
                    with patch('gdi.worker.execute', side_effect=AssertionError('delivery must not rerun')):
                        worker.tick()
                    self.assertEqual(self.counter.read_text(), executed)
                    self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
                    self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'PUBLISHED')

    def test_claim_metadata_lost_ack_and_queue_ack_failure_are_safe(self):
        worker = self.counted_worker(); client, req = self.submit(worker)
        original = self.store.upload; hit = []
        def lost_claim(source, target):
            original(source, target)
            if target.endswith('/worker.running.json') and not hit:
                hit.append(target); raise GdiError('claim acknowledgement lost')
        with patch.object(self.store, 'upload', side_effect=lost_claim): worker.tick()
        self.assertFalse(self.counter.exists())
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'CLAIMED')
        worker = self.restart(worker)
        with patch.object(self.store, 'delete_queue', side_effect=GdiError('queue ack lost')):
            worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'PUBLISHED')
        worker = self.restart(worker)
        with patch('gdi.worker.execute', side_effect=AssertionError('published job must not rerun')): worker.tick()
        self.assertEqual(self.counter.read_text(), 'x')
        self.assertNotIn('ci/queue/' + req['job_id'] + '.json', self.store.data)
        self.assertEqual(client.status(req['job_id'])['state'], 'PASS')

    def test_final_result_atomic_write_failures_before_and_after_replace(self):
        worker = self.counted_worker()
        for after in (False, True):
            self.commit(self.a, 'result-' + str(after)); client, req = self.submit(worker)
            from gdi import worker as module
            original = module.atomic_write; hit = []
            def fail(path, data):
                if Path(path).name == 'result.json' and not hit:
                    hit.append(path)
                    if after: original(path, data)
                    raise OSError('result fsync unavailable')
                original(path, data)
            with patch('gdi.worker.atomic_write', side_effect=fail): worker.tick()
            self.assertTrue(hit)
            executed = self.counter.read_text()
            worker = self.restart(worker)
            with patch('gdi.worker.execute', side_effect=AssertionError('finalizing must not rerun')): worker.tick()
            self.assertEqual(self.counter.read_text(), executed)
            self.assertEqual(client.status(req['job_id'])['state'], 'PASS' if after else 'INTERRUPTED')

    def test_atomic_write_fsync_replace_and_directory_fsync_boundaries(self):
        root = self.root / 'atomic'; root.mkdir()
        path = root / 'value.json'; path.write_bytes(b'old')
        real_fsync = os.fsync
        for step in ('file', 'replace', 'directory'):
            with self.subTest(step=step):
                path.write_bytes(b'old')
                calls = []
                def fsync(fd):
                    calls.append(fd)
                    if (step == 'file' and len(calls) == 1) or (step == 'directory' and len(calls) == 2):
                        raise OSError('fsync unavailable')
                    return real_fsync(fd)
                context = patch('gdi.ci_protocol.os.replace', side_effect=OSError('replace unavailable')) if step == 'replace' else patch('gdi.ci_protocol.os.fsync', side_effect=fsync)
                with context, self.assertRaises(OSError): atomic_write(path, b'new')
                self.assertEqual(path.read_bytes(), b'new' if step == 'directory' else b'old')
                self.assertEqual(list(root.iterdir()), [path])
                atomic_write(path, b'new')
                self.assertEqual(path.read_bytes(), b'new')

    def test_execution_process_record_crash_kills_child_and_recovery_never_reruns(self):
        marker = self.root / 'must-not-complete'
        worker = self.worker(self.config([sys.executable, '-c',
            f"import time; from pathlib import Path; time.sleep(.3); Path({str(marker)!r}).touch()" ]))
        client, req = self.submit(worker)
        original = worker.ledger.update; captured = []
        def fail(jid, state, process=None):
            if state == 'RUNNING' and process is not None and not captured:
                captured.append(process); original(jid, state, process); raise Crash('process identity committed')
            return original(jid, state, process)
        with patch.object(worker.ledger, 'update', side_effect=fail), self.assertRaises(Crash): worker.tick()
        self.assertTrue(captured)
        with self.assertRaises(ProcessLookupError): os.kill(captured[0]['pid'], 0)
        worker = self.restart(worker)
        with patch('gdi.worker.execute', side_effect=AssertionError('uncertain execution must not repeat')): worker.tick()
        self.assertFalse(marker.exists())
        self.assertEqual(client.status(req['job_id'])['state'], 'INTERRUPTED')

    def test_durable_chunk_fsync_failure_before_or_after_write_recovers_same_result(self):
        worker = self.counted_worker()
        for after in (False, True):
            self.commit(self.a, 'chunk-' + str(after)); client, req = self.submit(worker)
            from gdi import worker as module
            original = module.atomic_write; hit = []
            def fail(path, data):
                if Path(path).parent.name == 'log-chunks':
                    hit.append(path)
                    if after: original(path, data)
                    raise OSError('chunk fsync unavailable')
                return original(path, data)
            with patch('gdi.worker.atomic_write', side_effect=fail): worker.tick()
            self.assertTrue(hit)
            executed = self.counter.read_text()
            self.assertEqual(worker.ledger.get(req['job_id'])['state'], 'UPLOAD_PENDING')
            worker = self.restart(worker)
            with patch('gdi.worker.execute', side_effect=AssertionError('chunk delivery must not rerun')): worker.tick()
            self.assertEqual(self.counter.read_text(), executed)
            self.assertEqual(client.status(req['job_id'])['state'], 'PASS')
