import io
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from gdi import agent_ci
from gdi.cli import execute, parser
from gdi.exchange import decode, digest, encode
from gdi.git import GdiError
from gdi.scheduling import heartbeat, inspect_workers, policy
from tests import test_agent_ci
from tests.test_ci import CiTestCase
from tests.test_inbox import InboxTestCase


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class HeartbeatTests(unittest.TestCase):
    def test_boundary_missing_bad_timestamp_and_clock_skew(self):
        self.assertEqual(heartbeat(iso(1000), 600, at=1599)['state'], 'fresh')
        stale = heartbeat(iso(1000), 600, at=1600)
        self.assertEqual((stale['state'], stale['age_seconds']), ('stale', 600))
        self.assertEqual(heartbeat(None, 600, at=1600)['state'], 'missing')
        self.assertEqual(heartbeat('invalid', 600, at=1600)['state'], 'invalid')
        skew = heartbeat(iso(1700), 600, at=1600)
        self.assertEqual((skew['state'], skew['age_seconds'], skew['clock_ahead_seconds']), ('clock_skew', None, 100))
        self.assertEqual(heartbeat(iso(1601), 600, at=1600)['age_seconds'], 0)
        for limit in (0, -1, float('nan'), True):
            with self.assertRaises(GdiError):
                heartbeat(None, limit)


