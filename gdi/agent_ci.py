"""Prepare and verify CI files offline, using the same protocol/result validators."""

from pathlib import Path

from .agent import (file_plan, load_plan, persist_directory, plan_lock, same_route,
                    save_plan, verdict)
from .ci import CiResultReader
from .ci_protocol import (capability_revision, marker, prepare_request, request,
                          workflow_selection, atomic_write, cancellation, cancellation_capability,
                          validate_cancellation)
from .git import GdiError
from .inbox import filename, notification
from .publication import decode, digest, encode
from .snapshot import Snapshot, download_proof, local_file, metadata_file


def revision(path, worker, profile):
    return capability_revision(decode(metadata_file(path)), worker, profile)


def publication(snapshot, publication_id):
    if publication_id not in dict(snapshot.chain):
        raise GdiError('CI publication is not on the verified snapshot chain')
    return dict(snapshot.chain)[publication_id]


def ci_plan(git, directory, expected_id):
    value = load_plan(directory, expected_id, 'ci')
    keys = {'plan_version', 'kind', 'repository_id', 'repository_path', 'ref', 'head',
            'publication_id', 'job_id', 'worker_id', 'profile_id', 'profile_revision',
            'request_sha256', 'files', 'state', 'checked_snapshot'}
    if (set(value) != keys or value['state'] not in ('prepared', 'request_checked', 'accepted') or
            set(value['files']) != {'request', 'ready', 'inbox'}):
        raise GdiError('invalid CI plan')
    raw = metadata_file(local_file(directory, 'request.json'))
    req = request(decode(raw), git, expected_id)
    if (encode(req) != raw or digest(raw) != value['request_sha256'] or req['ci_version'] != 2 or
            any(req[key] != value[key] for key in ('repository_id', 'ref', 'head', 'publication_id',
                                                  'job_id', 'worker_id', 'profile_id', 'profile_revision'))):
        raise GdiError('saved CI request differs from plan')
    ready_raw = encode(marker(req, raw))
    if metadata_file(local_file(directory, 'request.ready')) != ready_raw:
        raise GdiError('saved CI ready differs from request')
    event = notification(expected_id, value['repository_path'], req, req['publication_id'], req, raw)
    if metadata_file(local_file(directory, 'inbox.json')) != encode(event):
        raise GdiError('saved CI inbox differs from request')
    prefix = value['repository_path'] + '/ci/jobs/' + req['job_id']
    for name, local, target in (('request', 'request.json', prefix + '/request.json'),
                                ('ready', 'request.ready', prefix + '/request.ready'),
                                ('inbox', 'inbox.json', 'inbox/' + filename(event))):
        if value['files'][name] != file_plan(local_file(directory, local), local, target):
            raise GdiError('saved CI destination differs from plan')
    return value, req, raw


def verify_snapshot(snapshot, value, req):
    same_route(snapshot, value)
    pub = publication(snapshot, req['publication_id'])
    if pub['ref'] != req['ref'] or pub['head'] != req['head']:
        raise GdiError('CI request differs from verified publication')


