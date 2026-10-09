"""Opt-in Drive acceptance benchmark in a uniquely owned, disposable remote root.

Run from the source tree: python3 -m tests.rc_drive_check --remote gdrive:
Never uses the current Git repository or its configured GDI connections.
"""

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

from gdi.diagnostics import command_session
from gdi.exchange import Exchange, decode, digest, encode
from gdi.git import GdiError, Git, run
from gdi.status import inspect
from gdi.transport import Rclone
from tests.rc_transport import RcServer


def repository(path):
    path.mkdir()
    git = Git(path)
    git.call('init', '-q', '-b', 'main')
    return git


def commit(git, message):
    (git.path / 'file.txt').write_text(message + '\n')
    git.call('add', 'file.txt')
    git.call('-c', 'user.name=Gdi Check', '-c', 'user.email=gdi@example.invalid',
             'commit', '-qm', message)
    return git.oid('HEAD')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote', required=True, help='configured rclone remote name, e.g. gdrive:')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*:', args.remote):
        parser.error('--remote must be only a configured remote name ending in colon')
    token = uuid.uuid4().hex
    owned_root = args.remote + 'gdi-rc-check-' + token
    evidence = Path(tempfile.mkdtemp(prefix='gdi-rc-evidence-'))
    result = {'owned_root': owned_root, 'evidence': str(evidence), 'measurements': [],
              'checks': [], 'limitations': []}
    print(json.dumps({'evidence': str(evidence), 'owned_root': owned_root}), flush=True)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        result['checks'].append(name)
        print('PASS ' + name, file=sys.stderr, flush=True)

    def measure(label, mode, function):
        print('RUN ' + label, file=sys.stderr, flush=True)
        started = time.monotonic()
        with command_session(label, path=evidence / 'profile.jsonl', progress=False) as profile:
            with (RcServer(trace_path=evidence / (label.replace(' ', '-') + '.jsonl'))
                  if mode == 'rc' else nullcontext()) as server:
                try:
                    value = function(server.transport if server else Rclone, server)
                finally:
                    if server:
                        # close first, so the trace includes process termination.
                        server.close()
                        (evidence / (label.replace(' ', '-') + '.json')).write_text(
                            json.dumps(server.events, indent=2), encoding='utf-8')
            calls = len([event for event in server.events if event['event'] == 'rc_start']) if server else 0
            processes = profile.counts.get('rclone', 0)
        duration = time.monotonic() - started
        record = {'label': label, 'seconds': round(duration, 3),
                  'rclone_processes': processes, 'rc_calls': calls}
        result['measurements'].append(record)
        print(json.dumps(record), flush=True)
        return value

    try:
        git = repository(evidence / 'source')
        first = commit(git, 'first')
        ids = {}
        for mode in ('cli', 'rc'):
            ids[mode] = measure('init ' + mode, mode,
                lambda factory, server, mode=mode: Exchange(git, factory).add(
                    mode, owned_root + '/' + mode, initialize=True))
        for mode in ('cli', 'rc'):
            pushed = measure('full push ' + mode, mode,
                lambda factory, server, mode=mode: Exchange(git, factory).push(mode))
            check('full ' + mode, pushed[0] == first and pushed[2])
        second = commit(git, 'second')
        for mode in ('cli', 'rc'):
            pushed = measure('incremental push ' + mode, mode,
                lambda factory, server, mode=mode: Exchange(git, factory).push(mode))
            check('incremental ' + mode, pushed[0] == second and pushed[2])
            pushed = measure('unchanged push ' + mode, mode,
                lambda factory, server, mode=mode: Exchange(git, factory).push(mode))
            check('unchanged ' + mode, pushed[0] == second and not pushed[2])
        for mode in ('cli', 'rc'):
            receiver = repository(evidence / ('receiver-' + mode))
            Exchange(receiver).add(mode, owned_root + '/' + mode, expected_id=ids[mode])
            measure('fetch ' + mode, mode,
                lambda factory, server, mode=mode: Exchange(receiver, factory).fetch(mode))
            check('fetch leaves HEAD unborn ' + mode, receiver.oid('HEAD') is None)
            measure('cached pull ' + mode, mode,
                lambda factory, server, mode=mode: Exchange(receiver, factory).pull(mode))
            check('pull exact HEAD and bytes ' + mode,
                  receiver.oid('HEAD') == second and (receiver.path / 'file.txt').read_text() == 'second\n')
            status = measure('status ' + mode, mode,
                lambda factory, server, mode=mode: inspect(Exchange(receiver, factory), mode))
            check('status published ' + mode, status['connections'][0]['state'] == 'published')
            history = measure('log ' + mode, mode,
                lambda factory, server, mode=mode: Exchange(receiver, factory).log(mode))
            check('log full incremental order ' + mode,
                  [item['bundle_kind'] for item in history['publications']] == ['incremental', 'full'])

        # Only metadata is needed to exercise listing, batches and chain verification.
        staging = evidence / 'metadata'
        branch = 'history/ветка%25'
        previous = None
        for index in range(64):
            raw = encode({'version': 3, 'repository_id': ids['rc'], 'ref': 'refs/heads/' + branch,
                          'head': second, 'bundle_sha256': '1' * 64, 'previous': previous,
                          'nonce': f'{index:032x}', 'bundle_bytes': 100, 'bundle_kind': 'full',
                          'base_publication': None, 'base_head': None, 'prerequisites': []})
            relative = 'branches/history%2Fветка%2525/' + digest(raw) + '.json'
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            previous = digest(raw)
        measure('upload 64 manifests rc', 'rc', lambda factory, server:
            server.request('/sync/copy', {'srcFs': str(staging), 'dstFs': owned_root + '/rc',
                '_config': {'Immutable': True, 'CheckSum': True, 'NoTraverse': True}}))
        chains = []
        for mode in ('cli', 'rc'):
            def read_chain(factory, server):
                exchange = Exchange(git, factory)
                transport, identity = exchange.connect('rc')
                return exchange.publications(transport, identity, 'refs/heads/' + branch)
            chains.append(measure('read 64 manifests ' + mode, mode, read_chain))
        check('64 manifests exact chain match', chains[0] == chains[1] and len(chains[0]) == 64)

        def faults(factory, server):
            transport = factory(owned_root + '/rc')
            source = evidence / 'immutable'
            source.write_bytes(b'original')
            transport.upload(source, 'probe/immutable.json')
            transport.upload(source, 'probe/immutable.json')
            stamp = source.stat()
            source.write_bytes(b'changed!')
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            try:
                transport.upload(source, 'probe/immutable.json')
            except GdiError:
                pass
            else:
                raise AssertionError('immutable overwrite accepted')
            check('Drive immutable same size/mtime', transport.read('probe/immutable.json') == b'original')
            original = next(staging.rglob('*.json'))
            relative = str(original.relative_to(staging))
            raw = original.read_bytes()
            original.write_bytes(raw.replace(b'"bundle_bytes":100', b'"bundle_bytes":101'))
            # Intentional corruption, confined to our owned test root.
            server.request('/operations/copyfile', {'srcFs': str(original.parent),
                'srcRemote': original.name, 'dstFs': transport.url, 'dstRemote': relative,
                '_config': {'IgnoreTimes': True}})
            try:
                Exchange(git, factory).publications(transport, ids['rc'], 'refs/heads/' + branch)
            except GdiError as error:
                check('Drive fresh corruption detection', 'checksum' in str(error))
            else:
                raise AssertionError('cached corruption accepted')
        measure('Drive upload and corruption faults rc', 'rc', faults)

        def binding(factory, server):
            target = owned_root + '/binding'
            old = owned_root + '/binding-old'
            source = evidence / 'identity'
            source.write_bytes(b'{"identity":"old"}')
            factory(target).upload(source, 'repository.json')
            check('binding initial identity', factory(target).read('repository.json') == source.read_bytes())
            run(['rclone', 'moveto', target, old])
            source.write_bytes(b'{"identity":"new"}')
            Rclone(target).upload(source, 'repository.json')
            rebound = factory(target).read('repository.json')
            fresh = Rclone(target).read('repository.json')
            check('repository read resolves replaced Drive folder automatically', rebound == fresh)
        measure('Drive folder replacement rc', 'rc', binding)

        def replace_repository(target):
            run(['rclone', 'moveto', target, target + '-old'])
            replacement = {'version': 3, 'repository_id': uuid.uuid4().hex, 'object_format': 'sha1'}
            path = evidence / 'replacement-repository.json'
            path.write_bytes(encode(replacement))
            fresh = Rclone(target)
            fresh.mkdir('bundles')
            fresh.mkdir('branches')
            fresh.upload(path, 'repository.json')

        def identity_fault(factory, server, stage):
            name = 'replace-' + stage
            url = owned_root + '/' + name
            exchange = Exchange(git, factory)
            exchange.add(name, url, initialize=True)
            if stage == 'connect':
                exchange.connect(name)
                replace_repository(url)
                operation = lambda: exchange.connect(name)
                context = nullcontext()
            elif stage == 'before-upload':
                original = git.call

                def call(*arguments, **kwargs):
                    value = original(*arguments, **kwargs)
                    if arguments[:2] == ('bundle', 'create'):
                        replace_repository(url)
                    return value

                context = patch.object(git, 'call', side_effect=call)
                operation = lambda: exchange.push(name)
            else:
                transport = factory(url)
                original = transport.upload

                def upload(source, relative):
                    original(source, relative)
                    if relative.startswith('bundles/'):
                        replace_repository(url)

                # Inbox is never reached in this rejected push.
                context = patch.object(exchange, 'transport_factory', return_value=transport)
                context_upload = patch.object(transport, 'upload', side_effect=upload)
                operation = lambda: exchange.push(name)
            with context:
                with (context_upload if stage == 'during-upload' else nullcontext()):
                    try:
                        operation()
                    except GdiError as error:
                        check('Drive identity mismatch rejected at ' + stage, 'repository ID mismatch' in str(error))
                    else:
                        raise AssertionError('replaced Drive repository was accepted at ' + stage)
            if stage != 'connect':
                check('no manifest in replacement or old folder at ' + stage,
                      Rclone(url).list('branches', recursive=True) == [] and
                      Rclone(url + '-old').list('branches', recursive=True) == [])
                if stage == 'before-upload':
                    check('no bundle uploaded before identity recheck',
                          Rclone(url).list('bundles') == [] and Rclone(url + '-old').list('bundles') == [])

        for stage in ('connect', 'before-upload', 'during-upload'):
            measure('Drive identity replacement ' + stage + ' rc', 'rc',
                    lambda factory, server, stage=stage: identity_fault(factory, server, stage))
        result['result'] = 'PASS_WITH_LIMITATIONS' if result['limitations'] else 'PASS'
    except BaseException as error:
        result['result'] = 'FAIL'
        result['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        (evidence / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        # The root is generated here; no URL/path supplied by the caller is purged.
        assert owned_root == args.remote + 'gdi-rc-check-' + token
        print('CLEANUP ' + owned_root, file=sys.stderr, flush=True)
        try:
            run(['rclone', 'purge', owned_root])
            result['cleanup'] = 'removed owned test root'
        except GdiError as error:
            result['cleanup'] = str(error)
        (evidence / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({'result': result.get('result'), 'evidence': str(evidence),
                          'cleanup': result['cleanup']}), flush=True)


if __name__ == '__main__':
    main()
