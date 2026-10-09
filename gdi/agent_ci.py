"""Prepare and verify CI files offline, using the same protocol/result validators."""

from .agent import (file_plan, load_plan, persist_directory, plan_lock, same_route,
                    save_plan, verdict)
from .ci import CiResultReader
from .ci_protocol import (capability_revision, marker, prepare_request, request,
                          workflow_selection)
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
            old = reader.result(retry_of)
            if old is None:
                raise GdiError('cannot retry an active job; download its complete terminal result first')
            if old['publication_id'] != publication_id or old['worker_id'] != worker_id or old['profile_id'] != profile_id:
                raise GdiError('retry must refer to the same publication and worker/profile')
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
                                  github_repository=git.github_repository(), retry_of=retry_of)
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
        from .ci_protocol import validate_result
        if 'result.json' in {item['Path'] for item in snapshot.list(prefix)}:
            remote_result = validate_result(decode(snapshot.read(prefix + '/result.json')), req, digest(raw))
            for item in remote_result['artifacts']:
                snapshot.require_file(prefix + '/' + item['path'], item['sha256'], item['bytes'])
            for name, checksum in reader.chunks(req['job_id']):
                path = prefix + '/log-chunks/' + name
                snapshot.require_file(path, checksum, snapshot.entries[path]['bytes'])
        completed = reader.result(req['job_id'])
        if completed is None:
            return {'job_id': req['job_id'], 'state': 'PENDING', 'verified': False}
        return {**completed, 'verified': True, 'local_artifacts': str(reader.root / 'results' / req['job_id'])}
