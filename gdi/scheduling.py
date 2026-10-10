"""Worker discovery and timeout policy shared by CLI and connector agents."""

from datetime import datetime, timezone
from pathlib import Path

from .ci_protocol import capability_revision, cancellation_capability, identifier
from .exchange import decode, digest, encode
from .git import GdiError
from .snapshot import listing_pages, local_file, metadata_file, opaque_id
from .worker_config import seconds


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise GdiError('invalid scheduling timestamp') from exc


def clock():
    return datetime.now(timezone.utc).timestamp()


def heartbeat(updated_at, timeout_seconds, *, at=None):
    """Freshness is advisory: an old heartbeat does not prove the process stopped."""
    seconds(timeout_seconds)
    at = clock() if at is None else at
    value = {'updated_at': updated_at, 'age_seconds': None,
             'timeout_seconds': timeout_seconds, 'state': 'missing'}
    if updated_at is None:
        return value
    try:
        age = at - timestamp(updated_at)
    except GdiError:
        return {**value, 'state': 'invalid'}
    if age < -30:
        return {**value, 'state': 'clock_skew', 'clock_ahead_seconds': -age}
    return {**value, 'state': 'stale' if age >= timeout_seconds else 'fresh',
            'age_seconds': max(0, age)}


def policy(value=None):
    defaults = {'policy_version': 1, 'queue_timeout_seconds': 900,
                'heartbeat_timeout_seconds': 600, 'worker_fresh_seconds': 300,
                'max_attempts': 3, 'backoff_seconds': 30, 'retry_mode': 'confirmed_stop',
                'selection': 'load', 'preferred_worker': None, 'pinned_worker': None,
                'required_labels': [], 'required_platforms': []}
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise GdiError('invalid CI scheduling policy fields')
    result = {**defaults, **value}
    if type(result['policy_version']) is not int or result['policy_version'] != 1:
        raise GdiError('unsupported CI scheduling policy')
    for key in ('queue_timeout_seconds', 'heartbeat_timeout_seconds', 'worker_fresh_seconds', 'backoff_seconds'):
        seconds(result[key])
    if type(result['max_attempts']) is not int or not 1 <= result['max_attempts'] <= 100:
        raise GdiError('max_attempts must be an integer between 1 and 100')
    if result['retry_mode'] not in ('repeatable', 'confirmed_stop', 'never'):
        raise GdiError('retry_mode must be repeatable, confirmed_stop or never')
    if result['selection'] not in ('load', 'round_robin', 'preferred', 'current'):
        raise GdiError('selection must be load, round_robin, preferred or current')
    for key in ('preferred_worker', 'pinned_worker'):
        if result[key] is not None:
            identifier(result[key])
    if result['selection'] in ('preferred', 'current') and result['preferred_worker'] is None:
        raise GdiError('preferred/current selection requires preferred_worker (current host ID from its config)')
    labels(result['required_labels'])
    if (not isinstance(result['required_platforms'], list) or
            any(not isinstance(item, str) or not item or any(ord(c) < 32 for c in item)
                for item in result['required_platforms']) or
            len(set(result['required_platforms'])) != len(result['required_platforms'])):
        raise GdiError('required_platforms must contain unique nonempty runs-on names')
    return result


def load_policy(path):
    return policy(decode(metadata_file(path))) if path else policy()


def labels(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value) or len(value) != len(set(value)):
        raise GdiError('worker labels must be a list of unique identifiers')
    for item in value:
        identifier(item)
    return value


def discover(transport):
    """Do not replace transport failures or incomplete listings with an empty registry."""
    from .ci import optional_read
    result, names = {}, set()
    for entry in transport.list('ci/workers'):
        worker = identifier(entry['Path'])
        if worker in names or not entry['IsDir']:
            raise GdiError('duplicate or invalid worker directory')
        names.add(worker)
        base = 'ci/workers/' + worker
        caps = optional_read(transport, base + '/capabilities.json')
        status = optional_read(transport, base + '/status.json')
        result[worker] = {'capabilities': decode(caps) if caps is not None else None,
                          'status': decode(status) if status is not None else None}
    return result


