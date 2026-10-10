import json
import os
from unittest.mock import patch

from gdi.worker_config import workflow_config
from gdi.workflow import prepare
from tests.test_exchange import ExchangeTestCase


class WorkflowProxyTests(ExchangeTestCase):
    def setUp(self):
        super().setUp()
        workflow = self.a.path / '.github/workflows/ci.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('on: push\njobs: {}\n')
        self.a.call('add', '.github')
        self.head = self.commit(self.a, 'workflow')
        self.profile = {'workflow': workflow_config({'path': '.github/workflows/ci.yml'}),
                        'env': {}, 'timeout_seconds': 10}
        self.spool = self.root / 'spool'
        self.spool.mkdir()
        self.request = {'head': self.head, 'ref': 'refs/heads/main'}

    def prepared(self, environment):
        with patch.dict(os.environ, environment, clear=True):
            return prepare(self.a, self.profile, self.spool, self.request)

    def test_proxies_reach_actions_without_credentials_in_argv_or_profile(self):
        proxy = 'http://owner:password@127.0.0.1:10809'
        result = self.prepared({'HTTPS_PROXY': proxy, 'http_proxy': proxy,
                                'ALL_PROXY': 'socks5://127.0.0.1:10808',
                                'NO_PROXY': 'internal.example', 'UNRELATED_SECRET': 'secret'})
        argv = result['stages'][0]['argv']
        proxy_file = self.spool / 'worker-proxy-env.yaml'
        env = json.loads(proxy_file.read_bytes())
        self.assertEqual(env['HTTPS_PROXY'], proxy)
        self.assertEqual(env['http_proxy'], proxy)
        self.assertEqual(env['ALL_PROXY'], 'socks5://127.0.0.1:10808')
        self.assertNotIn('UNRELATED_SECRET', env)
        self.assertEqual(proxy_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(argv[argv.index('--env-file') + 1], str(proxy_file))
        self.assertNotIn(proxy, repr(result))
        self.assertNotIn('password', repr(result))
        for name in ('NO_PROXY', 'no_proxy'):
            self.assertIn('internal.example', env[name].split(','))
            self.assertIn('127.0.0.1', env[name].split(','))
            self.assertIn('localhost', env[name].split(','))

    def test_absent_proxy_does_not_create_proxy_or_bypass_variables(self):
        result = self.prepared({})
        argv = result['stages'][0]['argv']
        self.assertNotIn('HTTP_PROXY', argv)
        self.assertNotIn('NO_PROXY', argv)
        self.assertNotIn('NO_PROXY', result['process_env'])
        self.assertEqual(json.loads((self.spool / 'worker-proxy-env.yaml').read_bytes()), {})

    def test_explicit_legacy_profile_env_takes_precedence(self):
        self.profile['env'] = {'HTTPS_PROXY': 'http://profile:3128', 'NO_PROXY': 'custom.example'}
        result = self.prepared({'HTTPS_PROXY': 'http://host:8080', 'https_proxy': 'http://host:8080',
                                'NO_PROXY': 'host.example', 'no_proxy': 'host.example'})
        argv = result['stages'][0]['argv']
        self.assertIn('HTTPS_PROXY=http://profile:3128', argv)
        self.assertNotIn('HTTPS_PROXY', argv)
        self.assertIn('NO_PROXY=custom.example', argv)
        self.assertNotIn('NO_PROXY', result['process_env'])
        env = json.loads((self.spool / 'worker-proxy-env.yaml').read_bytes())
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertNotIn('https_proxy', env)
        self.assertNotIn('NO_PROXY', env)
        self.assertNotIn('no_proxy', env)
