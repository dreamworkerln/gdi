from unittest.mock import patch
from gdi.network import NetworkRoutes

n = NetworkRoutes(state_path='/tmp/gdi-iar-no-route-state')
n.selected = {
    'http_proxy': 'http://old-proxy:8080',
    'https_proxy': 'http://old-proxy:8080',
    'no_proxy': 'old.internal',
}
n.discovered = []
n.discovered_at = 10**20

with patch.dict('os.environ', {
    'http_proxy': 'http://new-proxy:8080',
    'https_proxy': 'http://new-proxy:8080',
    'NO_PROXY': 'new.internal',
}, clear=True):
    routes = n.routes()
    print('first_route=', routes[0])
    used = n.run(lambda route: route)
    print('successful_route=', used)
    print('process_environment_no_proxy=', n.environment(used).get('no_proxy'))
