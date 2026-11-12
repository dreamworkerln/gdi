"""Offline client of the same protocol-v4 packet used by the operator CLI."""

from pathlib import Path
import re

from . import packet
from .agent import persist_directory, plan_lock
from .ci_protocol import atomic_write
from .git import GdiError, Git
from .publication import decode, encode, file_digest
from .snapshot import local_file, metadata_file

VERSION = 2
PUSH_NAME = re.compile(r'agent-push-([0-9a-f]{32})-([0-9a-f]{32})-([0-9a-f]{64})-([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.zip')
BASE_FILE = 'agent-base.json'


def receipt_path(repository_id, request_id):
    return 'agent/receipt-' + repository_id + '-' + request_id + '.json'


def pull(path, repository_id, *, head_path, destination=None, repo=None):
    if (destination is None) == (repo is None):
        raise GdiError('choose --destination or --repo')
    expected = packet.pointer(decode(metadata_file(head_path)), repository_id)
    with packet.read(path, repository_id, expected) as (value, quarantine):
        if destination is not None:
            with plan_lock(destination) as directory:
                def build(temporary):
                    git = Git(temporary, isolated=True)
                    git.call('init', '--quiet', '--object-format=sha1', '--template=')
                    git.import_objects(quarantine, value['metadata']['head'])
                    git.call('checkout', '--quiet', '-b', value['ref'][11:], value['metadata']['head'])
                    from .repository import initialize
                    git = initialize(temporary)
                    atomic_write(git.gdi_dir() / BASE_FILE, encode(value))
                persist_directory(directory, build)
            git = Git.discover(directory)
        else:
            git = Git.discover(repo)
            with git.lock():
                base_path = git.gdi_dir() / BASE_FILE
                if base_path.exists() and decode(metadata_file(base_path)).get('repository_id') != repository_id:
                    raise GdiError('existing checkout belongs to another repository')
                before = git.snapshot()
                git.require_clean(before)
                if git.ref(before[0]) != value['ref']:
                    raise GdiError('agent pull does not switch branches')
                git.import_objects(quarantine, value['metadata']['head'])
                head = value['metadata']['head']
                if before[1] is None or git.ancestor(before[1], head):
                    git.call('merge', '--ff-only', '--no-autostash', '--no-stat', head)
                elif not git.ancestor(head, before[1]):
                    raise GdiError('agent pull diverged; reconcile history explicitly')
                atomic_write(base_path, encode(value))
    return {'state': 'PULLED', 'repository_id': repository_id, 'ref': value['ref'],
            'head': value['metadata']['head'], 'local_head': git.oid('HEAD'),
            'publication_id': value['publication_id'], 'connector_actions': []}


def load_push(directory, repository_id):
    value = decode(metadata_file(local_file(directory, 'plan.json')))
    if (set(value) != {'agent_exchange_version', 'pointer', 'base', 'filename'}
            or type(value['agent_exchange_version']) is not int or value['agent_exchange_version'] != VERSION):
        raise GdiError('invalid saved agent push plan')
    target = packet.pointer(value['pointer'], repository_id)
    packet.pointer(value['base'], repository_id)
    data = target['metadata']
    name = PUSH_NAME.fullmatch(value['filename'])
    if (not name or name.groups() != (repository_id, data['request_id'], target['publication_id'], data['worker_id'])
            or file_digest(local_file(directory, 'packet.zip')) != target['publication_id']):
        raise GdiError('saved packet or request identity changed')
    return value


def push(git, repository_id, output, *, full=False, receipt=None, worker_id=None):
    with git.lock(), plan_lock(output) as directory:
        if receipt is not None:
            plan = load_push(directory, repository_id)
            target, base = plan['pointer'], plan['base']
            data = target['metadata']
            value = decode(metadata_file(receipt))
            expected = {'agent_exchange_version': VERSION, 'repository_id': repository_id,
                        'request_id': data['request_id'], 'package_sha256': target['publication_id'],
                        'worker_id': data['worker_id'], 'ref': data['ref'], 'head': data['head']}
            if (type(value.get('agent_exchange_version')) is not int or
                    any(value.get(key) != item for key, item in expected.items())):
                raise GdiError('receipt does not match saved push')
            if value.get('state') not in ('PUBLISHED', 'CONFLICT', 'ERROR'):
                raise GdiError('invalid publication receipt state')
            if value['state'] == 'PUBLISHED':
                accepted = packet.pointer(value.get('pointer'), repository_id, target['ref'])
                if accepted != target or value.get('publication_id') != target['publication_id']:
                    raise GdiError('receipt changed the published packet')
                current = packet.pointer(decode(metadata_file(git.gdi_dir() / BASE_FILE)), repository_id)
                if current not in (base, accepted) or git.ref(git.branch()) != target['ref']:
                    raise GdiError('checkout base or branch changed since preparing this push')
                atomic_write(git.gdi_dir() / BASE_FILE, encode(accepted))
            return value
        before = git.snapshot()
        git.require_clean(before)
        ref = git.ref(before[0])
        if (directory / 'plan.json').exists():
            plan = load_push(directory, repository_id)
            if plan['pointer']['metadata']['head'] != before[1] or plan['pointer']['ref'] != ref:
                raise GdiError('HEAD or branch changed; use a new output directory')
        else:
            base = packet.pointer(decode(metadata_file(git.gdi_dir() / BASE_FILE)), repository_id)
            previous = base
            if base['ref'] != ref:
                from . import work
                if not ref.startswith(work.PREFIX):
                    raise GdiError('agent push requires the selected branch base')
                binding = work.validate(git, ref, before[1], repository_id)
                if binding['target'] != base['ref'] or binding['base'] != base['metadata']['head']:
                    raise GdiError('work branch does not match its assigned target base')
                previous = None
            if not git.ancestor(base['metadata']['head'], before[1]):
                raise GdiError('push is not a fast-forward from downloaded base')
            if base['metadata']['head'] == before[1] and previous is not None:
                return {'state': 'ALREADY_PUBLISHED', 'publication_id': base['publication_id'], 'connector_actions': []}
            selected = worker_id or base['metadata']['worker_id']
            if selected is None:
                raise GdiError('choose a compatible worker from the cached registry using --worker')
            def prepare(temporary):
                _, target = packet.build(git, repository_id, ref, temporary, previous=previous,
                    repository_path=base['metadata']['repository_path'], worker_id=selected)
                data = target['metadata']
                filename = 'agent-push-' + repository_id + '-' + data['request_id'] + '-' + target['publication_id'] + '-' + selected + '.zip'
                atomic_write(temporary / 'plan.json', encode({'agent_exchange_version': VERSION,
                    'pointer': target, 'base': base, 'filename': filename}))
            persist_directory(directory, prepare)
            plan = load_push(directory, repository_id)
        target = plan['pointer']
        data = target['metadata']
        if data['repository_path'] is None:
            raise GdiError('agent push requires a repository below a shared inbox root')
        return {'state': 'PREPARED', 'request_id': data['request_id'], 'head': data['head'],
                'files': {'package': {'local_path': str(directory / 'packet.zip'), 'target_folder': 'inbox',
                    'target_name': plan['filename'], 'sha256': target['publication_id'], 'bytes': target['packet_bytes']}},
                'connector_actions': ['upload_package'], 'read_back_required': False,
                'receipt_path': data['repository_path'] + '/' + receipt_path(repository_id, data['request_id']),
                'next_step': 'upload once; fetch receipt after a local wait; do not poll Drive'}
