"""Bounded live metadata with immutable, content-addressed publication archives."""

from pathlib import Path
import re
import tempfile

from .branches import branch_directory
from .git import GdiError
from .publication import (PreparedPublication, decode, digest, encode, hex_value, validate_manifest,
                          validate_publications, validate_repository, verify_bundle)


CAPABILITY = 'publication_checkpoint_version'
CHECKPOINT = 'checkpoint.json'
ARCHIVE = 'publication-archive/'
BLOCK_LIMIT = 512 * 1024


class PublicationChain(list):
    def __init__(self, values=(), *, checkpoint=None, residue=(), raw_records=None):
        super().__init__(values)
        self.checkpoint = checkpoint
        self.residue = tuple(residue)
        self.raw_records = raw_records or {}


def enabled(repository):
    return repository.get(CAPABILITY) == 1


def marker_path(ref):
    return 'branches/' + branch_directory(ref) + '/' + CHECKPOINT


def archive_path(identity):
    if not hex_value(identity, 64):
        raise GdiError('invalid publication archive ID')
    return ARCHIVE + identity + '.json'


def validate_checkpoint(value, repository_id, ref):
    keys = {'checkpoint_version', 'repository_id', 'ref', 'root', 'previous',
            'previous_head', 'history', 'archived_count'}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value['checkpoint_version']) is not int or value['checkpoint_version'] != 1
            or value['repository_id'] != repository_id or value['ref'] != ref
            or not hex_value(value['root'], 64)
            or type(value['archived_count']) is not int or value['archived_count'] < 0
            or (value['previous'] is not None and not hex_value(value['previous'], 64))
            or (value['previous_head'] is not None and not hex_value(value['previous_head'], 40))
            or (value['history'] is not None and not hex_value(value['history'], 64))
            or ((value['previous'] is None) != (value['previous_head'] is None))
            or ((value['archived_count'] == 0) != (value['previous'] is None))
            or ((value['history'] is None) != (value['previous'] is None))):
        raise GdiError('invalid publication checkpoint')
    return value


def read_block(read, identity, repository_id, ref):
    raw = read(archive_path(identity))
    if digest(raw) != identity:
        raise GdiError('publication archive SHA256 mismatch')
    block = decode(raw)
    if (set(block) != {'archive_version', 'repository_id', 'ref', 'previous', 'records'}
            or type(block['archive_version']) is not int or block['archive_version'] != 1
            or block['repository_id'] != repository_id or block['ref'] != ref
            or (block['previous'] is not None and not hex_value(block['previous'], 64))
            or not isinstance(block['records'], list) or not block['records']):
        raise GdiError('invalid publication archive block')
    records = []
    for entry in block['records']:
        if (not isinstance(entry, dict) or set(entry) != {'id', 'manifest'}
                or not hex_value(entry['id'], 64) or not isinstance(entry['manifest'], str)):
            raise GdiError('invalid archived publication')
        data = validate_manifest(entry['manifest'].encode('utf-8'), entry['id'], repository_id, ref)
        records.append((entry['id'], data, entry['manifest'].encode('utf-8')))
    if len({row[0] for row in records}) != len(records):
        raise GdiError('duplicate archived publication')
    for before, after in zip(records, records[1:]):
        if after[1]['previous'] != before[0]:
            raise GdiError('disconnected publication archive')
    return block['previous'], records


def archived_prefix(checkpoint, read):
    """Only the just-closed live segment, not the entire historical archive."""
    remaining = checkpoint['archived_count']
    identity, seen, chunks = checkpoint['history'], set(), []
    while remaining:
        if identity in seen or identity is None:
            raise GdiError('incomplete or cyclic publication checkpoint archive')
        seen.add(identity)
        identity, records = read_block(read, identity, checkpoint['repository_id'], checkpoint['ref'])
        if len(records) > remaining:
            raise GdiError('checkpoint archive count mismatch')
        remaining -= len(records)
        chunks.append(records)
    records = [row for chunk in reversed(chunks) for row in chunk]
    if records and (records[-1][0] != checkpoint['previous']
                    or records[-1][1]['head'] != checkpoint['previous_head']):
        raise GdiError('checkpoint predecessor differs from its archive')
    return records


