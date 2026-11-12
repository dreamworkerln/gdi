"""Explicit, resumable v3 -> v4 conversion; no legacy data is deleted."""

from pathlib import Path
import tempfile

from . import packet
from .agent import persist_directory
from .ci_protocol import atomic_write
from .git import GdiError, Git
from .publication import decode, encode, file_digest, validate_repository


def upgrade(exchange, name, *, quiescent=False, all_clients_updated=False):
    if not quiescent or not all_clients_updated:
        raise GdiError('remote upgrade requires --quiescent --all-clients-updated')
    from .exchange import LegacyExchange
    from .local_config import load, save, repository_path
    settings = exchange.remote(name)
    identity = settings['repository_id']
    transport = exchange.transport_factory(settings['url'])
    old = validate_repository(decode(transport.read('repository.json')))
    if old['repository_id'] != identity:
        raise GdiError('repository ID mismatch; do not migrate a replaced remote')
    if old['version'] == 4:
        state = load(exchange.git)
        state['remotes'][name]['protocol_version'] = 4
        save(exchange.git, state)
        return {'repository_id': identity, 'version': 4, 'state': 'already_upgraded'}
    legacy = LegacyExchange(exchange.git, exchange.transport_factory)
    transport.repository = old
    from .branches import directory_ref
    from .work_lifecycle import records
    retired = {value['ref'] for value in records(transport, identity, 'retired').values()}
    refs = sorted(directory_ref(item['Path']) for item in transport.list('branches') if item['IsDir'])
    completed = []
    for ref in refs:
        if ref in retired:
            continue
        chain = legacy.publications(transport, identity, ref)
        if not chain:
            continue
        source_id, source = chain[-1]
        key = packet.digest(encode({'url': settings['url'], 'ref': ref, 'publication': source_id}))
        directory = exchange.git.gdi_dir() / 'packet-upgrade' / key
        if not directory.exists():
            def prepare(temporary):
                owner = temporary / 'git'
                owner.mkdir()
                git = Git(owner, isolated=True)
                git.call('init', '--quiet', '--bare', '--object-format=sha1', '--template=')
                if exchange.git.has_commit(source['head']):
                    git.import_objects(exchange.git.path, source['head'])
                else:
                    cache = legacy.restore(transport, identity, chain)
                    git.import_objects(cache.path, source['head'])
                git.call('update-ref', ref, source['head'])
                _, value = packet.build(git, identity, ref, temporary,
                    repository_path=repository_path(settings['inbox_root'], settings['url']) if settings['inbox_root'] else None,
                    worker_id=packet.publisher_id(settings['inbox_root']))
                atomic_write(temporary / 'plan.json', encode({'pointer': value, 'phase': 'PREPARED'}))
            directory.parent.mkdir(parents=True, exist_ok=True)
            persist_directory(directory, prepare)
        plan_path = directory / 'plan.json'
        plan = decode(plan_path.read_bytes())
        value = packet.pointer(plan['pointer'], identity, ref)
        if file_digest(directory / 'packet.zip') != value['publication_id']:
            raise GdiError('saved migration packet changed')
        if plan['phase'] == 'UPLOADING' and transport.packet_exists(packet.packet_path(value['publication_id'])):
            plan['phase'] = 'UPLOADED'
        if plan['phase'] in ('PREPARED', 'UPLOADING'):
            plan['phase'] = 'UPLOADING'
            atomic_write(plan_path, encode(plan))
            transport.upload(directory / 'packet.zip', packet.packet_path(value['publication_id']))
            plan['phase'] = 'UPLOADED'
            atomic_write(plan_path, encode(plan))
        if plan['phase'] != 'PUBLISHED':
            packet.write_head(transport, value)
            plan['phase'] = 'PUBLISHED'
            atomic_write(plan_path, encode(plan))
        completed.append({'ref': ref, 'head': source['head'], 'publication_id': value['publication_id']})
    with tempfile.TemporaryDirectory(prefix='gdi-upgrade-') as temporary:
        path = Path(temporary) / 'repository.json'
        path.write_bytes(encode({'version': 4, 'repository_id': identity, 'object_format': 'sha1', 'work_version': 1}))
        for directory in ('heads', 'packets', 'work/imports', 'work/retired'):
            transport.mkdir(directory)
        transport.update_repository(path)
    state = load(exchange.git)
    state['remotes'][name]['protocol_version'] = 4
    save(exchange.git, state)
    return {'repository_id': identity, 'version': 4, 'state': 'upgraded', 'branches': completed,
            'legacy_data_deleted': False}
