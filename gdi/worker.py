"""Single-host persistent worker with durable spool and independent log publication."""

import fcntl
from contextlib import nullcontext
from functools import wraps
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

from .ci import files, publication, optional_read
from .ci_protocol import (atomic_write, descriptor, marker, now, request, upload_json,
                          validate_result, verify_file, validate_cancellation)
from .exchange import Exchange, decode, digest, encode, validate_repository
from .executor import execute, inside, kill_owned
from .git import GdiError, Git
from .ledger import Ledger
from .transport import Rclone
from .workflow import prepare as prepare_workflow, require_completed_job, collect_uploads
from . import runner

LOG = logging.getLogger("gdi.worker")
CHUNK_BYTES = 256 * 1024


class JobCancelled(Exception):
    pass


def transport_operation(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self.transport_session if self.transport_session is not None else nullcontext():
            return function(self, *args, **kwargs)
    return wrapped


class Worker:
    def __init__(self, config, transport_factory=Rclone):
        self.config = config
        from .rc_transport import TransportSession
        self.transport_session = TransportSession(options=config.get('transport')) if transport_factory is Rclone else None
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
        self.transport_factory = self.transport_session if self.transport_session is not None else transport_factory
        self.stop = threading.Event()

    def close(self):
        try:
            if self.transport_session is not None:
                self.transport_session.close()
        finally:
            self.ledger.close()
            self.lock_file.close()

    def transport(self, repo):
        transport = self.transport_factory(repo['remote_url'])
        remote = validate_repository(decode(transport.read('repository.json')))
        if remote['repository_id'] != repo['repository_id']:
            raise GdiError("worker repository identity mismatch")
        return transport

    @transport_operation
    def advertise(self):
        if self.shared:
            self.config['execution_profile'] = runner.resolve_profile(self.config['execution_profile'], self.root)
            transport = self.transport_factory(self.config['remote_url'])
            for path in ('inbox', f'ci/workers/{self.worker_id}'):
                transport.mkdir(path)
            capabilities = {'ci_version': 1, 'inbox_version': 1, 'cancel_version': 1, 'worker_id': self.worker_id,
                            'profiles': {'full': self.config['execution_profile']['revision']}}
            upload_json(transport, f'ci/workers/{self.worker_id}/capabilities.json', capabilities, mutable=True)
            upload_json(transport, f'ci/workers/{self.worker_id}/status.json',
                        {'ci_version': 1, 'worker_id': self.worker_id, 'state': 'READY', 'updated_at': now()}, mutable=True)
            return
        capabilities = {'ci_version': 1, 'cancel_version': 1, 'worker_id': self.worker_id,
                        'repositories': {repo['repository_id']: {name: profile['revision'] for name, profile in repo['profiles'].items()}
                                         for repo in self.config['repositories']}}
        for repo in self.config['repositories']:
            transport = self.transport(repo)
            for path in ('ci/queue', 'ci/jobs', f'ci/workers/{self.worker_id}'):
                transport.mkdir(path)
            upload_json(transport, f'ci/workers/{self.worker_id}/capabilities.json', capabilities, mutable=True)
            upload_json(transport, f'ci/workers/{self.worker_id}/status.json',
                        {'ci_version': 1, 'worker_id': self.worker_id, 'state': 'READY', 'updated_at': now()}, mutable=True)

    @transport_operation
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
                        if self.check_cancellation(transport, row, req):
                            self.process(row)
                        else:
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
                    if self.check_cancellation(transport, row, req):
                        self.process(row)
                    else:
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

    def publish_live(self, transport, row, sent, *, spool=None, prefix=None):
        spool = self.spool(row['job_id']) if spool is None else spool
        prefix = f"ci/jobs/{row['job_id']}" if prefix is None else prefix
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
            # Heartbeats replace this pathname while rclone is still reading/hash-
            # checking it. Upload one captured value from a private stable file;
            # use that same snapshot for the per-job and per-worker status.
            status = decode((spool / 'status.json').read_bytes())
            upload_json(transport, prefix + '/status.json', status, mutable=True)
            target = self.transport_factory(self.config['remote_url']) if self.shared else transport
            upload_json(target, f'ci/workers/{self.worker_id}/status.json', status, mutable=True)

    def check_cancellation(self, transport, row, req):
        path = self.spool(req['job_id']) / 'cancel.json'
        if path.exists():
            validate_cancellation(path.read_bytes(), req, row['raw'])
            return True
        content = optional_read(transport, f"ci/jobs/{req['job_id']}/cancel.json")
        if content is None:
            return False
        validate_cancellation(content, req, row['raw'])
        # Persist before stopping: a restart or a lost remote response cannot undo cancellation.
        atomic_write(path, content)
        return True

    def publisher(self, transport, row, done, cancelled):
        sent = set()
        req = decode(row['raw'])
        while not done.is_set():
            try:
                if self.check_cancellation(transport, row, req):
                    cancelled.set()
            except (GdiError, OSError) as exc:
                LOG.warning('job=%s cancellation check pending: %s', row['job_id'], exc)
            try:
                self.publish_live(transport, row, sent)
            except (GdiError, OSError) as exc:
                LOG.warning('job=%s live upload pending: %s', row['job_id'], exc)
            done.wait(self.config['poll_active_seconds'])

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
                raise GdiError('CI request does not match git publication')
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
            # act copies files rather than empty directories. Keep a real ref
            # so .git/refs survives the copy and git works inside the container.
            checkout.call('update-ref', req['ref'], req['head'])
            checkout.call('checkout', '--quiet', '--detach', req['head'])
            if checkout.oid('HEAD') != req['head']:
                raise GdiError('checkout HEAD mismatch')
            checkout.require_clean((None, req['head'], checkout.call('status', '--porcelain=v1', '--untracked-files=all').stdout))
            return checkout

    def finish(self, row, req, state, code, failed_stage, stages, warnings, started_at, duration, detail='', checkout=None, profile=None, *, delivery_spool=None):
        spool = self.spool(req['job_id']) if delivery_spool is None else delivery_spool
        log = spool / 'build.log'
        if not log.exists():
            atomic_write(log, b'')
        if detail:
            with log.open('ab') as handle:
                handle.write(('\n[gdi] ' + detail + '\n').encode('utf-8', errors='replace'))
        with log.open('ab') as handle:
            handle.flush(); os.fsync(handle.fileno())
        artifacts = []
        environment_path = spool / 'artifacts/environment.json'
        if environment_path.exists():
            artifacts.append(descriptor(environment_path, 'artifacts/environment.json'))
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
        status = (self.spool(req['job_id']) / 'status.json').read_bytes()
        if delivery_spool is not None:
            atomic_write(spool / 'status.json', status)
        atomic_write(spool / 'final-status.json', status)
        artifacts.extend([descriptor(log, 'build.log'), descriptor(spool / 'final-status.json', 'final-status.json')])
        identity = {key: req[key] for key in ('ci_version', 'job_id', 'repository_id', 'ref', 'head', 'publication_id', 'worker_id', 'profile_id', 'profile_revision')}
        result = {**identity, 'request_sha256': digest(row['raw']), 'run_id': row['run_id'], 'state': state,
                  'exit_code': code, 'failed_stage': failed_stage, 'stages': stages, 'warnings': warnings,
                  'started_at': started_at, 'finished_at': now(), 'duration_seconds': duration,
                  'artifacts': artifacts, 'detail': detail}
        validate_result(result, req, digest(row['raw']))
        atomic_write(spool / 'result.json', encode(result))
        self.ledger.update(req['job_id'], 'RESULT_READY')

    def verify_remote(self, transport, req, row, *, spool=None, prefix=None):
        spool = self.spool(req['job_id']) if spool is None else spool
        prefix = f"ci/jobs/{req['job_id']}" if prefix is None else prefix
        result = validate_result(decode(transport.read(prefix + '/result.json')), req, digest(row['raw']))
        if result['run_id'] != row['run_id'] or (spool / 'result.json').read_bytes() != encode(result):
            raise GdiError('remote result differs from durable local result')
        import tempfile
        with tempfile.TemporaryDirectory(prefix='gdi-verify-result-') as directory:
            for spec in result['artifacts']:
                target = Path(directory) / 'artifact'
                transport.download(prefix + '/' + spec['path'], target)
                verify_file(target, spec)
        return result

    def stop_cancelled(self, transport, row, req):
        claim = optional_read(transport, f"ci/jobs/{req['job_id']}/worker.running.json")
        if claim is not None and decode(claim) != {
                'ci_version': 1, 'job_id': req['job_id'], 'run_id': row['run_id'],
                'worker_id': self.worker_id, 'request_sha256': digest(row['raw'])}:
            raise GdiError('foreign/stale claim: cancellation must be confirmed by the original worker ledger')
        kill_owned(decode(row['process']) if row['process'] else None)
        runner.cleanup(self.spool(req['job_id']))

    def prepare_cancelled_delivery(self, transport, row, req):
        """Preserve immutable execution files; cancellation has its own final log/result."""
        self.stop_cancelled(transport, row, req)
        original = self.spool(req['job_id'])
        destination = original / 'cancelled'
        destination.mkdir(exist_ok=True)
        if not (destination / 'result.json').exists():
            if (original / 'build.log').exists():
                shutil.copyfile(original / 'build.log', destination / 'build.log')
            self.ledger.update(req['job_id'], 'FINALIZING')
            self.finish(row, req, 'CANCELLED', 130, None, [], [], now(), 0,
                        'durable cancellation suppresses the original result; owned execution stopped',
                        delivery_spool=destination)

    def deliver(self, repo, row, req, *, cancelled_result=False):
        transport = self.transport(repo)
        spool = self.spool(req['job_id'])
        prefix = f"ci/jobs/{req['job_id']}"
        if not cancelled_result and (spool / 'cancelled/result.json').exists():
            cancelled_result = True
        if cancelled_result:
            if not self.check_cancellation(transport, row, req):
                raise GdiError('cancelled delivery requires its durable cancellation marker')
            spool = spool / 'cancelled'
            prefix += '/cancelled'
        result = validate_result(decode((spool / 'result.json').read_bytes()), req, digest(row['raw']))
        if cancelled_result and result['state'] != 'CANCELLED':
            raise GdiError('invalid state in cancelled delivery')
        self.ledger.update(req['job_id'], 'UPLOAD_PENDING')
        def require_active():
            if result['state'] != 'CANCELLED' and self.check_cancellation(transport, row, req):
                raise JobCancelled()
        try:
            require_active()
            if result['state'] == 'CANCELLED' and (self.spool(req['job_id']) / 'cancel.json').exists():
                # Restore the persisted tombstone before acknowledging cancellation.
                transport.upload(self.spool(req['job_id']) / 'cancel.json',
                                 f"ci/jobs/{req['job_id']}/cancel.json")
                validate_cancellation(transport.read(f"ci/jobs/{req['job_id']}/cancel.json"), req, row['raw'])
            if cancelled_result:
                upload_json(transport, f"ci/jobs/{req['job_id']}/status.json",
                            decode((spool / 'status.json').read_bytes()), mutable=True)
            self.publish_live(transport, row, set(), spool=spool, prefix=prefix)
            require_active()
            for spec in result['artifacts']:
                require_active()
                verify_file(spool / spec['path'], spec)
                transport.upload(spool / spec['path'], prefix + '/' + spec['path'])
            checksum, size = hashlib.sha256(), 0
            for path in self.make_chunks(spool):
                data = path.read_bytes()
                checksum.update(data); size += len(data)
            log = next(spec for spec in result['artifacts'] if spec['path'] == 'build.log')
            if checksum.hexdigest() != log['sha256'] or size != log['bytes']:
                raise GdiError('durable log chunks do not match the finalized log')
            require_active()
            transport.upload(spool / 'result.json', prefix + '/result.json')
            require_active()
            self.verify_remote(transport, req, row, spool=spool, prefix=prefix)
            require_active()
        except JobCancelled:
            self.prepare_cancelled_delivery(transport, row, req)
            self.deliver(repo, row, req, cancelled_result=True)
            return
        self.ledger.update(req['job_id'], 'PUBLISHED')
        self.acknowledge(row)
        LOG.info('job=%s result=%s published', req['job_id'], result['state'])

    @transport_operation
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
        cancelled = threading.Event()
        if self.check_cancellation(transport, row, req):
            # Also covers a locally discovered job whose notification disappeared.
            self.stop_cancelled(transport, row, req)
            self.ledger.update(req['job_id'], 'FINALIZING')
            self.finish(row, req, 'CANCELLED', 130, None, [], [], now(), 0,
                        'cancelled by durable client request; owned processes and containers stopped')
            self.deliver(repo, row, req)
            return
        if row['state'] in ('RUNNING', 'FINALIZING'):
            # Execution may have finished just before the crash; never rerun blindly.
            kill_owned(decode(row['process']) if row['process'] else None)
            runner.cleanup(spool)
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
        publisher = threading.Thread(target=self.publisher, args=(transport, row, done, cancelled), daemon=True)
        publisher.start()
        checkout, state, code, failed, stages, warnings, detail = None, 'ERROR', 1, None, [], [], ''
        def require_active():
            if cancelled.is_set() or self.check_cancellation(transport, row, req):
                cancelled.set()
                raise JobCancelled()
        try:
            require_active()
            self.status(row, 'RESTORING')
            self.ledger.update(req['job_id'], 'RESTORING')
            checkout = self.checkout(repo, transport, req)
            require_active()
            self.status(row, 'CHECKOUT')
            if 'workflow' in profile:
                if req['ci_version'] == 2:
                    profile = {**profile, 'workflow': {**profile['workflow'], **req['workflow']}}
                profile = prepare_workflow(checkout, profile, spool, req, cache_dir=Path(self.config['cache_dir']) / self.worker_id / 'act')
                if profile.get('environment'):
                    atomic_write(spool / 'artifacts/environment.json', encode(profile['environment']))
                runner.record_owner(spool, checkout, profile)
            require_active()
            self.ledger.update(req['job_id'], 'RUNNING')
            state, code, failed, stages, warnings = execute(checkout, profile, spool / 'build.log',
                lambda state, stage: self.status(row, state, stage),
                lambda identity: self.ledger.update(req['job_id'], 'RUNNING', identity),
                cancelled=cancelled.is_set)
            require_active()
            if state == 'PASS' and 'workflow' in profile:
                require_completed_job(spool / 'build.log')
            if checkout.oid('HEAD') != req['head']:
                raise GdiError('CI changed requested HEAD')
        except JobCancelled:
            state, code, detail = 'CANCELLED', 130, 'cancelled by durable client request'
        except KeyboardInterrupt:
            state, code, detail = 'INTERRUPTED', 130, 'worker interrupted during execution'
            self.stop.set()
        except (GdiError, OSError, ValueError) as exc:
            state, code, detail = 'ERROR', 1, str(exc)
            LOG.exception('job=%s execution error', req['job_id'])
        finally:
            done.set()
            publisher.join()
        try:
            runner.cleanup(spool)
        except (GdiError, OSError) as exc:
            # Keep RUNNING until recovery can finish cleanup; never lose ownership.
            raise GdiError('Docker cleanup pending: ' + str(exc)) from exc
        self.ledger.update(req['job_id'], 'FINALIZING')
        self.finish(row, req, state, code, failed, stages, warnings, started_at, time.monotonic() - started,
                    detail, checkout if state != 'CANCELLED' else None,
                    profile if state != 'CANCELLED' else None)
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

    def recover_delivery(self):
        """Prepared results and uncertain runs recover even when runner setup fails."""
        for row in self.ledger.pending():
            if self.stop.is_set():
                return
            if (self.spool(row['job_id']) / 'result.json').exists() or row['state'] in ('RUNNING', 'FINALIZING'):
                try:
                    self.process(row)
                except (GdiError, OSError) as exc:
                    LOG.error('job=%s recovery pending: %s', row['job_id'], exc)

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
                    self.recover_delivery()
                    delay = min(delay * 2, self.config['poll_idle_max_seconds'])
                if once:
                    return
                self.stop.wait(delay)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
