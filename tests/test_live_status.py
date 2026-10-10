"""Atomic heartbeat replacement must not change an active upload's source."""

from pathlib import Path
import threading
from unittest.mock import patch

from gdi.exchange import decode
from gdi.git import GdiError
from tests.test_ci import CiTestCase
from tests.test_inbox import InboxTestCase


def assert_stable_status_upload(case):
    worker = case.worker()
    _, req = case.submit(worker)
    worker.discover()
    row = worker.ledger.get(req['job_id'])
    repo = (worker.routed_repository(req['repository_id'], 'repos/a') if worker.shared else
            worker.repositories[req['repository_id']])
    transport = worker.transport(repo)
    worker.status(row, 'RUNNING', 'first')
    source = worker.spool(req['job_id']) / 'status.json'
    initial = source.read_bytes()
    job_path = f"ci/jobs/{req['job_id']}/status.json"
    worker_path = f'ci/workers/{worker.worker_id}/status.json'
    entered, release = threading.Event(), threading.Event()
    failures = []
    original = case.store.update_advisory

    def checked_upload(path, target):
        if target.endswith(job_path) and not entered.is_set():
            before = Path(path).read_bytes()
            entered.set()
            if not release.wait(5):
                raise AssertionError('heartbeat writer did not release upload')
            if Path(path).read_bytes() != before:
                raise GdiError('corrupted on transfer: source hashes changed')
        return original(path, target)

    def publish():
        try:
            worker.publish_live(transport, row, set())
        except BaseException as exc:
            failures.append(exc)

    with patch.object(case.store, 'update_advisory', side_effect=checked_upload):
        thread = threading.Thread(target=publish)
        thread.start()
        try:
            case.assertTrue(entered.wait(5), 'publisher did not start status upload')
            worker.status(row, 'RUNNING', 'second')
            case.assertNotEqual(source.read_bytes(), initial)
        finally:
            release.set()
            thread.join(timeout=5)
        case.assertFalse(thread.is_alive())
        case.assertEqual(failures, [])
    job_key = next(key for key in case.store.data if key.endswith(job_path))
    worker_key = next(key for key in case.store.data if key.endswith(worker_path))
    case.assertEqual(case.store.data[job_key], initial)
    worker_status = decode(case.store.data[worker_key])
    initial_status = decode(initial)
    for key, value in initial_status.items():
        if key != 'state':
            case.assertEqual(worker_status[key], value)
    case.assertEqual(worker_status['state'], 'BUSY')
    case.assertTrue(worker_status['busy'])
    case.assertEqual(worker_status['registry_version'], 1)
    # The next pass publishes the new heartbeat rather than freezing old status.
    worker.publish_live(transport, row, set())
    case.assertEqual(case.store.data[job_key], source.read_bytes())
    worker_status = decode(case.store.data[worker_key])
    for key, value in decode(source.read_bytes()).items():
        if key != 'state':
            case.assertEqual(worker_status[key], value)
    case.assertEqual(decode(case.store.data[job_key])['stage'], 'second')


class LegacyLiveStatusTests(CiTestCase):
    def test_heartbeat_update_during_upload_uses_one_stable_snapshot(self):
        assert_stable_status_upload(self)


class GlobalLiveStatusTests(InboxTestCase):
    def test_heartbeat_update_during_upload_uses_one_stable_snapshot(self):
        assert_stable_status_upload(self)