def live_chain(repository_id, ref, listing, metadata, checkpoint=None, read=None):
    if checkpoint is None:
        values = validate_publications(repository_id, ref, listing, metadata)
        prefix = branch_directory(ref) + '/'
        return PublicationChain(values, raw_records={identity: metadata[prefix + identity + '.json']
                                                    for identity, _ in values})
    validate_checkpoint(checkpoint, repository_id, ref)
    prefix = branch_directory(ref) + '/'
    records, raw_records = {}, {}
    for item in listing:
        path = item['Path']
        if item['IsDir'] or not path.startswith(prefix) or path == prefix + CHECKPOINT:
            continue
        identity = path[len(prefix):-5] if path.endswith('.json') else ''
        if not hex_value(identity, 64) or path not in metadata or identity in records:
            raise GdiError('invalid or incomplete live publication metadata')
        raw = metadata[path]
        records[identity] = validate_manifest(raw, identity, repository_id, ref)
        raw_records[identity] = raw
    root = checkpoint['root']
    if root not in records:
        raise GdiError('checkpoint full publication is missing; retry snapshot')
    first = records[root]
    if first['bundle_kind'] != 'full' or first['previous'] != checkpoint['previous']:
        raise GdiError('checkpoint does not identify its declared full publication')
    children = {}
    for identity, data in records.items():
        parent = data['previous']
        if parent in children:
            raise GdiError('conflicting publications: concurrent push detected')
        children[parent] = identity
    active, seen, current = [], set(), root
    while current is not None:
        if current in seen:
            raise GdiError('cyclic live publication chain')
        data = records[current]
        if data['bundle_kind'] == 'incremental':
            base = data['base_publication']
            if base not in seen or records[base]['head'] != data['base_head']:
                raise GdiError('incremental base is outside the active checkpoint')
        seen.add(current)
        active.append((current, data))
        current = children.get(current)
    residue = set(records) - seen
    if residue:
        archived = {identity: raw for identity, _, raw in archived_prefix(checkpoint, read)}
        if any(archived.get(identity) != raw_records[identity] for identity in residue):
            raise GdiError('unarchived or conflicting publication outside active checkpoint')
    return PublicationChain(active, checkpoint=checkpoint, residue=sorted(residue), raw_records=raw_records)


def history_chain(chain, read):
    checkpoint = getattr(chain, 'checkpoint', None)
    if checkpoint is None:
        return chain
    identity, seen, chunks = checkpoint['history'], set(), []
    while identity is not None:
        if identity in seen:
            raise GdiError('cyclic publication archive')
        seen.add(identity)
        identity, records = read_block(read, identity, checkpoint['repository_id'], checkpoint['ref'])
        chunks.append(records)
    records = [row for chunk in reversed(chunks) for row in chunk]
    prefix = branch_directory(checkpoint['ref']) + '/'
    metadata = {prefix + identity + '.json': raw for identity, _, raw in records}
    for identity, data in chain:
        raw = getattr(chain, 'raw_records', {}).get(identity, encode(data))
        if digest(raw) != identity:
            raise GdiError('noncanonical live manifest cannot be reconstructed for history')
        path = prefix + identity + '.json'
        if path in metadata:
            raise GdiError('publication occurs in archive and live chain')
        metadata[path] = raw
    listing = [{'Path': path, 'IsDir': False} for path in metadata]
    full = validate_publications(checkpoint['repository_id'], checkpoint['ref'], listing, metadata)
    if full[-len(chain):] != list(chain):
        raise GdiError('checkpoint archive is not a prefix of the active chain')
    return PublicationChain(full, raw_records={path[len(prefix):-5]: raw for path, raw in metadata.items()})


def previous_metadata(chain, data):
    previous = dict(chain).get(data['previous'])
    checkpoint = getattr(chain, 'checkpoint', None)
    if previous is None and checkpoint and data['previous'] == checkpoint['previous']:
        return {'head': checkpoint['previous_head']} if checkpoint['previous_head'] else None
    return previous


