"""Automatic routes remember success and do not retry protocol/auth failures."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from gdi.git import GdiError
from gdi.network import NetworkRoutes, file_settings
from gdi.rc_transport import RcError, RcServer
from gdi.transport import Rclone


PROXY = {'http_proxy': 'http://owner:password@127.0.0.1:10809',
         'https_proxy': 'http://owner:password@127.0.0.1:10809'}


class NetworkRoutesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'route.json'
        for context in (patch.dict(os.environ, {}, clear=True),
                        patch('gdi.network.discover_proxies', return_value=[PROXY]),
                        patch.object(NetworkRoutes, 'available', return_value=True)):
            context.start()
            self.addCleanup(context.stop)

    def routes(self):
        return NetworkRoutes(state_path=self.state)

    def test_discovered_proxy_is_tried_before_direct_without_worker_env(self):
        calls = []
        network = self.routes()
        network.run(lambda route: calls.append(route))
        self.assertEqual(calls, [PROXY])
        self.assertEqual(network.selected, PROXY)

    def test_successful_direct_route_is_first_in_next_process(self):
        calls = []
        def connect(route):
            calls.append(route)
            if route:
                raise GdiError('dial tcp: connection refused')
            return 'connected'
        self.assertEqual(self.routes().run(connect), 'connected')
        self.assertEqual(calls, [PROXY, {}])
        calls.clear()
        self.routes().run(lambda route: calls.append(route))
        self.assertEqual(calls, [{}])

    def test_direct_failure_switches_back_to_proxy_and_remembers_it(self):
        def direct_only(route):
            if route:
                raise GdiError('dial tcp: refused')
        self.routes().run(direct_only)
        calls = []
        def connect(route):
            calls.append(route)
            if not route:
                raise GdiError('dial tcp: no such host')
            return 'connected'
        self.assertEqual(self.routes().run(connect), 'connected')
        self.assertEqual(calls, [{}, PROXY])
        calls.clear()
        self.routes().run(lambda route: calls.append(route))
        self.assertEqual(calls, [PROXY])
        self.assertNotIn('password', self.state.read_text())
        self.assertNotIn('127.0.0.1', self.state.read_text())

    def test_unavailable_local_proxy_is_skipped_without_remote_attempt(self):
        calls = []
        with patch.object(NetworkRoutes, 'available', side_effect=lambda route: not route):
            self.routes().run(lambda route: calls.append(route))
        self.assertEqual(calls, [{}])

    def test_authentication_and_protocol_errors_do_not_switch(self):
        for message in ('invalid_grant', 'file not found', 'checksum mismatch'):
            calls = []
            def fail(route):
                calls.append(route)
                raise GdiError(message)
            with self.assertRaisesRegex(GdiError, message):
                self.routes().run(fail)
            self.assertEqual(calls, [PROXY])

    def test_all_routes_are_tried_once_and_failure_is_reported(self):
        calls = []
        def fail(route):
            calls.append(route)
            raise GdiError('dial tcp: connection refused')
        with self.assertRaisesRegex(GdiError, 'connection refused'):
            self.routes().run(fail)
        self.assertEqual(calls, [PROXY, {}])
        self.assertFalse(self.state.exists())

    def test_shell_discovery_reads_literals_without_executing_commands(self):
        path = self.root / '.bashrc'
        path.write_text('adb forward tcp:10809 tcp:10809\n'
                        'export HTTPS_PROXY="http://127.0.0.1:10809/"\n'
                        'export http_proxy="$(touch /should-not-exist)"\n'
                        'export ALL_PROXY="`malicious`"\n')
        self.assertEqual(file_settings(path), {'https_proxy': 'http://127.0.0.1:10809/'})

    def test_discovery_preserves_host_bypass_and_does_not_modify_host_env(self):
        with patch.dict(os.environ, {'NO_PROXY': 'internal.example'}, clear=True):
            network = self.routes()
            route = network.routes()[0]
            self.assertEqual(route['no_proxy'], 'internal.example')
            environment = network.environment(route)
            self.assertEqual(environment['NO_PROXY'], 'internal.example')
            self.assertEqual(environment['HTTPS_PROXY'], PROXY['https_proxy'])
            self.assertEqual(dict(os.environ), {'NO_PROXY': 'internal.example'})

    def test_cli_retries_with_clean_environment_then_remembers_direct(self):
        network = self.routes()
        result = subprocess.CompletedProcess(['rclone'], 0, '[]', '')
        with patch('gdi.transport.run', side_effect=[GdiError('dial tcp: refused'), result]) as run:
            self.assertEqual(Rclone('drive:project', network=network).list(''), [])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs['environment']['HTTPS_PROXY'], PROXY['https_proxy'])
        self.assertNotIn('HTTPS_PROXY', run.call_args_list[1].kwargs['environment'])
        self.assertEqual(network.selected, {})

    def test_rc_restarts_before_remote_retry_but_not_for_local_cache_call(self):
        network = self.routes()
        server = RcServer(network=network)
        server.network_route = PROXY
        server.process = Mock()
        with patch.object(server, 'close') as close, patch.object(server, '__enter__') as start, \
                patch.object(server, '_single_request', side_effect=[RcError(500, 'dial tcp: refused'), {}]):
            self.assertEqual(server.request('/operations/list', {'fs': 'drive:project'}), {})
        close.assert_called_once()
        start.assert_called_once()
        self.assertEqual(network.selected, {})
        with patch.object(network, 'run') as automatic, patch.object(server, '_single_request', return_value={}) as call:
            server.request('/fscache/clear')
        automatic.assert_not_called()
        call.assert_called_once()

    def test_async_transfer_failure_switches_whole_transfer_not_job_poll(self):
        network = self.routes()
        server = RcServer(network=network)
        server.network_route = PROXY
        server.process = Mock()
        with patch.object(server, 'close'), patch.object(server, '__enter__'), \
                patch.object(server, '_transfer', side_effect=[RcError(500, 'dial tcp: refused'), {}]) as transfer:
            self.assertEqual(server.transfer('/sync/copy', {'srcFs': '/tmp/data', 'dstFs': 'drive:project'}, 'upload'), {})
        self.assertEqual(transfer.call_count, 2)
        self.assertEqual(network.selected, {})

    def test_unstarted_rc_request_never_spawns_an_unowned_daemon(self):
        server = RcServer(network=self.routes())
        with patch.object(server, '__enter__') as start:
            with self.assertRaisesRegex(GdiError, 'RC process is not running'):
                server.request('/operations/list', {'fs': 'drive:project'})
        start.assert_not_called()
