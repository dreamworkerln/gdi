"""Single-host persistent worker with durable spool and independent log publication."""

import fcntl
import logging
import hashlib
import re
import os
from pathlib import Path
import shutil
import signal
import threading
import time
import uuid

from .ci import files, publication
from .ci_protocol import (atomic_write, descriptor, marker, now, request, upload_json,
                          validate_result, verify_file)
from .exchange import Exchange, decode, digest, encode, validate_repository
from .executor import execute, inside, kill_owned
from .git import GdiError, Git
from .ledger import Ledger
from .transport import Rclone
from .workflow import prepare as prepare_workflow, require_completed_job, collect_uploads

LOG = logging.getLogger("gdi.worker")
CHUNK_BYTES = 256 * 1024


class Worker:
    def __init__(self, config, transport_factory=Rclone):
        self.config = config
        self.worker_id = config['worker_id']
        self.root = Path(config['state_dir']) / self.worker_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = (self.root / 'worker.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            raise GdiError("this worker is already running") from exc
        self.ledger = Ledger(self.root / 'ledger.sqlite3')
        self.shared = config['config_version'] == 2
        self.repositories = {repo['repository_id']: repo for repo in config.get('repositories', [])}
        self.transport_factory = (lambda url: Rclone(url, options=config['transport'])) if self.shared and transport_factory is Rclone else transport_factory
        self.stop = threading.Event()

    def close(self):
        self.ledger.close()
        self.lock_file.close()

    def transport(self, repo):
        transport = self.transport_factory(repo['remote_url'])
        remote = validate_repository(decode(transport.read('repository.json')))
        if remote['repository_id'] != repo['repository_id']:
            raise GdiError("worker repository identity mismatch")
        return transport

    def advertise(self):
        if self.shared:
            transport = self.transport_factory(self.config['remote_url'])
            for path in ('inbox', f'ci/workers/{self.worker_id}'):
                transport.mkdir(path)
            capabilities = {'ci_version': 1, 'inbox_version': 1, 'worker_id': self.worker_id,
                            'profiles': {'full': self.config['execution_profile']['revision']}}
            upload_json(transport, f'ci/workers/{self.worker_id}/capabilities.json', capabilities, mutable=True)
            upload_json(transport, f'ci/workers/{self.worker_id}/status.json',
                        {'ci_version': 1, 'worker_id': self.worker_id, 'state': 'READY', 'updated_at': now()}, mutable=True)
            return
        capabilities = {'ci_version': 1, 'worker_id': self.worker_id,
                        'repositories': {repo['repository_id']: {name: profile['revision'] for name, profile in repo['profiles'].items()}
                                         for repo in self.config['repositories']}}
        for repo in self.config['repositories']:
            transport = self.transport(repo)
            for path in ('ci/queue', 'ci/jobs', f'ci/workers/{self.worker_id}'):
                transport.mkdir(path)
            upload_json(transport, f'ci/workers/{self.worker_id}/capabilities.json', capabilities, mutable=True)
            upload_json(transport, f'ci/workers/{self.worker_id}/status.json',
                        {'ci_version': 1, 'worker_id': self.worker_id, 'state': 'READY', 'updated_at': now()}, mutable=True)

    def discover(self):
        if self.shared:
            return self.discover_inbox()
        for repo in self.config['repositories']:
            transport = self.transport(repo)
            # Queue absence after setup is an error, not a swallowed authentication failure.
            for entry in sorted(transport.list('ci/queue'), key=lambda e: e['Path']):
                if self.stop.is_set():
                    return
                try:
                    if entry['IsDir'] or not re.fullmatch(r'[0-9a-f]{32}\.json', entry['Path']):
                        raise GdiError('invalid CI queue path')
                    jid = entry['Path'][:-5]
                    prefix = f'ci/jobs/{jid}'
                    if 'request.ready' not in files(transport, prefix):
                        LOG.warning('job=%s incomplete submission; waiting for request.ready', jid)
                        continue
                    raw = transport.read(prefix + '/request.json')
                    scratch = Path(self.config['cache_dir'])
                    scratch.mkdir(parents=True, exist_ok=True)
                    req = request(decode(raw), Git(scratch, isolated=True), repo['repository_id'])
                    expected = marker(req, raw)
                    if req['job_id'] != jid or decode(transport.read('ci/queue/' + entry['Path'])) != expected or decode(transport.read(prefix + '/request.ready')) != expected:
                        raise GdiError('CI queue/ready/request identity mismatch')
                    if req['worker_id'] != self.worker_id:
                        continue
                    row = self.ledger.discover(req, raw, uuid.uuid4().hex)
                    if row['state'] == 'PUBLISHED':
                        self.verify_remote(transport, req, row)
                        transport.delete_queue(f'ci/queue/{jid}.json')
                except (GdiError, OSError) as exc:
                    LOG.error('queue=%s: %s', entry['Path'], exc)

    def discover_inbox(self):
        from .inbox import NAME, read
        root = self.transport_factory(self.config['remote_url'])
        for entry in sorted(root.list('inbox'), key=lambda item: item['Path']):
            if self.stop.is_set():
                return
            name = entry['Path']
            try:
                if entry['IsDir'] or not NAME.fullmatch(name):
                    raise GdiError('invalid inbox entry')
                event = read(root, name)
                if event['type'] == 'ci_requested' and event['worker_id'] != self.worker_id:
                    continue
                repo = self.routed_repository(event['repository_id'], event['repository_path'])
                transport = self.transport(repo)
                scratch = Path(self.config['cache_dir'])
                scratch.mkdir(parents=True, exist_ok=True)
                git = Git(scratch, isolated=True)
                git.ref(event['ref'][11:])
                if event['type'] == 'repository_updated':
                    receipt = self.root / 'notifications' / name
                    if not receipt.exists():
                        pub, _ = publication(Exchange(git, self.transport_factory), transport,
                                             event['repository_id'], event['publication_id'])
                        if (pub['head'], pub['ref']) != (event['head'], event['ref']):
                            raise GdiError('inbox publication identity mismatch')
                        atomic_write(receipt, encode(event))
                    elif receipt.read_bytes() != encode(event):
                        raise GdiError('durable inbox receipt differs')
                    root.delete_notification('inbox/' + name)
                    continue
                prefix = 'ci/jobs/' + event['job_id']
                raw = transport.read(prefix + '/request.json')
                req = request(decode(raw), git, event['repository_id'])
                expected = marker(req, raw)
                if (req['ci_version'] != 2 or event['request_sha256'] != digest(raw)
                        or any(event[key] != req[key] for key in ('job_id', 'worker_id', 'ref', 'head', 'publication_id'))
                        or decode(transport.read(prefix + '/request.ready')) != expected):
                    raise GdiError('inbox/request/ready identity mismatch')
                route = {'root_url': self.config['remote_url'], 'repository_path': event['repository_path']}
                row = self.ledger.discover(req, raw, uuid.uuid4().hex, route=route, notification=name)
                if row['state'] == 'PUBLISHED':
                    self.verify_remote(transport, req, row)
                    self.acknowledge(row)
            except (GdiError, OSError) as exc:
                LOG.error('inbox=%s: %s; notification retained', name, exc)

    def routed_repository(self, repository_id, path):
        from .inbox import relative_repository
        relative_repository(path)
        return {'repository_id': repository_id, 'remote_url': self.config['remote_url'] + '/' + path,
                'profiles': {'full': self.config['execution_profile']}}

    def acknowledge(self, row):
        if row['route'] is not None:
            self.transport_factory(decode(row['route'])['root_url']).delete_notification('inbox/' + row['notification'])
        else:
            repo = self.repositories[row['repository_id']]
            self.transport(repo).delete_queue('ci/queue/' + row['job_id'] + '.json')

    def spool(self, jid):
        path = self.root / 'jobs' / jid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def status(self, row, state, stage=None):
        spool = self.spool(row['job_id'])
        status_path = spool / 'status.json'
        old = decode(status_path.read_bytes()) if status_path.exists() else {}
        value = {'ci_version': 1, 'job_id': row['job_id'], 'run_id': row['run_id'],
                 'worker_id': self.worker_id, 'request_sha256': digest(row['raw']), 'state': state,
                 'stage': stage, 'sequence': old.get('sequence', 0) + 1, 'updated_at': now()}
        atomic_write(status_path, encode(value))
        if (old.get('state'), old.get('stage')) != (state, stage):
            event = spool / 'events' / f"{value['sequence']:08d}.json"
            atomic_write(event, encode(value))
            LOG.info('job=%s state=%s stage=%s', row['job_id'], state, stage)

    def make_chunks(self, spool):
        directory = spool / 'log-chunks'
        directory.mkdir(exist_ok=True)
        chunks = sorted(directory.glob('*.bin'))
        for number, path in enumerate(chunks, 1):
            match = re.fullmatch(r'([0-9]{8})-([0-9a-f]{64})\.bin', path.name)
            if not match or int(match[1]) != number or digest(path.read_bytes()) != match[2]:
                raise GdiError('durable log chunk is corrupt; execution will not be repeated')
        offset = sum(path.stat().st_size for path in chunks)
        log = spool / 'build.log'
        if not log.exists():
            return chunks
        with log.open('rb') as handle:
            handle.seek(offset)
            while True:
                data = handle.read(CHUNK_BYTES)
                if not data:
                    break
                path = directory / f'{len(chunks) + 1:08d}-{digest(data)}.bin'
                atomic_write(path, data)
                chunks.append(path)
        return chunks

    def publish_live(self, transport, row, sent):
        spool = self.spool(row['job_id'])
        prefix = f"ci/jobs/{row['job_id']}"
        for directory in ('log-chunks', 'events'):
            transport.mkdir(prefix + '/' + directory)
        paths = self.make_chunks(spool)
        paths += sorted((spool / 'events').glob('*.json')) if (spool / 'events').exists() else []
        for path in paths:
            name = path.relative_to(spool).as_posix()
            if name not in sent:
                transport.upload(path, prefix + '/' + name)
                sent.add(name)
        if (spool / 'status.json').exists():
            transport.update_advisory(spool / 'status.json', prefix + '/status.json')
            status = decode((spool / 'status.json').read_bytes())
            target = self.transport_factory(self.config['remote_url']) if self.shared else transport
            upload_json(target, f'ci/workers/{self.worker_id}/status.json', status, mutable=True)

    def publisher(self, transport, row, done):
        sent = set()
        while not done.is_set():
            try:
                self.publish_live(transport, row, sent)
            except (GdiError, OSError) as exc:
                LOG.warning('job=%s live upload pending: %s', row['job_id'], exc)
            done.wait(1)

    def checkout(self, repo, transport, req):
        receiver = Path(self.config['cache_dir']) / self.worker_id / repo['repository_id'] / 'receiver'
        receiver.mkdir(parents=True, exist_ok=True)
        git = Git(receiver, isolated=True)
        if not (receiver / '.git').exists():
            git.call('init', '--quiet', '--object-format=sha1', '--template=')
        exchange = Exchange(git, self.transport_factory)
        with git.lock():
            pub, chain = publication(exchange, transport, repo['repository_id'], req['publication_id'])
            if pub['head'] != req['head'] or pub['ref'] != req['ref']:
                raise GdiError('CI request does not match Git publication')
            cache = exchange.restore(transport, repo['repository_id'], chain)
            if not cache.git.has_commit(req['head']) or not cache.git.ancestor(req['head'], chain[-1][1]['head']):
                raise GdiError('requested commit is not reachable from verified history')
            destination = self.spool(req['job_id']) / 'checkout'
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir()
            checkout = Git(destination, isolated=True)
            checkout.call('init', '--quiet', '--object-format=sha1', '--template=')
            checkout.import_objects(cache.path, req['head'])
            checkout.call('checkout', '--quiet', '--detach', req['head'])
            if checkout.oid('HEAD') != req['head']:
                raise GdiError('checkout HEAD mismatch')
            checkout.require_clean((None, req['head'], checkout.call('status', '--porcelain=v1', '--untracked-files=all').stdout))
            return checkout

    def finish(self, row, req, state, code, failed_stage, stages, warnings, started_at, duration, detail='', checkout=None, profile=None):
        spool = self.spool(req['job_id'])
        log = spool / 'build.log'
        if not log.exists():
            atomic_write(log, b'')
        if detail:
            with log.open('ab') as handle:
                handle.write(('\n[gdi] ' + detail + '\n').encode('utf-8', errors='replace'))
        with log.open('ab') as handle:
            handle.flush(); os.fsync(handle.fileno())
        artifacts = []
        if checkout is not None and profile is not None:
            for spec in profile['artifacts']:
                try:
                    source = inside(checkout.path, spec['path'])
                    if not source.is_file():
                        if spec['required']:
                            raise GdiError('required artifact missing: ' + spec['path'])
                        continue
                    destination = spool / 'artifacts' / spec['name']
                    destination.parent.mkdir(exist_ok=True)
                    with source.open('rb') as src, destination.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush(); os.fsync(dst.fileno())
                    artifacts.append(descriptor(destination, 'artifacts/' + spec['name']))
                except (OSError, GdiError) as exc:
                    state, code, detail = 'ERROR', 1, str(exc)
        if profile is not None and 'workflow' in profile:
            try:
                archive = collect_uploads(spool)
                if archive is not None:
                    artifacts.append(descriptor(archive, 'artifacts/workflow-artifacts.zip'))
            except (GdiError, OSError, ValueError) as exc:
                state, code, detail = 'ERROR', 1, str(exc)
        self.status(row, state, failed_stage)
        atomic_write(spool / 'final-status.json', (spool / 'status.json').read_bytes())
        artifacts.extend([descriptor(log, 'build.log'), descriptor(spool / 'final-status.json', 'final-status.json')])
        identity = {key: req[key] for key in ('ci_version', 'job_id', 'repository_id', 'ref', 'head', 'publication_id', 'worker_id', 'profile_id', 'profile_revision')}
        result = {**identity, 'request_sha256': digest(row['raw']), 'run_id': row['run_id'], 'state': state,
                  'exit_code': code, 'failed_stage': failed_stage, 'stages': stages, 'warnings': warnings,
                  'started_at': started_at, 'finished_at': now(), 'duration_seconds': duration,
                  'artifacts': artifacts, 'detail': detail}
        validate_result(result, req, digest(row['raw']))
        atomic_write(spool / 'result.json', encode(result))
        self.ledger.update(req['job_id'], 'RESULT_READY')

    def verify_remote(self, transport, req, row):
        spool = self.spool(req['job_id'])
        result = validate_result(decode(transport.read(f"ci/jobs/{req['job_id']}/result.json")), req, digest(row['raw']))
        if result['run_id'] != row['run_id'] or (spool / 'result.json').read_bytes() != encode(result):
            raise GdiError('remote result differs from durable local result')
        import tempfile
        with tempfile.TemporaryDirectory(prefix='gdi-verify-result-') as directory:
            for spec in result['artifacts']:
                target = Path(directory) / 'artifact'
                transport.download(f"ci/jobs/{req['job_id']}/" + spec['path'], target)
                verify_file(target, spec)
        return result

    def deliver(self, repo, row, req):
        transport = self.transport(repo)
        spool = self.spool(req['job_id'])
        prefix = f"ci/jobs/{req['job_id']}"
        result = validate_result(decode((spool / 'result.json').read_bytes()), req, digest(row['raw']))
        self.ledger.update(req['job_id'], 'UPLOAD_PENDING')
        self.publish_live(transport, row, set())
        for spec in result['artifacts']:
            verify_file(spool / spec['path'], spec)
            transport.upload(spool / spec['path'], prefix + '/' + spec['path'])
        checksum, size = hashlib.sha256(), 0
        for path in self.make_chunks(spool):
            data = path.read_bytes()
            checksum.update(data); size += len(data)
        log = next(spec for spec in result['artifacts'] if spec['path'] == 'build.log')
        if checksum.hexdigest() != log['sha256'] or size != log['bytes']:
            raise GdiError('durable log chunks do not match the finalized log')
        transport.upload(spool / 'result.json', prefix + '/result.json')
        self.verify_remote(transport, req, row)
        self.ledger.update(req['job_id'], 'PUBLISHED')
        self.acknowledge(row)
        LOG.info('job=%s result=%s published', req['job_id'], result['state'])

    def process(self, row):
        req = request(decode(row['raw']), Git(self.root, isolated=True), row['repository_id'])
        if row['route'] is not None:
            route = decode(row['route'])
            if not self.shared or route['root_url'] != self.config['remote_url']:
                raise GdiError('restore the original inbox root to deliver pending jobs')
            repo = self.routed_repository(req['repository_id'], route['repository_path'])
        else:
            repo = self.repositories.get(req['repository_id'])
        if repo is None:
            raise GdiError('pending repository was removed from config; restore registration to deliver result')
        transport = self.transport(repo)
        spool = self.spool(req['job_id'])
        if (spool / 'result.json').exists():
            self.deliver(repo, row, req)
            return
        if row['state'] in ('RUNNING', 'FINALIZING'):
            # Execution may have finished just before the crash; never rerun blindly.
            kill_owned(decode(row['process']) if row['process'] else None)
            self.finish(row, req, 'INTERRUPTED', 1, None, [], [], now(), 0,
                        'worker restarted with uncertain execution; explicit ci retry is required')
            self.deliver(repo, row, req)
            return
        claim = {'ci_version': 1, 'job_id': req['job_id'], 'run_id': row['run_id'],
                 'worker_id': self.worker_id, 'request_sha256': digest(row['raw'])}
        if 'worker.running.json' in files(transport, f"ci/jobs/{req['job_id']}"):
            if decode(transport.read(f"ci/jobs/{req['job_id']}/worker.running.json")) != claim:
                raise GdiError('foreign/stale claim: keep original ledger; automatic takeover is disabled')
        self.ledger.update(req['job_id'], 'CLAIMED')
        upload_json(transport, f"ci/jobs/{req['job_id']}/worker.running.json", claim)
        profile = repo['profiles'].get(req['profile_id'])
        if profile is None or profile['revision'] != req['profile_revision']:
            self.finish(row, req, 'REJECTED', 1, None, [], [], now(), 0, 'profile absent or revision changed; resubmit using current capabilities')
            self.deliver(repo, row, req)
            return
        started_at, started = now(), time.monotonic()
        done = threading.Event()
        publisher = threading.Thread(target=self.publisher, args=(transport, row, done), daemon=True)
        publisher.start()
        checkout, state, code, failed, stages, warnings, detail = None, 'ERROR', 1, None, [], [], ''
        try:
            self.status(row, 'RESTORING')
            self.ledger.update(req['job_id'], 'RESTORING')
            checkout = self.checkout(repo, transport, req)
            self.status(row, 'CHECKOUT')
            if 'workflow' in profile:
                if req['ci_version'] == 2:
                    profile = {**profile, 'workflow': {**profile['workflow'], **req['workflow']}}
                profile = prepare_workflow(checkout, profile, spool, req, cache_dir=Path(self.config['cache_dir']) / self.worker_id / 'act')
            self.ledger.update(req['job_id'], 'RUNNING')
            state, code, failed, stages, warnings = execute(checkout, profile, spool / 'build.log',
                lambda state, stage: self.status(row, state, stage),
                lambda identity: self.ledger.update(req['job_id'], 'RUNNING', identity))
            if state == 'PASS' and 'workflow' in profile:
                require_completed_job(spool / 'build.log')
            if checkout.oid('HEAD') != req['head']:
                raise GdiError('CI changed requested HEAD')
        except KeyboardInterrupt:
            state, code, detail = 'INTERRUPTED', 130, 'worker interrupted during execution'
            self.stop.set()
        except (GdiError, OSError, ValueError) as exc:
            state, code, detail = 'ERROR', 1, str(exc)
            LOG.exception('job=%s execution error', req['job_id'])
        finally:
            done.set()
            publisher.join()
        self.ledger.update(req['job_id'], 'FINALIZING')
        self.finish(row, req, state, code, failed, stages, warnings, started_at, time.monotonic() - started,
                    detail, checkout, profile)
        self.deliver(repo, row, req)

    def tick(self):
        self.discover()
        pending = self.ledger.pending()
        for row in pending:
            if self.stop.is_set():
                break
            try:
                self.process(row)
            except (GdiError, OSError) as exc:
                LOG.error('job=%s pending state=%s: %s', row['job_id'], self.ledger.get(row['job_id'])['state'], exc)
        return bool(pending)

    def run(self, *, once=False):
        delay = self.config['poll_active_seconds']
        advertised = False
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda signum, frame: self.stop.set())
        try:
            while not self.stop.is_set():
                try:
                    if not advertised:
                        self.advertise(); advertised = True
                    active = self.tick()
                    delay = self.config['poll_active_seconds'] if active else min(delay * 1.5, self.config['poll_idle_max_seconds'])
                except (GdiError, OSError) as exc:
                    LOG.error('worker transport/config error: %s', exc)
                    delay = min(delay * 2, self.config['poll_idle_max_seconds'])
                if once:
                    return
                self.stop.wait(delay)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