def worker_snapshot(path):
    """Complete registry listing plus per-worker listings and actual downloaded files."""
    path = Path(path).absolute()
    value = decode(metadata_file(path))
    if (set(value) != {'workers_version', 'folder_id', 'pages', 'workers'} or
            type(value['workers_version']) is not int or value['workers_version'] != 1 or
            not isinstance(value['workers'], list)):
        raise GdiError('invalid workers snapshot')
    root = opaque_id(value['folder_id'])
    ids = {root}
    entries = listing_pages(value['pages'], ids)
    directories = {}
    for entry in entries:
        identifier(entry['name'])
        if not entry['is_dir']:
            raise GdiError('worker registry entries must be directories')
        directories[entry['name']] = entry['id']
    result, locals_used = {}, set()
    for row in value['workers']:
        if (not isinstance(row, dict) or set(row) != {'worker_id', 'folder_id', 'pages', 'files'} or
                row['worker_id'] in result or directories.get(row['worker_id']) != row['folder_id'] or
                not isinstance(row['files'], list)):
            raise GdiError('invalid or duplicate worker snapshot entry')
        listing = listing_pages(row['pages'], ids)
        by_id = {item['id']: item for item in listing if not item['is_dir']}
        downloaded = {}
        for file in row['files']:
            if not isinstance(file, dict) or set(file) != {'file_id', 'local_path'} or file['file_id'] not in by_id:
                raise GdiError('unlisted worker snapshot download')
            entry = by_id[file['file_id']]
            local = local_file(path.parent, file['local_path'])
            if entry['name'] in downloaded or local in locals_used or local.stat().st_size != entry['bytes']:
                raise GdiError('duplicate worker download or size mismatch')
            locals_used.add(local)
            downloaded[entry['name']] = decode(metadata_file(local))
        for name in ('capabilities.json', 'status.json'):
            if any(item['name'] == name for item in listing) and name not in downloaded:
                raise GdiError('worker snapshot download is missing: ' + name)
        result[row['worker_id']] = {'capabilities': downloaded.get('capabilities.json'),
                                     'status': downloaded.get('status.json'),
                                     'capabilities_path': next((str(local_file(path.parent, item['local_path']))
                                         for item in row['files'] if by_id[item['file_id']]['name'] == 'capabilities.json'), None)}
    if set(result) != set(directories):
        raise GdiError('workers snapshot requires all listed worker directories')
    return result