def prepare(git, snapshot_path, expected_id, output, publication_id, worker_id, profile_id,
            capabilities_path, *, workflow=None, retry_of=None, prepared_request=None):
    from .workflow import validate_selection
    snapshot = Snapshot(snapshot_path, expected_id)
    pub = publication(snapshot, publication_id)
    selector = workflow_selection(workflow)
    current_revision = revision(capabilities_path, worker_id, profile_id)
    namespace = git.github_repository()
    if not git.has_commit(pub['head']):
        raise GdiError('CI publication HEAD is missing locally; restore the published repository first')
    git.check_payload(pub['head'])
    validate_selection(git, pub['head'], selector)
    if retry_of is not None:
        from .ci_protocol import job_id
        import tempfile
        job_id(retry_of)
        with tempfile.TemporaryDirectory(prefix='gdi-agent-retry-') as temporary:
            reader = CiResultReader(git, snapshot, expected_id, temporary)
            old = reader.retry_request(retry_of)
            if (old['publication_id'] != publication_id or old['profile_id'] != profile_id
                    or old.get('workflow') != selector):
                raise GdiError('retry must refer to the same publication, profile and workflow')
            namespace = old.get('github_repository', '')
    with plan_lock(output) as directory:
        if (directory / 'plan.json').exists():
            value, req, _ = ci_plan(git, directory, expected_id)
            verify_snapshot(snapshot, value, req)
            if (req['publication_id'] != publication_id or req['worker_id'] != worker_id or
                    req['profile_id'] != profile_id or req['profile_revision'] != current_revision or
                    req['workflow'] != selector or req['retry_of'] != retry_of):
                raise GdiError('CI settings/revision changed; preserve this plan and use a new output directory')
            if prepared_request is not None and req != prepared_request:
                raise GdiError('automatic successor differs from saved CI plan')
            return verdict(directory, value)
        def build(temporary):
            req = prepared_request or prepare_request(git, expected_id, publication_id, pub, worker_id, profile_id,
                                  current_revision, workflow=selector,
                                  github_repository=namespace, retry_of=retry_of)
            request(req, git, expected_id)
            if (req['publication_id'] != publication_id or req['worker_id'] != worker_id or
                    req['profile_id'] != profile_id or req['profile_revision'] != current_revision or
                    req.get('workflow') != selector or req['retry_of'] != retry_of or
                    req['ref'] != pub['ref'] or req['head'] != pub['head'] or req.get('github_repository', '') != namespace):
                raise GdiError('automatic CI request differs from verified execution settings')
            raw = encode(req)
            event = notification(expected_id, snapshot.repository_path, pub, publication_id, req, raw)
            from .ci_protocol import atomic_write
            for name, content in (('request.json', raw), ('request.ready', encode(marker(req, raw))), ('inbox.json', encode(event))):
                atomic_write(temporary / name, content)
            prefix = snapshot.repository_path + '/ci/jobs/' + req['job_id']
            value = {'plan_version': 1, 'kind': 'ci', 'repository_id': expected_id,
                     'repository_path': snapshot.repository_path, 'ref': pub['ref'], 'head': pub['head'],
                     'publication_id': publication_id, 'job_id': req['job_id'], 'worker_id': worker_id,
                     'profile_id': profile_id, 'profile_revision': current_revision, 'request_sha256': digest(raw),
                     'files': {'request': file_plan(temporary / 'request.json', 'request.json', prefix + '/request.json'),
                               'ready': file_plan(temporary / 'request.ready', 'request.ready', prefix + '/request.ready'),
                               'inbox': file_plan(temporary / 'inbox.json', 'inbox.json', 'inbox/' + filename(event))},
                     'state': 'prepared', 'checked_snapshot': None}
            save_plan(temporary, value)
        persist_directory(directory, build)
        value, _, _ = ci_plan(git, directory, expected_id)
        return verdict(directory, value)


def workers(workers_path, profile_id='full', policy_path=None):
    from .scheduling import inspect_workers, load_policy, worker_snapshot
    return {'workers': inspect_workers(worker_snapshot(workers_path), profile_id, load_policy(policy_path)),
            'freshness': 'connector_provided_snapshot'}


def dispatch(git, snapshot_path, expected_id, output, publication_id, profile_id, workers_path,
             policy_path=None, *, workflow=None):
    from .scheduling import choose, inspect_workers, load_policy, worker_snapshot
    registry, settings = worker_snapshot(workers_path), load_policy(policy_path)
    output = Path(output)
    # Selection belongs to this immutable plan; replay never silently changes host.
    if (output / 'plan.json').exists():
        value, req, _ = ci_plan(git, output, expected_id)
        selected = {'worker_id': req['worker_id']}
    else:
        previous_path = git.gdi_dir() / 'scheduler.json'
        previous = decode(previous_path.read_bytes()).get('last_worker') if previous_path.exists() else None
        selected = choose(inspect_workers(registry, profile_id, settings), settings, previous=previous)
    if selected is None:
        raise GdiError('no fresh compatible worker is available')
    caps = registry.get(selected['worker_id'], {}).get('capabilities_path')
    if caps is None:
        raise GdiError('selected worker capabilities download is missing')
    value = prepare(git, snapshot_path, expected_id, output, publication_id, selected['worker_id'], profile_id,
                    caps, workflow=workflow)
    atomic_write(git.gdi_dir() / 'scheduler.json', encode({'last_worker': selected['worker_id']}))
    return value


