"""One-file connector exchange. All functions here run offline except publish_export."""

from contextlib import contextmanager
from pathlib import Path
import re
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile

from .agent import persist_directory, plan_lock
from .ci_protocol import atomic_write
from .git import GdiError, Git
from .inbox import relative_repository
from .publication import (decode, digest, encode, file_digest, hex_value,
                          prepare_publication, verify_bundle)


VERSION = 1
PUSH_NAME = re.compile(r'agent-push-([0-9a-f]{32})-([0-9a-f]{32})-([0-9a-f]{64})-([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.zip')
BASE_FILE = 'agent-base.json'


def export_name(repository_id, ref):
    return 'export-' + repository_id + '-' + digest(ref.encode('utf-8')) + '.zip'


def receipt_path(repository_id, request_id):
    return 'agent/receipt-' + repository_id + '-' + request_id + '.json'


def validate_metadata(value, expected_id, kind):
    common = {'agent_exchange_version', 'kind', 'repository_id', 'repository_path', 'ref', 'head',
              'bundle_sha256', 'bundle_bytes'}
    extra = {'publication_id', 'worker_id'} if kind == 'export' else {'request_id', 'base_head', 'base_publication', 'full', 'worker_id'}
    if (set(value) != common | extra or type(value.get('agent_exchange_version')) is not int
            or value['agent_exchange_version'] != VERSION or value['kind'] != kind
            or not hex_value(expected_id, 32) or value['repository_id'] != expected_id
            or not hex_value(value['head'], 40) or not hex_value(value['bundle_sha256'], 64)
            or type(value['bundle_bytes']) is not int or value['bundle_bytes'] <= 0):
        raise GdiError('invalid agent exchange package metadata or repository identity')
    relative_repository(value['repository_path'])
    from .ci_protocol import identifier
    if value['worker_id'] is not None:
        identifier(value['worker_id'])
    from .branches import branch_directory
    branch_directory(value['ref'])
    if kind == 'export':
        if not hex_value(value['publication_id'], 64):
            raise GdiError('invalid exported publication identity')
    elif (value['worker_id'] is None or not hex_value(value['request_id'], 32) or not hex_value(value['base_head'], 40)
          or not hex_value(value['base_publication'], 64) or type(value['full']) is not bool):
        raise GdiError('invalid agent push base or request identity')
    return value


def write_package(git, repository_id, ref, directory, metadata):
    publication = prepare_publication(git, repository_id, ref, metadata['head'], directory)
    value = {'agent_exchange_version': VERSION, 'repository_id': repository_id, 'ref': ref,
             'bundle_sha256': publication.data['bundle_sha256'],
             'bundle_bytes': publication.data['bundle_bytes'], **metadata}
    validate_metadata(value, repository_id, value['kind'])
    path = Path(directory) / 'package.zip'
    # Git packs are already compressed. Fixed metadata makes retry artifacts stable.
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_STORED) as archive:
        entry = zipfile.ZipInfo('metadata.json', date_time=(1980, 1, 1, 0, 0, 0))
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, encode(value))
        entry = zipfile.ZipInfo('source.bundle', date_time=(1980, 1, 1, 0, 0, 0))
        entry.external_attr = 0o100644 << 16
        with publication.bundle.open('rb') as source, archive.open(entry, 'w', force_zip64=True) as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
    return path, value