def inspect_workers(registry, profile_id, settings, *, repository_id=None, at=None):
    at = clock() if at is None else at
    result = []
    for worker, row in sorted(registry.items()):
        caps, status = row['capabilities'], row['status']
        observed = heartbeat(status.get('updated_at') if isinstance(status, dict) else None,
                             settings['worker_fresh_seconds'], at=at)
        value = {'worker_id': worker, 'available': False, 'reason': None, 'reason_code': None,
                 'heartbeat': observed, 'heartbeat_age_seconds': observed['age_seconds'],
                 'requested_profile': profile_id}
        def reject(code, message, **details):
            value.update(reason_code=code, **details)
            raise GdiError(message)
        try:
            if not isinstance(caps, dict):
                reject('capabilities_missing', 'worker capabilities are missing or invalid; update/start worker')
            registrations = caps.get('repositories')
            profiles = (caps.get('profiles') if repository_id is None else
                        registrations.get(repository_id) if isinstance(registrations, dict) else None)
            if isinstance(profiles, dict):
                value['available_profiles'] = sorted(profiles)
                if profile_id not in profiles:
                    reject('profile_missing', 'worker does not advertise the requested CI profile: ' + profile_id)
            elif repository_id is not None:
                reject('repository_missing', 'worker does not advertise this repository', repository_id=repository_id)
            revision = capability_revision(caps, worker, profile_id, repository_id)
            cancellation_capability(caps, worker)
            value['profile_revision'] = revision
            if (not isinstance(status, dict) or status.get('worker_id') != worker or
                    status.get('registry_version') != 1 or type(status.get('registry_version')) is not int):
                reject('registry_status_missing', 'worker heartbeat is missing, legacy or has a different worker ID; update/start worker')
            revisions = caps['profiles'] if repository_id is None else caps['repositories']
            if status.get('profile_revisions') != revisions:
                reject('revision_mismatch', 'worker heartbeat revision does not match capabilities; refresh metadata or restart worker',
                       advertised_revisions=revisions, heartbeat_revisions=status.get('profile_revisions'))
            if (type(status.get('queue_length')) is not int or status['queue_length'] < 0 or
                    type(status.get('busy')) is not bool):
                reject('registry_status_invalid', 'worker busy/queue information is invalid')
            value.update(busy=status['busy'], queue_length=status['queue_length'])
            if observed['state'] != 'fresh':
                reasons = {'stale': 'stale worker heartbeat; host may be offline or unable to publish to Drive',
                           'missing': 'worker heartbeat timestamp is missing',
                           'invalid': 'worker heartbeat timestamp is invalid',
                           'clock_skew': 'worker heartbeat is in the future; check clock synchronization'}
                reject('heartbeat_' + observed['state'], reasons[observed['state']])
            if status.get('state') not in ('READY', 'BUSY'):
                reject('worker_not_ready', 'worker is not ready for assignment', worker_state=status.get('state'))
            advertised_labels = labels(caps.get('labels', []))
            value['labels'] = advertised_labels
            if status.get('labels') != advertised_labels:
                reject('labels_mismatch', 'worker labels changed after capabilities; refresh metadata or restart worker')
            missing_labels = sorted(set(settings['required_labels']) - set(advertised_labels))
            if missing_labels:
                reject('labels_missing', 'required environment labels are missing: ' + ', '.join(missing_labels),
                       missing_labels=missing_labels)
            platforms = caps.get('platforms', [])
            if (not isinstance(platforms, list) or any(not isinstance(item, str) or not item for item in platforms) or
                    status.get('platforms', []) != platforms):
                reject('platforms_mismatch', 'worker platforms do not match capabilities; refresh metadata or restart worker')
            value['platforms'] = platforms
            missing_platforms = sorted(set(settings['required_platforms']) - set(platforms))
            if missing_platforms:
                reject('platforms_missing', 'required workflow platforms are missing: ' + ', '.join(missing_platforms),
                       missing_platforms=missing_platforms)
            value['available'] = True
        except GdiError as exc:
            value['reason'] = str(exc)
            if value['reason_code'] is None:
                value['reason_code'] = 'capabilities_invalid'
        result.append(value)
    return result


def choose(workers, settings, *, exclude=(), previous=None):
    eligible = [row for row in workers if row['available'] and row['worker_id'] not in exclude]
    pinned = settings['pinned_worker']
    if pinned:
        eligible = [row for row in eligible if row['worker_id'] == pinned]
    if not eligible:
        return None
    preferred = settings['preferred_worker']
    if settings['selection'] in ('preferred', 'current'):
        match = next((row for row in eligible if row['worker_id'] == preferred), None)
        if match:
            return match
    if settings['selection'] != 'round_robin':
        best = min((row['busy'], row['queue_length']) for row in eligible)
        eligible = [row for row in eligible if (row['busy'], row['queue_length']) == best]
    eligible.sort(key=lambda row: row['worker_id'])
    return next((row for row in eligible if previous is None or row['worker_id'] > previous), eligible[0])