def supervise(git, snapshot_path, expected_id, output, jid, workers_path, policy_path, *, inbox_proof=None):
    from .scheduling import Supervisor, load_policy, worker_snapshot
    snapshot = Snapshot(snapshot_path, expected_id)
    registry = worker_snapshot(workers_path)
    with plan_lock(output) as directory:
        directory.mkdir(parents=True, exist_ok=True)
        reader = CiResultReader(git, snapshot, expected_id, directory / 'verified-ci')
        supervisor = Supervisor(reader, jid, directory, load_policy(policy_path))
        decision = supervisor.decide(registry)
        active = decision['job_id']
        cancel_dir = directory / ('cancel-' + active)
        if decision['state'] == 'CANCEL_REQUIRED':
            caps = registry.get(reader.load_request(active)[0]['worker_id'], {}).get('capabilities_path')
            value = cancel(git, snapshot_path, expected_id, cancel_dir, active, caps)
            if value.get('already_finished'):
                return supervisor.response(value['state'], result=value, verified=True)
            return {**decision, 'state': 'CANCEL_PREPARED', 'plan': value, 'next_tool': 'agent ci cancel-check'}
        if (cancel_dir / 'plan.json').exists():
            # Validate actual connector delivery with the existing cancellation tool.
            cancel_check(git, cancel_dir, snapshot_path, expected_id)
        if decision['state'] != 'RETRY_REQUIRED':
            return decision
        req, raw, successor = supervisor.pending_request(decision['worker'])
        successor_path = directory / ('successor-' + active + '.json')
        atomic_write(successor_path, encode(successor))
        target = snapshot.repository_path + '/ci/jobs/' + active + '/successor.json'
        successor_file = file_plan(successor_path, successor_path.name, target)
        successor_file['local_path'] = str(successor_path.absolute())
        current = snapshot.read_optional('ci/jobs/' + active + '/successor.json')
        if current is None:
            return {**decision, 'state': 'SUCCESSOR_PREPARED', 'safe_to_upload_successor': True,
                    'files': {'successor': successor_file}, 'upload_order': ['successor']}
        if current != encode(successor):
            raise GdiError('conflicting automatic successor; stop competing coordinators')
        retry_dir = directory / ('attempt-' + req['job_id'])
        if (retry_dir / 'plan.json').exists():
            value, saved, _ = ci_plan(git, retry_dir, expected_id)
            if saved != req:
                raise GdiError('automatic successor differs from saved retry plan')
            verify_snapshot(snapshot, value, req)
            if value['state'] == 'accepted':
                supervisor.activate(req)
                return supervisor.response('REASSIGNED', worker_id=req['worker_id'], retry_of=active)
            value = verdict(retry_dir, value)
        else:
            caps = registry.get(req['worker_id'], {}).get('capabilities_path')
            if caps is None:
                raise GdiError('replacement worker capabilities download is missing')
            value = prepare(git, snapshot_path, expected_id, retry_dir, req['publication_id'], req['worker_id'],
                            req['profile_id'], caps, workflow=req.get('workflow'), retry_of=active, prepared_request=req)
        prefix = 'ci/jobs/' + req['job_id']
        binding = {'automatic_version': 1, 'previous_job': active, 'request_sha256': digest(raw),
                   'policy_sha256': digest(encode(supervisor.settings))}
        binding_path = retry_dir / 'automatic.json'
        if binding_path.exists() and metadata_file(binding_path) != encode(binding):
            raise GdiError('automatic retry binding changed after preparation')
        atomic_write(binding_path, encode(binding))
        caps = registry.get(req['worker_id'], {}).get('capabilities_path')
        if prefix in snapshot.folders and {'request.json', 'request.ready'}.issubset(
                {entry['Path'] for entry in snapshot.list(prefix)}):
            value = check(git, retry_dir, snapshot_path, expected_id, capabilities_path=caps)
            if inbox_proof is not None:
                value = accept(git, retry_dir, snapshot_path, expected_id, capabilities_path=caps, inbox_proof=inbox_proof)
                supervisor.activate(req)
                return supervisor.response('REASSIGNED', worker_id=req['worker_id'], retry_of=active, plan=value)
        return {**decision, 'state': 'RETRY_PREPARED', 'plan': value,
                'next_tool': 'agent ci accept' if value['safe_to_upload_inbox'] else 'agent ci check'}