@contextmanager
def read_package(path, expected_id, kind):
    """Read only the two defined entries, without trusting names or extracting paths."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise GdiError('agent package must be a regular file')
    with tempfile.TemporaryDirectory(prefix='gdi-agent-package-') as temporary:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) != 2 or {entry.filename for entry in entries} != {'metadata.json', 'source.bundle'}:
                    raise GdiError('agent package must contain exactly metadata.json and source.bundle')
                for entry in entries:
                    mode = entry.external_attr >> 16
                    if entry.is_dir() or stat.S_ISLNK(mode) or entry.compress_type != zipfile.ZIP_STORED:
                        raise GdiError('unsupported agent package entry')
                if archive.getinfo('metadata.json').file_size > 1024 * 1024:
                    raise GdiError('agent package metadata exceeds 1 MiB')
                value = validate_metadata(decode(archive.read('metadata.json')), expected_id, kind)
                if archive.getinfo('source.bundle').file_size != value['bundle_bytes']:
                    raise GdiError('agent package bundle size mismatch')
                bundle = Path(temporary) / 'source.bundle'
                with archive.open('source.bundle') as source, bundle.open('xb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
        except GdiError:
            raise
        except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise GdiError('invalid or incomplete agent package') from exc
        verification = {'bundle_kind': 'full', 'head': value['head'], 'bundle_sha256': value['bundle_sha256'],
                        'bundle_bytes': value['bundle_bytes'], 'prerequisites': []}
        previous = {'head': value['base_head']} if kind == 'push' else None
        # Only ZIP decoding errors are adapted above. Verification and caller
        # failures must keep their original type and actionable diagnostics.
        with verify_bundle(bundle, verification, value['ref'], previous=previous) as quarantine:
            yield value, quarantine


def base_record(value):
    return {key: value[key] for key in ('repository_id', 'repository_path', 'ref', 'head', 'publication_id', 'worker_id')}


def pull(path, repository_id, *, destination=None, repo=None):
    if (destination is None) == (repo is None):
        raise GdiError('choose --destination for a clone or --repo for an existing checkout')
    with read_package(path, repository_id, 'export') as (value, quarantine):
        if destination is not None:
            with plan_lock(destination) as directory:
                def build(temporary):
                    git = Git(temporary, isolated=True)
                    git.call('init', '--quiet', '--object-format=sha1', '--template=')
                    git.import_objects(quarantine, value['head'])
                    git.call('checkout', '--quiet', '-b', value['ref'][11:], value['head'])
                    from .repository import initialize
                    git = initialize(temporary)
                    atomic_write(git.gdi_dir() / BASE_FILE, encode(base_record(value)))
                persist_directory(directory, build)
            head = value['head']
        else:
            git = Git.discover(repo)
            with git.lock():
                base_path = git.gdi_dir() / BASE_FILE
                if base_path.exists() and decode(base_path.read_bytes()).get('repository_id') != repository_id:
                    raise GdiError('existing agent checkout belongs to another repository')
                before = git.snapshot()
                git.require_clean(before)
                if git.ref(before[0]) != value['ref']:
                    raise GdiError('agent pull does not switch branches; select the intended branch explicitly')
                git.import_objects(quarantine, value['head'])
                if before[1] is None or git.ancestor(before[1], value['head']):
                    git.call('merge', '--ff-only', '--no-autostash', '--no-stat', value['head'])
                elif not git.ancestor(value['head'], before[1]):
                    raise GdiError('agent pull diverged; reconcile history explicitly')
                atomic_write(git.gdi_dir() / BASE_FILE, encode(base_record(value)))
                head = git.oid('HEAD')
    return {'state': 'PULLED', **base_record(value), 'local_head': head,
            'repo': str(Path(destination if destination is not None else repo).absolute()),
            'connector_actions': [], 'snapshot_required': False}


def load_push(directory, repository_id):
    from .snapshot import local_file, metadata_file
    plan = decode(metadata_file(local_file(directory, 'plan.json')))
    if (set(plan) != {'agent_exchange_version', 'request', 'package_sha256', 'package_name'}
            or type(plan['agent_exchange_version']) is not int
            or plan['agent_exchange_version'] != VERSION or not hex_value(plan['package_sha256'], 64)):
        raise GdiError('invalid saved agent push plan')
    value = validate_metadata(plan['request'], repository_id, 'push')
    match = PUSH_NAME.fullmatch(plan['package_name'])
    if not match or match.groups() != (repository_id, value['request_id'], plan['package_sha256'], value['worker_id']):
        raise GdiError('agent push filename differs from its request')
    package = local_file(directory, 'package.zip')
    if file_digest(package) != plan['package_sha256']:
        raise GdiError('saved agent push package changed')
    return plan


def push(git, repository_id, output, *, full=False, receipt=None):
    from .snapshot import metadata_file
    with git.lock(), plan_lock(output) as directory:
        if receipt is not None:
            plan = load_push(directory, repository_id)
            request = plan['request']
            value = decode(metadata_file(Path(receipt)))
            expected = {'agent_exchange_version': VERSION, 'repository_id': repository_id,
                        'request_id': request['request_id'], 'package_sha256': plan['package_sha256'],
                        'ref': request['ref'], 'head': request['head'], 'worker_id': request['worker_id']}
            if (type(value.get('agent_exchange_version')) is not int or
                    any(value.get(key) != item for key, item in expected.items())):
                raise GdiError('agent receipt does not match the saved push')
            if value.get('state') not in ('PUBLISHED', 'CONFLICT', 'ERROR'):
                raise GdiError('invalid agent publication receipt state')
            if value['state'] == 'PUBLISHED':
                if not hex_value(value.get('publication_id'), 64):
                    raise GdiError('published receipt has no publication ID')
                current = decode(metadata_file(git.gdi_dir() / BASE_FILE))
                expected_base = base_record({**request, 'head': request['base_head'],
                                             'publication_id': request['base_publication']})
                accepted_base = base_record({**request, 'publication_id': value['publication_id']})
                if current not in (expected_base, accepted_base) or git.ref(git.branch()) != request['ref']:
                    raise GdiError('checkout base or branch changed since preparing this push; receipt retained')
                atomic_write(git.gdi_dir() / BASE_FILE, encode({**base_record({**request,
                    'publication_id': value['publication_id']})}))
            return value
        before = git.snapshot()
        git.require_clean(before)
        if (directory / 'plan.json').exists():
            plan = load_push(directory, repository_id)
            if plan['request']['head'] != before[1] or plan['request']['ref'] != git.ref(before[0]):
                raise GdiError('HEAD or branch changed; preserve this push plan and use a new output directory')
        else:
            base = decode(metadata_file(git.gdi_dir() / BASE_FILE))
            if (set(base) != {'repository_id', 'repository_path', 'ref', 'head', 'publication_id', 'worker_id'}
                    or base.get('repository_id') != repository_id or base.get('ref') != git.ref(before[0])
                    or not hex_value(base.get('head'), 40) or not hex_value(base.get('publication_id'), 64)):
                raise GdiError('agent push requires a matching base from agent pull')
            if not git.ancestor(base['head'], before[1]):
                raise GdiError('agent push is not a fast-forward from its downloaded base')
            if before[1] == base['head'] and not full:
                return {'state': 'ALREADY_PUBLISHED', **base, 'connector_actions': []}
            if base['worker_id'] is None:
                raise GdiError('export has no publisher route; operator must refresh it with a configured worker')
            def build(temporary):
                metadata = {'kind': 'push', 'repository_path': base['repository_path'], 'head': before[1],
                            'request_id': uuid.uuid4().hex, 'base_head': base['head'],
                            'base_publication': base['publication_id'], 'full': bool(full), 'worker_id': base['worker_id']}
                package, request = write_package(git, repository_id, base['ref'], temporary, metadata)
                checksum = file_digest(package)
                name = 'agent-push-' + repository_id + '-' + request['request_id'] + '-' + checksum + '-' + request['worker_id'] + '.zip'
                atomic_write(temporary / 'plan.json', encode({'agent_exchange_version': VERSION,
                    'request': request, 'package_sha256': checksum, 'package_name': name}))
            persist_directory(directory, build)
            plan = load_push(directory, repository_id)
        request = plan['request']
        return {'state': 'PREPARED', 'request_id': request['request_id'], 'head': request['head'],
                'files': {'package': {'local_path': str(directory / 'package.zip'), 'target_folder': 'inbox',
                    'target_name': plan['package_name'], 'sha256': plan['package_sha256'],
                    'bytes': (directory / 'package.zip').stat().st_size}},
                'connector_actions': ['upload_package'], 'read_back_required': False,
                'receipt_path': request['repository_path'] + '/' + receipt_path(repository_id, request['request_id']),
                'next_step': 'upload once; wait locally, then fetch the receipt once; do not poll Drive'}


def publish_export(exchange, name, repository_id, ref, head, publication_id, *, force=False, worker_id=None):
    """Host-side only: prepare the single file downloaded by agent pull."""
    from .local_config import repository_path
    settings = exchange.remote(name)
    root = settings.get('inbox_root')
    if root is None:
        return
    if worker_id is None:
        worker_id = getattr(exchange, 'publisher_worker_id', None)
    if worker_id is None:
        config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'gdi/worker.json'
        if config.exists():
            try:
                value = json.loads(config.read_text())
            except (OSError, ValueError) as exc:
                raise GdiError('cannot read publisher worker config: ' + str(config)) from exc
            if not isinstance(value, dict):
                raise GdiError('publisher worker config must be an object')
            if value.get('config_version') == 2 and value.get('remote_url') == root:
                worker_id = value.get('worker_id')
    key = {'url': settings['url'], 'repository_id': repository_id, 'ref': ref,
           'head': head, 'publication_id': publication_id, 'worker_id': worker_id}
    receipt = exchange.git.gdi_dir() / 'agent-exports' / (digest(encode(key)) + '.json')
    if not force and receipt.exists() and receipt.read_bytes() == encode(key):
        return
    with tempfile.TemporaryDirectory(prefix='gdi-agent-export-') as temporary:
        path, _ = write_package(exchange.git, repository_id, ref, temporary,
            {'kind': 'export', 'repository_path': repository_path(root, settings['url']),
             'head': head, 'publication_id': publication_id, 'worker_id': worker_id})
        transport, expected_id = exchange.connect(name)
        if expected_id != repository_id:
            raise GdiError('repository changed before publishing agent export')
        if not getattr(transport, 'creates_parents', False):
            transport.mkdir('agent')
        transport.update_agent_export(path, 'agent/' + export_name(repository_id, ref))
        atomic_write(receipt, encode(key))
