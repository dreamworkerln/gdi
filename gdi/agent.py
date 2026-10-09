"""Offline Git publication plans; the connector performs all remote I/O."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import tempfile

from .branches import branch_directory
from .ci_protocol import atomic_write
from .git import GdiError, Git
from .inbox import filename, notification
from .publication import (decode, digest, encode, file_digest, hex_value,
                          prepare_publication, verify_bundle, validate_manifest)
from .snapshot import Snapshot, local_file, metadata_file


@contextmanager
def plan_lock(directory):
    directory = Path(directory).absolute()
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise GdiError('agent plan must not be a symlink')
    lock = directory.parent / ('.' + directory.name + '.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GdiError('another command is using this agent plan') from exc
        yield directory


def persist_directory(directory, build):
    """Publish a fully fsynced directory atomically, before any upload permission."""
    if directory.exists():
        raise GdiError('agent output already exists without a reusable plan')
    temporary = Path(tempfile.mkdtemp(prefix='.' + directory.name + '-', dir=directory.parent))
    try:
        build(temporary)
        for path in temporary.rglob('*'):
            if path.is_file():
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
        os.rename(temporary, directory)
        fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def file_plan(source, relative, target):
    folder, _, name = target.rpartition('/')
    return {'local_path': relative, 'target_folder': folder, 'target_name': name,
            'sha256': file_digest(source), 'bytes': Path(source).stat().st_size}


def validate_files(directory, files):
    if not isinstance(files, dict):
        raise GdiError('invalid agent file plan')
    for item in files.values():
        if (not isinstance(item, dict) or set(item) != {'local_path', 'target_folder', 'target_name', 'sha256', 'bytes'}
                or not hex_value(item['sha256'], 64) or type(item['bytes']) is not int or item['bytes'] < 0):
            raise GdiError('invalid agent file descriptor')
        path = local_file(directory, item['local_path'])
        if path.stat().st_size != item['bytes'] or file_digest(path) != item['sha256']:
            raise GdiError('saved agent artifact checksum/size mismatch')


def save_plan(directory, value):
    atomic_write(directory / 'plan.json', encode(value))


def load_plan(directory, expected_id, kind='publication'):
    value = decode(metadata_file(local_file(directory, 'plan.json')))
    if value.get('plan_version') != 1 or type(value.get('plan_version')) is not int or value.get('kind') != kind:
        raise GdiError('unsupported agent plan')
    if not hex_value(expected_id, 32) or value.get('repository_id') != expected_id:
        raise GdiError('agent plan repository ID mismatch')
    from .inbox import relative_repository
    relative_repository(value.get('repository_path'))
    branch_directory(value.get('ref'))
    validate_files(directory, value.get('files'))
    return value


def publication_plan(directory, expected_id):
    value = load_plan(directory, expected_id)
    keys = {'plan_version', 'kind', 'repository_id', 'repository_path', 'ref', 'head',
            'publication_id', 'base_chain', 'base_head', 'files', 'state', 'checked_snapshot'}
    if (set(value) != keys or value['state'] not in ('prepared', 'bundle_checked', 'accepted') or
            not hex_value(value['head'], 40) or not hex_value(value['publication_id'], 64) or
            not isinstance(value['base_chain'], list) or
            any(not hex_value(identity, 64) for identity in value['base_chain']) or
            len(value['base_chain']) != len(set(value['base_chain'])) or
            (not hex_value(value['base_head'], 40) if value['base_chain'] else value['base_head'] is not None) or
            set(value['files']) != {'bundle', 'manifest', 'notification'}):
        raise GdiError('invalid publication plan')
    raw = metadata_file(local_file(directory, 'publication.json'))
    data = validate_manifest(raw, value['publication_id'], expected_id, value['ref'])
    previous = value['base_chain'][-1] if value['base_chain'] else None
    if (digest(raw) != value['publication_id'] or encode(data) != raw or
            any(data.get(key) != value[key] for key in ('repository_id', 'ref', 'head')) or
            data.get('previous') != previous or data.get('bundle_kind') != 'full'):
        raise GdiError('saved publication metadata differs from plan')
    paths = {'bundle': 'bundles/' + data['bundle_sha256'] + '.bundle',
             'manifest': 'branches/' + branch_directory(value['ref']) + '/' + value['publication_id'] + '.json'}
    event = notification(expected_id, value['repository_path'], data, value['publication_id'])
    if metadata_file(local_file(directory, 'notification.json')) != encode(event):
        raise GdiError('saved notification differs from publication')
    paths['notification'] = 'inbox/' + filename(event)
    for key, local in (('bundle', 'source.bundle'), ('manifest', 'publication.json'), ('notification', 'notification.json')):
        target = paths[key] if key == 'notification' else value['repository_path'] + '/' + paths[key]
        if value['files'][key] != file_plan(local_file(directory, local), local, target):
            raise GdiError('saved publication destination differs from plan')
    return value, data


def verdict(directory, value):
    # A previous attempt may have lost its acknowledgement at the directory
    # fsync after rename. Reuse the same bytes, and make that publication durable.
    for folder in (directory, directory.parent):
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    state = value['state']
    publication = value['kind'] == 'publication'
    next_step = ({'prepared': 'upload_bundle', 'bundle_checked': 'upload_manifest', 'accepted': 'publication_accepted'}
                 if publication else {'prepared': 'upload_request_then_ready', 'request_checked': 'upload_inbox', 'accepted': 'ci_request_accepted'})[state]
    return {**value, 'plan_path': str(directory / 'plan.json'),
            'files': {key: {**item, 'local_path': str(directory / item['local_path'])} for key, item in value['files'].items()},
            'next_step': next_step,
            'upload_order': ['bundle', 'manifest', 'notification'] if publication else ['request', 'ready', 'inbox'],
            'freshness': 'connector_provided_snapshot',
            **({'safe_to_upload_bundle': state != 'accepted', 'safe_to_upload_manifest': state == 'bundle_checked',
                'safe_to_upload_notification': state == 'accepted',
                'expected_previous_publication': value['base_chain'][-1] if value['base_chain'] else None,
                'expected_new_tip': value['publication_id']} if publication else
               {'safe_to_upload_request': state == 'prepared', 'safe_to_upload_ready': state == 'prepared',
                'safe_to_upload_inbox': state == 'request_checked'})}


def same_route(snapshot, value):
    if snapshot.repository_path != value['repository_path'] or snapshot.ref != value['ref']:
        raise GdiError('snapshot route/ref differs from saved plan')


def same_base(snapshot, value):
    same_route(snapshot, value)
    if [identity for identity, _ in snapshot.chain] != value['base_chain']:
        raise GdiError('remote changed: prepare a new plan using a fresh snapshot after reconciling history')
    if snapshot.chain and snapshot.chain[-1][1]['head'] != value['base_head']:
        raise GdiError('publication base HEAD changed')


def prepare(git, snapshot_path, expected_id, output, branch=None):
    ref = git.ref(branch if branch is not None else git.branch())
    snapshot = Snapshot(snapshot_path, expected_id, ref=ref)
    head = git.oid(ref)
    if head is None:
        raise GdiError('branch has no committed HEAD')
    with plan_lock(output) as directory:
        if (directory / 'plan.json').exists():
            value, _ = publication_plan(directory, expected_id)
            same_route(snapshot, value)
            if value['head'] != head:
                raise GdiError('HEAD changed; preserve this plan and use a new output directory')
            ids = [identity for identity, _ in snapshot.chain]
            if ids not in (value['base_chain'], value['base_chain'] + [value['publication_id']]):
                raise GdiError('remote changed; prepare a new plan after reconciling history')
            return verdict(directory, value)
        tip = snapshot.chain[-1] if snapshot.chain else None
        if tip and head == tip[1]['head']:
            git.check_payload(head)
            return {'state': 'already_published', 'repository_id': expected_id, 'ref': ref,
                    'head': head, 'publication_id': tip[0], 'files': {}, 'next_step': 'none',
                    'safe_to_upload_bundle': False, 'safe_to_upload_manifest': False}
        def build(temporary):
            publication = prepare_publication(git, expected_id, ref, head, temporary, previous=tip)
            with verify_bundle(publication.bundle, publication.data, ref, previous=tip[1] if tip else None):
                pass
            event = notification(expected_id, snapshot.repository_path, publication.data, publication.publication_id)
            atomic_write(temporary / 'notification.json', encode(event))
            value = {'plan_version': 1, 'kind': 'publication', 'repository_id': expected_id,
                     'repository_path': snapshot.repository_path, 'ref': ref, 'head': head,
                     'publication_id': publication.publication_id,
                     'base_chain': [identity for identity, _ in snapshot.chain], 'base_head': tip[1]['head'] if tip else None,
                     'files': {'bundle': file_plan(publication.bundle, 'source.bundle', snapshot.repository_path + '/' + publication.bundle_relative),
                               'manifest': file_plan(publication.manifest, 'publication.json', snapshot.repository_path + '/' + publication.manifest_relative),
                               'notification': file_plan(temporary / 'notification.json', 'notification.json', 'inbox/' + filename(event))},
                     'state': 'prepared', 'checked_snapshot': None}
            save_plan(temporary, value)
        persist_directory(directory, build)
        value, _ = publication_plan(directory, expected_id)
        return verdict(directory, value)


def check(output, snapshot_path, expected_id):
    with plan_lock(output) as directory:
        value, data = publication_plan(directory, expected_id)
        snapshot = Snapshot(snapshot_path, expected_id, ref=value['ref'])
        same_base(snapshot, value)
        if value['state'] == 'accepted':
            raise GdiError('publication was already accepted; use accept with a fresh snapshot')
        bundle = snapshot.require_file('bundles/' + data['bundle_sha256'] + '.bundle', data['bundle_sha256'], data['bundle_bytes'])
        with verify_bundle(bundle, data, value['ref'], previous={'head': value['base_head']} if value['base_head'] else None):
            pass
        value.update(state='bundle_checked', checked_snapshot=snapshot.fingerprint)
        save_plan(directory, value)
        return verdict(directory, value)


def accept(output, snapshot_path, expected_id):
    with plan_lock(output) as directory:
        value, data = publication_plan(directory, expected_id)
        snapshot = Snapshot(snapshot_path, expected_id, ref=value['ref'])
        same_route(snapshot, value)
        if [identity for identity, _ in snapshot.chain] != value['base_chain'] + [value['publication_id']]:
            raise GdiError('publication is missing or remote advanced/conflicted; no acceptance')
        path = 'branches/' + branch_directory(value['ref']) + '/' + value['publication_id'] + '.json'
        if snapshot.read(path) != metadata_file(local_file(directory, 'publication.json')):
            raise GdiError('uploaded manifest differs from saved bytes')
        bundle = snapshot.require_file('bundles/' + data['bundle_sha256'] + '.bundle', data['bundle_sha256'], data['bundle_bytes'])
        with verify_bundle(bundle, data, value['ref'], previous={'head': value['base_head']} if value['base_head'] else None):
            pass
        value.update(state='accepted', checked_snapshot=snapshot.fingerprint)
        save_plan(directory, value)
        return verdict(directory, value)


def clone(snapshot_path, expected_id, destination):
    """Restore a new worktree using only downloaded checkpoint/prerequisite bundles."""
    from .exchange import Exchange
    snapshot = Snapshot(snapshot_path, expected_id)
    if not snapshot.chain:
        raise GdiError('snapshot has no completed publication')
    with plan_lock(destination) as directory:
        if directory.exists():
            raise GdiError('clone destination must not exist; existing files are preserved')
        def build(temporary):
            git = Git(temporary, isolated=True)
            git.call('init', '-q', '--object-format=sha1', '--template=')
            git.ref(snapshot.ref[11:])
            cache = Exchange(git).restore(snapshot, expected_id, snapshot.chain)
            head = snapshot.chain[-1][1]['head']
            git.import_objects(cache.path, head)
            git.call('checkout', '-q', '-b', snapshot.ref[11:], head)
        persist_directory(directory, build)
        return {'repository_id': expected_id, 'ref': snapshot.ref, 'head': snapshot.chain[-1][1]['head'],
                'publication_id': snapshot.chain[-1][0], 'destination': str(directory)}