def check_request(git, directory, snapshot_path, expected_id, capabilities_path):
    value, req, raw = ci_plan(git, directory, expected_id)
    snapshot = Snapshot(snapshot_path, expected_id, ref=req['ref'])
    verify_snapshot(snapshot, value, req)
    if revision(capabilities_path, req['worker_id'], req['profile_id']) != req['profile_revision']:
        raise GdiError('worker capabilities revision changed; prepare a new CI request explicitly')
    prefix = 'ci/jobs/' + req['job_id']
    for remote, local in (('request.json', raw), ('request.ready', encode(marker(req, raw)))):
        snapshot.require_file(prefix + '/' + remote, digest(local), len(local))
    # Automatic plans have a persisted predecessor proposal. Explicit retries
    # keep their existing snapshot requirements.
    if (directory / 'automatic.json').exists():
        from .scheduling import validate_successor
        binding = decode(metadata_file(directory / 'automatic.json'))
        if (set(binding) != {'automatic_version', 'previous_job', 'request_sha256', 'policy_sha256'} or
                type(binding['automatic_version']) is not int or binding['automatic_version'] != 1 or
                binding['previous_job'] != req['retry_of'] or binding['request_sha256'] != digest(raw)):
            raise GdiError('automatic retry binding/request identity mismatch')
        reader = CiResultReader(git, snapshot, expected_id, directory.parent / 'verified-ci')
        old, old_raw = reader.load_request(req['retry_of'])
        successor = validate_successor(snapshot.read('ci/jobs/' + req['retry_of'] + '/successor.json'), old, old_raw, req)
        if successor['policy_sha256'] != binding['policy_sha256']:
            raise GdiError('automatic successor policy differs from the saved retry binding')
        reader.retry_request(req['retry_of'])
    return value, req, snapshot


def check(git, output, snapshot_path, expected_id, *, capabilities_path):
    with plan_lock(output) as directory:
        value, _, snapshot = check_request(git, directory, snapshot_path, expected_id, capabilities_path)
        if value['state'] == 'accepted':
            raise GdiError('CI request already accepted; use result to inspect it')
        value.update(state='request_checked', checked_snapshot=snapshot.fingerprint)
        save_plan(directory, value)
        return verdict(directory, value)


def accept(git, output, snapshot_path, expected_id, *, capabilities_path, inbox_proof):
    with plan_lock(output) as directory:
        value, _, snapshot = check_request(git, directory, snapshot_path, expected_id, capabilities_path)
        item = value['files']['inbox']
        download_proof(inbox_proof, item['target_name'], item['sha256'], item['bytes'])
        value.update(state='accepted', checked_snapshot=snapshot.fingerprint)
        save_plan(directory, value)
        return verdict(directory, value)


def result(git, output, snapshot_path, expected_id, *, heartbeat_timeout=600):
    from .worker_config import seconds
    seconds(heartbeat_timeout)
    with plan_lock(output) as directory:
        value, req, raw = ci_plan(git, directory, expected_id)
        snapshot = Snapshot(snapshot_path, expected_id, ref=req['ref'])
        verify_snapshot(snapshot, value, req)
        prefix = 'ci/jobs/' + req['job_id']
        for remote, content in (('request.json', raw), ('request.ready', encode(marker(req, raw)))):
            snapshot.require_file(prefix + '/' + remote, digest(content), len(content))
        reader = CiResultReader(git, snapshot, expected_id, directory / 'verified-ci')
        source = reader.result_source(req, raw)
        if source is not None:
            prefix, remote_result = source
            for item in remote_result['artifacts']:
                snapshot.require_file(prefix + '/' + item['path'], item['sha256'], item['bytes'])
            for name, checksum in reader.chunks(req['job_id'], cancelled_delivery=prefix.endswith('/cancelled')):
                path = prefix + '/log-chunks/' + name
                snapshot.require_file(path, checksum, snapshot.entries[path]['bytes'])
        completed = reader.result(req['job_id'])
        if completed is None:
            cancelled = reader.cancellation_requested(req['job_id'])
            status_path = 'ci/jobs/' + req['job_id'] + '/status.json'
            # Advisory diagnostics must not make old result snapshots require
            # an additional download. Distinguish omitted bytes from absence.
            if status_path in snapshot.entries and status_path not in snapshot.downloads:
                from .scheduling import heartbeat
                freshness = {**heartbeat(None, heartbeat_timeout), 'state': 'unavailable',
                             'reason_code': 'status_not_downloaded'}
            else:
                _, freshness = reader.live_status(req, raw, heartbeat_timeout=heartbeat_timeout)
            value = {'job_id': req['job_id'], 'state': 'CANCEL_REQUESTED' if cancelled else 'PENDING',
                     'verified': False, 'heartbeat': freshness}
            if cancelled and cancelled.get('worker_cancellation_supported') is False:
                value['worker_cancellation_supported'] = False
            return value
        return {**completed, 'verified': True, 'local_artifacts': str(reader.root / 'results' / req['job_id'])}