def timeout_reason(reader, req, raw, state, settings, *, at=None):
    """Log silence never measures liveness; a claim/status selects the heartbeat timeout."""
    from .ci import optional_read
    from .exchange import hex_value
    at = clock() if at is None else at
    prefix = 'ci/jobs/' + req['job_id']
    claim_raw = optional_read(reader.transport, prefix + '/worker.running.json')
    status_raw = optional_read(reader.transport, prefix + '/status.json')
    claim = decode(claim_raw) if claim_raw is not None else None
    status = decode(status_raw) if status_raw is not None else None
    if claim is not None:
        if (set(claim) != {'ci_version', 'job_id', 'run_id', 'worker_id', 'request_sha256'} or
                claim['ci_version'] != 1 or claim['job_id'] != req['job_id'] or
                claim['worker_id'] != req['worker_id'] or claim['request_sha256'] != digest(raw) or
                not hex_value(claim['run_id'], 32)):
            raise GdiError('CI execution claim identity mismatch')
    if status is not None:
        if (status.get('job_id') != req['job_id'] or status.get('worker_id') != req['worker_id'] or
                status.get('request_sha256') != digest(raw) or not hex_value(status.get('run_id'), 32) or
                (claim is not None and status['run_id'] != claim['run_id'])):
            raise GdiError('CI heartbeat/request identity mismatch')
        updated = timestamp(status.get('updated_at'))
        if updated > at + 30:
            raise GdiError('CI heartbeat is in the future; check clock synchronization')
        previous = state.get('heartbeat_at')
        if previous is not None and updated < previous:
            raise GdiError('CI heartbeat moved backwards')
        state['heartbeat_at'] = updated
    if claim is not None or status is not None or state.get('claimed_at') is not None:
        state.setdefault('claimed_at', at)
        latest = max(state['claimed_at'], state.get('heartbeat_at', 0))
        return 'HEARTBEAT_TIMEOUT' if at - latest >= settings['heartbeat_timeout_seconds'] else None
    created = timestamp(req['created_at'])
    if created > at + 30:
        raise GdiError('CI request is in the future; check clock synchronization')
    return 'QUEUE_TIMEOUT' if at - created >= settings['queue_timeout_seconds'] else None


def attempt_chain(reader, req):
    """Reconstruct history from immutable requests; reject cycles and changed work."""
    result = [req]
    while result[-1]['retry_of'] is not None:
        old, _ = reader.load_request(result[-1]['retry_of'])
        if old['job_id'] in {item['job_id'] for item in result}:
            raise GdiError('CI retry history contains a cycle')
        for key in ('repository_id', 'publication_id', 'ref', 'head', 'profile_id', 'workflow', 'github_repository'):
            if old.get(key) != req.get(key):
                raise GdiError('CI retry history changes publication, profile or workflow')
        result.append(old)
        if len(result) > 100:
            raise GdiError('CI retry history exceeds 100 attempts')
    return list(reversed(result))


def validate_successor(content, old, raw, req):
    """One immutable proposal per attempt; this is a fence, not a Drive mutex."""
    from .exchange import hex_value
    value = decode(content)
    if (set(value) != {'successor_version', 'job_id', 'request_sha256', 'policy_sha256', 'request'} or
            type(value['successor_version']) is not int or value['successor_version'] != 1 or
            value['job_id'] != old['job_id'] or value['request_sha256'] != digest(raw) or
            not hex_value(value['policy_sha256'], 64) or value['request'] != req or
            req['retry_of'] != old['job_id'] or content != encode(value)):
        raise GdiError('automatic successor/request identity mismatch; stop competing coordinators')
    for key in ('repository_id', 'publication_id', 'ref', 'head', 'profile_id', 'workflow', 'github_repository'):
        if old.get(key) != req.get(key):
            raise GdiError('automatic successor changes publication/profile/workflow')
    return value