class JobHeartbeatTests(CiTestCase):
    def live(self, updated_at):
        worker = self.worker()
        client, req = self.submit(worker)
        self.store.data[f"ci/jobs/{req['job_id']}/status.json"] = encode({
            'ci_version': 1, 'job_id': req['job_id'], 'worker_id': req['worker_id'],
            'request_sha256': digest(encode(req)), 'state': 'RUNNING', 'stage': 'quiet-build',
            'updated_at': updated_at})
        return worker, client, req

    def test_stale_status_is_advisory_and_custom_threshold_rechecks_freshness(self):
        _, client, req = self.live(iso(1000))
        with patch('gdi.scheduling.clock', return_value=1600):
            value = client.status(req['job_id'])
            self.assertEqual((value['state'], value['verified'], value['heartbeat']['state']), ('RUNNING', False, 'stale'))
            self.assertEqual(client.status(req['job_id'], heartbeat_timeout=601)['heartbeat']['state'], 'fresh')
        self.assertNotIn(f"ci/jobs/{req['job_id']}/cancel.json", self.store.data)
        self.store.data[f"ci/jobs/{req['job_id']}/status.json"] = self.store.data[
            f"ci/jobs/{req['job_id']}/status.json"].replace(iso(1000).encode(), iso(1600).encode())
        with patch('gdi.scheduling.clock', return_value=1600):
            self.assertEqual(client.status(req['job_id'])['heartbeat']['state'], 'fresh')

    def test_follow_shows_stale_and_missing_heartbeat_without_terminal_claim(self):
        _, client, req = self.live(iso(1000))
        out = io.StringIO()
        with patch('gdi.scheduling.clock', return_value=1600):
            value, code = client.wait(req['job_id'], follow=True, stream=out, timeout=.001)
        self.assertEqual(code, 124)
        self.assertIn('heartbeat_status=STALE age=600.0s threshold=600s', out.getvalue())
        self.assertFalse(value['verified'])
        del self.store.data[f"ci/jobs/{req['job_id']}/status.json"]
        value = client.status(req['job_id'])
        self.assertEqual((value['state'], value['heartbeat']['state']), ('QUEUED', 'missing'))

    def test_follow_reports_transition_to_stale_when_timestamp_does_not_change(self):
        _, client, req = self.live(iso(1000))
        out = io.StringIO()
        with patch('gdi.scheduling.clock', side_effect=[1000, 1600]), \
                patch('gdi.ci.time.sleep', side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                client.wait(req['job_id'], follow=True, stream=out, timeout=None)
        self.assertIn('heartbeat_status=FRESH', out.getvalue())
        self.assertIn('heartbeat_status=STALE age=600.0s', out.getvalue())

    def test_invalid_clock_and_cancelled_pending_keep_explicit_diagnostics(self):
        _, client, req = self.live('invalid')
        self.assertEqual(client.status(req['job_id'])['heartbeat']['state'], 'invalid')
        with patch('gdi.scheduling.clock', return_value=1000):
            client.cancel(req['job_id'])
            value = client.status(req['job_id'])
        self.assertEqual((value['state'], value['heartbeat']['state']), ('CANCEL_REQUESTED', 'invalid'))

    def test_verified_result_is_not_mislabeled_by_old_advisory_heartbeat(self):
        worker, client, req = self.live(iso(1000))
        worker.tick()
        with patch('gdi.scheduling.clock', return_value=9999999999):
            value = client.status(req['job_id'])
        self.assertEqual((value['state'], value['verified']), ('PASS', True))
        self.assertNotIn('heartbeat', value)

    def test_cli_status_passes_threshold_and_rejects_invalid_limit(self):
        _, client, req = self.live(iso(1000))
        args = parser().parse_args(['ci', 'status', 'drive', req['job_id'], '--heartbeat-timeout', '30', '--json'])
        with patch('gdi.cli.Git.discover', return_value=self.a), patch('sys.stdout', new_callable=io.StringIO) as out:
            with patch('gdi.scheduling.clock', return_value=1030):
                self.assertEqual(execute(args, transport_factory=lambda url: self.store), 0)
        self.assertEqual(decode(out.getvalue().encode())['heartbeat']['state'], 'stale')
        with self.assertRaises(GdiError):
            client.status(req['job_id'], heartbeat_timeout=0)


class WorkerHeartbeatTests(InboxTestCase):
    def registry(self):
        self.worker()
        status = decode(self.store.data['ci/workers/user-host/status.json'])
        status['updated_at'] = iso(1000)
        caps = decode(self.store.data['ci/workers/user-host/capabilities.json'])
        return {'user-host': {'capabilities': caps, 'status': status}}

    def inspect(self, registry, settings=None, at=1600):
        return inspect_workers(registry, 'full', policy(settings), at=at)[0]

    def test_staleness_is_visible_even_when_capabilities_are_incompatible(self):
        registry = self.registry()
        row = self.inspect(registry)
        self.assertEqual((row['reason_code'], row['heartbeat']['state'], row['heartbeat_age_seconds']),
                         ('heartbeat_stale', 'stale', 600))
        registry['user-host']['capabilities']['profiles'] = {'other': 'f' * 64}
        row = self.inspect(registry)
        self.assertEqual((row['reason_code'], row['available_profiles'], row['heartbeat']['state']),
                         ('profile_missing', ['other'], 'stale'))
        registry['user-host']['capabilities'] = None
        self.assertEqual(self.inspect(registry)['reason_code'], 'capabilities_missing')

    def test_revision_labels_platforms_report_the_concrete_difference(self):
        registry = self.registry()
        status = registry['user-host']['status']
        original = status['profile_revisions']
        status['profile_revisions'] = {'full': 'f' * 64}
        row = self.inspect(registry, at=1000)
        self.assertEqual(row['reason_code'], 'revision_mismatch')
        self.assertEqual(row['heartbeat_revisions'], {'full': 'f' * 64})
        self.assertEqual(row['advertised_revisions'], original)
        status['profile_revisions'] = original
        row = self.inspect(registry, {'required_labels': ['board', 'linux']}, at=1000)
        self.assertEqual((row['reason_code'], row['missing_labels']), ('labels_missing', ['board', 'linux']))
        row = self.inspect(registry, {'required_platforms': ['windows-latest']}, at=1000)
        self.assertEqual((row['reason_code'], row['missing_platforms']), ('platforms_missing', ['windows-latest']))

    def test_missing_legacy_status_and_future_clock_are_not_available(self):
        registry = self.registry()
        status = registry['user-host']['status']
        registry['user-host']['status'] = None
        row = self.inspect(registry)
        self.assertEqual((row['reason_code'], row['heartbeat']['state']), ('registry_status_missing', 'missing'))
        registry['user-host']['status'] = status
        registry['user-host']['status']['updated_at'] = iso(1700)
        self.assertEqual(self.inspect(registry)['reason_code'], 'heartbeat_clock_skew')


class AgentHeartbeatTests(InboxTestCase):
    snapshot = test_agent_ci.AgentCiTests.snapshot
    capabilities = test_agent_ci.AgentCiTests.capabilities
    prepare = test_agent_ci.AgentCiTests.prepare
    transfer = test_agent_ci.AgentCiTests.transfer

    def test_pending_connector_result_reports_stale_heartbeat(self):
        value = self.prepare(self.worker())
        self.transfer(value, 'request', 'ready')
        req = decode((self.plan / 'request.json').read_bytes())
        self.store.data[f"repos/a/ci/jobs/{req['job_id']}/status.json"] = encode({
            'job_id': req['job_id'], 'worker_id': req['worker_id'], 'request_sha256': digest(encode(req)),
            'state': 'RUNNING', 'updated_at': iso(1000)})
        snapshot = self.snapshot('pending', req['job_id'])
        with patch('gdi.scheduling.clock', return_value=1030):
            result = agent_ci.result(self.a, self.plan, snapshot, self.identity, heartbeat_timeout=30)
        self.assertEqual((result['state'], result['verified'], result['heartbeat']['state']), ('PENDING', False, 'stale'))
        # A listing can contain status without its optional download. Keep the
        # old result command usable, but never call that heartbeat fresh/missing.
        document = decode(snapshot.read_bytes())
        document['files'] = [item for item in document['files']
                             if not item['local_path'].endswith('/status.json')]
        snapshot.write_bytes(encode(document))
        result = agent_ci.result(self.a, self.plan, snapshot, self.identity)
        self.assertEqual((result['state'], result['heartbeat']['state'], result['heartbeat']['reason_code']),
                         ('PENDING', 'unavailable', 'status_not_downloaded'))