def cancellation_plan(git, directory, expected_id):
    value = load_plan(directory, expected_id, 'ci-cancel')
    keys = {'plan_version', 'kind', 'repository_id', 'repository_path', 'ref', 'head',
            'publication_id', 'job_id', 'worker_id', 'request_sha256', 'files', 'state', 'checked_snapshot'}
    if (set(value) != keys or value['state'] not in ('prepared', 'accepted')
            or set(value['files']) != {'cancel'}):
        raise GdiError('invalid CI cancellation plan')
    raw = metadata_file(local_file(directory, 'request.json'))
    req = request(decode(raw), git, expected_id)
    if (digest(raw) != value['request_sha256'] or
            any(req[key] != value[key] for key in ('repository_id', 'job_id', 'worker_id', 'ref', 'head', 'publication_id'))):
        raise GdiError('saved CI cancellation request differs from plan')
    local = local_file(directory, 'cancel.json')
    validate_cancellation(metadata_file(local), req, raw)
    target = value['repository_path'] + '/ci/jobs/' + req['job_id'] + '/cancel.json'
    if value['files']['cancel'] != file_plan(local, 'cancel.json', target):
        raise GdiError('saved CI cancellation destination differs from plan')
    return value, req, raw


def cancel(git, snapshot_path, expected_id, output, jid, capabilities_path):
    snapshot = Snapshot(snapshot_path, expected_id)
    with plan_lock(output) as directory:
        reader = CiResultReader(git, snapshot, expected_id, directory / 'verified-ci')
        req, raw = reader.load_request(jid)
        completed = reader.result(jid)
        if completed is not None:
            return {**completed, 'verified': True, 'already_finished': True}
        cancellation_capability(decode(metadata_file(capabilities_path)), req['worker_id'])
        pub = publication(snapshot, req['publication_id'])
        if (pub['ref'], pub['head']) != (req['ref'], req['head']):
            raise GdiError('CI cancellation request differs from verified publication')
        if (directory / 'plan.json').exists():
            value, saved, saved_raw = cancellation_plan(git, directory, expected_id)
            same_route(snapshot, value)
            if saved != req or saved_raw != raw:
                raise GdiError('CI cancellation settings changed; preserve the original plan')
            return verdict(directory, value)
        def build(temporary):
            atomic_write(temporary / 'request.json', raw)
            atomic_write(temporary / 'cancel.json', encode(cancellation(req, raw)))
            target = snapshot.repository_path + '/ci/jobs/' + jid + '/cancel.json'
            value = {'plan_version': 1, 'kind': 'ci-cancel', 'repository_id': expected_id,
                     'repository_path': snapshot.repository_path, 'ref': req['ref'], 'head': req['head'],
                     'publication_id': req['publication_id'], 'job_id': jid, 'worker_id': req['worker_id'],
                     'request_sha256': digest(raw), 'state': 'prepared', 'checked_snapshot': None,
                     'files': {'cancel': file_plan(temporary / 'cancel.json', 'cancel.json', target)}}
            save_plan(temporary, value)
        persist_directory(directory, build)
        value, _, _ = cancellation_plan(git, directory, expected_id)
        return verdict(directory, value)


def cancel_check(git, output, snapshot_path, expected_id):
    with plan_lock(output) as directory:
        value, req, raw = cancellation_plan(git, directory, expected_id)
        snapshot = Snapshot(snapshot_path, expected_id, ref=req['ref'])
        same_route(snapshot, value)
        reader = CiResultReader(git, snapshot, expected_id, directory / 'verified-ci')
        remote_req, remote_raw = reader.load_request(req['job_id'])
        if remote_req != req or remote_raw != raw:
            raise GdiError('CI cancellation remote request differs from plan')
        content = encode(cancellation(req, raw))
        snapshot.require_file('ci/jobs/' + req['job_id'] + '/cancel.json', digest(content), len(content))
        value.update(state='accepted', checked_snapshot=snapshot.fingerprint)
        save_plan(directory, value)
        return verdict(directory, value)