class Supervisor:
    """Durable single-coordinator decisions; remote I/O is performed by existing tools."""

    def __init__(self, reader, jid, directory, settings):
        from .ci_protocol import atomic_write, job_id
        self.reader, self.directory = reader, Path(directory)
        self.settings = policy(settings)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'session.json'
        if self.path.is_symlink():
            raise GdiError('supervisor session must not be a symlink')
        self.write = atomic_write
        if self.path.exists():
            state = decode(metadata_file(self.path))
            if (set(state) != {'supervisor_version', 'repository_id', 'root_job', 'policy', 'active_job', 'attempts', 'jobs'} or
                    type(state['supervisor_version']) is not int or state['supervisor_version'] != 1 or
                    state['repository_id'] != reader.repository_id or state['root_job'] != jid or
                    state['policy'] != self.settings or not isinstance(state['attempts'], list) or
                    not state['attempts'] or any(not isinstance(item, str) for item in state['attempts']) or
                    len(set(state['attempts'])) != len(state['attempts']) or
                    state['active_job'] != state['attempts'][-1] or not isinstance(state['jobs'], dict) or
                    set(state['jobs']) != set(state['attempts'])):
                raise GdiError('supervisor settings/history changed; preserve this session')
            for attempt in state['attempts']:
                job_id(attempt)
                if not isinstance(state['jobs'][attempt], dict):
                    raise GdiError('invalid supervisor attempt history')
            self.state = state
        else:
            req, _ = reader.load_request(jid)
            chain = attempt_chain(reader, req)
            self.state = {'supervisor_version': 1, 'repository_id': reader.repository_id,
                          'root_job': jid, 'policy': self.settings, 'active_job': jid,
                          'attempts': [item['job_id'] for item in chain],
                          'jobs': {item['job_id']: {} for item in chain}}
            self.save()

    def save(self):
        self.write(self.path, encode(self.state))

    def response(self, state, **extra):
        history = self.state['jobs'][self.state['active_job']]
        return {'job_id': self.state['active_job'], 'root_job': self.state['root_job'],
                'attempts': list(self.state['attempts']), 'state': state, 'verified': False,
                'cancellation_requested': history.get('cancellation_requested', False),
                'stop_confirmed': history.get('stop_confirmed', False), **extra}

    def decide(self, registry, *, at=None):
        at = clock() if at is None else at
        jid = self.state['active_job']
        req, raw = self.reader.load_request(jid)
        history = self.state['jobs'][jid]
        checksum = digest(raw)
        if history.get('request_sha256', checksum) != checksum:
            raise GdiError('supervised immutable request changed')
        history['request_sha256'] = checksum
        completed = self.reader.result(jid, _request=(req, raw))
        cancelled = self.reader.cancellation_record(req, raw)
        history['cancellation_requested'] = cancelled is not None
        history['stop_confirmed'] = completed is not None and completed['state'] == 'CANCELLED'
        # Preserve an already verified terminal result. Only our own timeout
        # cancellation can advance this session to another attempt.
        if completed is not None and (history.get('reason') is None or cancelled is None):
            self.save()
            return self.response(completed['state'], result={**completed, 'verified': True}, verified=True)
        if history.get('reason') is None:
            reason = timeout_reason(self.reader, req, raw, history, self.settings, at=at)
            if reason is None:
                self.save()
                return self.response('CANCEL_REQUESTED' if cancelled else 'MONITORING')
            # A manually cancelled attempt is not permission to retry it.
            if cancelled is not None:
                self.save()
                return self.response('CANCEL_REQUESTED')
            history.update(reason=reason, timed_out_at=at)
        self.save()
        if cancelled is None:
            caps = registry.get(req['worker_id'], {}).get('capabilities')
            cancellation_capability(caps, req['worker_id'])
            return self.response('CANCEL_REQUIRED', reason=history['reason'])
        if cancelled.get('worker_cancellation_supported') is False:
            raise GdiError('automatic retry requires durable supported cancellation')
        history.setdefault('cancelled_at', at)
        self.save()
        if self.settings['retry_mode'] == 'never':
            return self.response('CANCELLED' if completed else 'CANCEL_REQUESTED', retry_disabled=True,
                                 **({'result': {**completed, 'verified': True}, 'verified': True} if completed else {}))
        if len(self.state['attempts']) >= self.settings['max_attempts']:
            return self.response('ATTEMPTS_EXHAUSTED', reason=history['reason'])
        if self.settings['retry_mode'] == 'confirmed_stop' and (completed is None or completed['state'] != 'CANCELLED'):
            return self.response('WAITING_FOR_STOP', reason=history['reason'])
        delay = self.settings['backoff_seconds'] * 2 ** (len(self.state['attempts']) - 1)
        remaining = history['cancelled_at'] + delay - at
        if remaining > 0:
            return self.response('BACKOFF', retry_after_seconds=remaining)
        pending = self.directory / ('retry-' + jid + '.json')
        if pending.exists():
            from .ci_protocol import request
            saved = request(decode(metadata_file(pending)), self.reader.git, self.reader.repository_id)
            return self.response('RETRY_REQUIRED', worker={'worker_id': saved['worker_id'],
                                  'profile_revision': saved['profile_revision']}, reason=history['reason'])
        workers = inspect_workers(registry, req['profile_id'], self.settings,
                                  repository_id=None if req['ci_version'] == 2 else req['repository_id'], at=at)
        # Pinned jobs may retry only on that same host, after confirmed stop.
        exclude = () if self.settings['pinned_worker'] and completed is not None else (req['worker_id'],)
        selected = choose(workers, self.settings, exclude=exclude, previous=req['worker_id'])
        if selected is None:
            return self.response('NO_COMPATIBLE_WORKER', workers=workers)
        return self.response('RETRY_REQUIRED', worker=selected, reason=history['reason'])

    def pending_request(self, selected):
        """Freeze bytes before upload and use one deterministic successor job ID."""
        from .ci_protocol import prepare_request, request
        jid = self.state['active_job']
        old, raw = self.reader.load_request(jid)
        path = self.directory / ('retry-' + jid + '.json')
        successor_raw = self.reader.transport.read_optional('ci/jobs/' + jid + '/successor.json') if hasattr(self.reader.transport, 'read_optional') else None
        if not hasattr(self.reader.transport, 'read_optional'):
            from .ci import optional_read
            successor_raw = optional_read(self.reader.transport, 'ci/jobs/' + jid + '/successor.json')
        if path.exists():
            next_raw = metadata_file(path)
            req = request(decode(next_raw), self.reader.git, self.reader.repository_id)
        elif successor_raw is not None:
            successor = decode(successor_raw)
            req = request(successor.get('request', {}), self.reader.git, self.reader.repository_id)
            next_raw = encode(req)
        else:
            req = prepare_request(self.reader.git, old['repository_id'], old['publication_id'], old,
                                  selected['worker_id'], old['profile_id'], selected['profile_revision'],
                                  shared=old['ci_version'] == 2, workflow=old.get('workflow'),
                                  github_repository=old.get('github_repository', ''), retry_of=jid)
            req['job_id'] = digest(encode(['gdi-timeout-retry-v1', jid, self.settings]))[:32]
            next_raw = encode(req)
        if req['retry_of'] != jid:
            raise GdiError('automatic successor does not retry the active job')
        if next_raw != encode(req):
            raise GdiError('saved automatic request bytes changed after preparation')
        for key in ('repository_id', 'publication_id', 'ref', 'head', 'profile_id', 'workflow', 'github_repository'):
            if old.get(key) != req.get(key):
                raise GdiError('automatic successor changes publication/profile/workflow')
        expected = {'successor_version': 1, 'job_id': jid, 'request_sha256': digest(raw),
                    'policy_sha256': digest(encode(self.settings)), 'request': req}
        if req['job_id'] != digest(encode(['gdi-timeout-retry-v1', jid, self.settings]))[:32]:
            raise GdiError('automatic successor job ID differs from the saved policy')
        if successor_raw is not None and successor_raw != encode(expected):
            raise GdiError('conflicting automatic successor; stop competing coordinators')
        self.write(path, next_raw)
        return req, next_raw, expected

    def activate(self, req):
        jid = self.state['active_job']
        if req['retry_of'] != jid or req['job_id'] in self.state['attempts']:
            raise GdiError('invalid automatic retry transition')
        self.state['jobs'][jid]['successor'] = req['job_id']
        self.state['attempts'].append(req['job_id'])
        self.state['jobs'][req['job_id']] = {'request_sha256': digest(encode(req))}
        self.state['active_job'] = req['job_id']
        self.save()