def prepare(chain, publication, read, directory):
    """Preserve exact old manifests in bounded linked archive blocks before rotation."""
    data, root = publication.data, Path(directory)
    checkpoint = getattr(chain, 'checkpoint', None)
    previous = checkpoint['history'] if checkpoint else None
    artifacts, pending = {}, []
    def flush():
        nonlocal pending, previous
        if not pending:
            return
        block = {'archive_version': 1, 'repository_id': data['repository_id'], 'ref': data['ref'],
                 'previous': previous, 'records': pending}
        raw = encode(block)
        if len(raw) > BLOCK_LIMIT:
            raise GdiError('publication archive block exceeds size limit')
        identity = digest(raw)
        path = root / ('archive-' + identity + '.json')
        path.write_bytes(raw)
        artifacts[archive_path(identity)] = path
        previous, pending = identity, []
    for identity, metadata in chain:
        raw = read('branches/' + branch_directory(data['ref']) + '/' + identity + '.json')
        if validate_manifest(raw, identity, data['repository_id'], data['ref']) != metadata:
            raise GdiError('publication changed while archiving')
        entry = {'id': identity, 'manifest': raw.decode('utf-8')}
        if len(encode({'records': pending + [entry]})) > BLOCK_LIMIT - 1024:
            flush()
        pending.append(entry)
    flush()
    marker = {'checkpoint_version': 1, 'repository_id': data['repository_id'], 'ref': data['ref'],
              'root': publication.publication_id, 'previous': data['previous'],
              'previous_head': chain[-1][1]['head'] if chain else None,
              'history': previous, 'archived_count': len(chain)}
    validate_checkpoint(marker, data['repository_id'], data['ref'])
    target = root / CHECKPOINT
    target.write_bytes(encode(marker))
    return marker, artifacts, target


def activate(exchange, transport, repository_id, before, publication, marker, archives, target):
    """Publish a checkpoint only after its manifest and immutable archive are verified."""
    ref, identity = publication.data['ref'], publication.publication_id
    for relative, path in archives.items():
        if transport.read(relative) != path.read_bytes():
            raise GdiError('uploaded publication archive differs')
    expected = list(before) + [(identity, publication.data)]
    current = exchange.publications(transport, repository_id, ref)
    if list(current) != expected or current.checkpoint != getattr(before, 'checkpoint', None):
        raise GdiError('remote changed before checkpoint activation; preserve artifacts')
    transport.update_checkpoint(target, marker_path(ref))
    if transport.read(marker_path(ref)) != encode(marker):
        raise GdiError('checkpoint update was not confirmed')
    current = exchange.publications(transport, repository_id, ref)
    if list(current) != [(identity, publication.data)] or current.checkpoint != marker:
        raise GdiError('checkpoint activation conflicted; no cleanup attempted')
    compact(transport, marker, before)


def compact(transport, checkpoint, archived):
    """Remove only byte-verified copies already preserved in the immutable archive."""
    records = {identity: raw for identity, _, raw in archived_prefix(checkpoint, transport.read)}
    for identity, _ in archived:
        if transport.read(marker_path(checkpoint['ref'])) != encode(checkpoint):
            raise GdiError('checkpoint changed during archival cleanup; retry')
        relative = 'branches/' + branch_directory(checkpoint['ref']) + '/' + identity + '.json'
        if transport.read(relative) != records[identity]:
            raise GdiError('publication changed before archival cleanup')
        transport.delete_publication(relative)


def enable(exchange, name, *, quiescent=False, all_clients_updated=False):
    if not quiescent or not all_clients_updated:
        raise GdiError('checkpoint activation requires --quiescent --all-clients-updated')
    transport, repository_id = exchange.connect(name)
    return enable_repository(exchange, transport, repository_id)


def enable_repository(exchange, transport, repository_id):
    """Explicit activation preserves existing bundles and publication IDs."""
    before = transport.repository
    if not enabled(before):
        with tempfile.TemporaryDirectory(prefix='gdi-enable-checkpoints-') as temporary:
            source = Path(temporary) / 'repository.json'
            source.write_bytes(encode({**before, CAPABILITY: 1}))
            transport.enable_checkpoints(source)
        exchange.verify_identity(transport, repository_id)
        if transport.repository != {**before, CAPABILITY: 1}:
            raise GdiError('repository changed during checkpoint activation')
    from .branches import directory_ref
    from .work_lifecycle import records
    retired = {value['ref'] for value in records(transport, repository_id, 'retired').values()}
    branches = []
    for item in transport.list('branches'):
        if not item['IsDir']:
            raise GdiError('unexpected file in branch namespace')
        ref = directory_ref(item['Path'])
        if ref not in retired:
            branches.append(bootstrap(exchange, transport, repository_id, ref))
    return {'repository_id': repository_id, CAPABILITY: 1, 'branches': branches}


