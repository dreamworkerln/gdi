"""Prepare and verify CI files offline, using the same protocol/result validators."""

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
            capabilities_path, *, workflow=None, retry_of=None):
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
            return verdict(directory, value)
        def build(temporary):
            req = prepare_request(git, expected_id, publication_id, pub, worker_id, profile_id,
                                  current_revision, workflow=selector,
                                  github_repository=namespace, retry_of=retry_of)
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


def check_request(git, directory, snapshot_path, expected_id, capabilities_path):
    value, req, raw = ci_plan(git, directory, expected_id)
    snapshot = Snapshot(snapshot_path, expected_id, ref=req['ref'])
    verify_snapshot(snapshot, value, req)
    if revision(capabilities_path, req['worker_id'], req['profile_id']) != req['profile_revision']:
        raise GdiError('worker capabilities revision changed; prepare a new CI request explicitly')
    prefix = 'ci/jobs/' + req['job_id']
    for remote, local in (('request.json', raw), ('request.ready', encode(marker(req, raw)))):
        snapshot.require_file(prefix + '/' + remote, digest(local), len(local))
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


def result(git, output, snapshot_path, expected_id):
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
            value = {'job_id': req['job_id'], 'state': 'CANCEL_REQUESTED' if cancelled else 'PENDING', 'verified': False}
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