def bootstrap(exchange, transport, repository_id, ref):
    """Index the latest usable existing full publication, without rewriting history."""
    chain = exchange.publications(transport, repository_id, ref)
    if not chain:
        return {'ref': ref, 'state': 'empty'}
    if chain.checkpoint is not None:
        if chain.residue:
            compact(transport, chain.checkpoint, [(identity, None) for identity in chain.residue])
        return {'ref': ref, 'state': 'enabled', 'root': chain.checkpoint['root']}
    positions = {identity: number for number, (identity, _) in enumerate(chain)}
    start = max(number for number, (_, data) in enumerate(chain) if data['bundle_kind'] == 'full')
    while any(data['bundle_kind'] == 'incremental' and positions[data['base_publication']] < start
              for _, data in chain[start:]):
        start = max(number for number, (_, data) in enumerate(chain[:start]) if data['bundle_kind'] == 'full')
    root_id, root_data = chain[start]
    with tempfile.TemporaryDirectory(prefix='gdi-checkpoint-bootstrap-') as temporary:
        directory = Path(temporary)
        bundle = directory / 'source.bundle'
        transport.download('bundles/' + root_data['bundle_sha256'] + '.bundle', bundle)
        with verify_bundle(bundle, root_data, ref, previous=chain[start - 1][1] if start else None):
            pass
        publication = PreparedPublication(bundle, directory / 'publication.json', chain.raw_records[root_id])
        before = PublicationChain(chain[:start])
        marker, archives, target = prepare(before, publication, transport.read, directory)
        transport.mkdir('publication-archive')
        for path, source in archives.items():
            transport.upload(source, path)
            if transport.read(path) != source.read_bytes():
                raise GdiError('bootstrap archive read-back mismatch')
        exchange.verify_identity(transport, repository_id)
        if exchange.publications(transport, repository_id, ref) != chain:
            raise GdiError('branch changed during checkpoint bootstrap; retry in quiescence')
        transport.update_checkpoint(target, marker_path(ref))
        current = exchange.publications(transport, repository_id, ref)
        if current != list(chain[start:]) or current.checkpoint != marker:
            raise GdiError('bootstrap checkpoint conflicted; no cleanup attempted')
        compact(transport, marker, before)
    return {'ref': ref, 'state': 'enabled', 'root': root_id,
            'archived': start, 'active': len(chain) - start}


def enable_all(exchange, root_url, *, quiescent=False, all_clients_updated=False):
    if not quiescent or not all_clients_updated:
        raise GdiError('bulk checkpoint activation requires --quiescent --all-clients-updated')
    from .transport import validate_url
    validate_url(root_url)
    root = exchange.transport_factory(root_url)
    results = []
    for item in sorted(root.list(''), key=lambda item: item['Path']):
        if not item['IsDir']:
            continue
        path = item['Path']
        if not path or '/' in path or path in ('.', '..') or '\\' in path:
            raise GdiError('invalid repository folder name')
        url = root_url.rstrip('/') + '/' + path
        transport = exchange.transport_factory(url)
        raw = transport.read_optional('repository.json')
        if raw is None:
            results.append({'url': url, 'state': 'not_a_repository'})
            continue
        try:
            repository = validate_repository(decode(raw))
            repository_id = repository['repository_id']
            exchange.verify_identity(transport, repository_id)
            value = enable_repository(exchange, transport, repository_id)
            results.append({'url': url, 'state': 'enabled', **value})
        except (GdiError, OSError, UnicodeError) as exc:
            results.append({'url': url, 'state': 'failed', 'error': str(exc)})
    return {'root': root_url, 'repositories': results,
            'complete': not any(item['state'] == 'failed' for item in results)}
