"""Host-side publication of the agent's single uploaded packet, without CI execution."""

from .agent_sync import (PUSH_NAME, VERSION, read_package, receipt_path, publish_export)
from .ci_protocol import atomic_write
from .exchange import Exchange
from .git import GdiError, Git
from .publication import decode, encode, file_digest


def publisher(worker, repository_id, repository_path):
    """Private object store: never change the operator's checkout or branch."""
    route = worker.config['remote_url'] + '/' + repository_path
    owner = worker.root / 'agent-publishers' / repository_id
    owner.mkdir(parents=True, exist_ok=True, mode=0o700)
    git = Git(owner, isolated=True)
    if not (owner / '.git').exists():
        git.call('init', '--quiet', '--object-format=sha1', '--template=')
    exchange = Exchange(git, worker.transport_factory)
    exchange.publisher_worker_id = worker.worker_id
    from .local_config import load
    settings = load(git)['remotes'].get('drive')
    if settings is None:
        exchange.add('drive', route, expected_id=repository_id, inbox_root=worker.config['remote_url'])
    elif settings['url'] != route or settings['repository_id'] != repository_id:
        raise GdiError('agent publisher route changed; existing publisher state preserved')
    return exchange


def refresh_export(worker, repository_id, repository_path, ref):
    exchange = publisher(worker, repository_id, repository_path)
    transport, _ = exchange.connect('drive')
    from .work import PREFIX, work_id
    from .work_lifecycle import retirement
    if ref.startswith(PREFIX) and retirement(transport, repository_id, work_id(ref[len(PREFIX):])) is not None:
        return
    chain = exchange.publications(transport, repository_id, ref)
    if not chain:
        return
    cache = exchange.restore(transport, repository_id, chain)
    exchange.git.import_objects(cache.path, chain[-1][1]['head'])
    exchange.git.call('update-ref', ref, chain[-1][1]['head'])
    publish_export(exchange, 'drive', repository_id, ref, chain[-1][1]['head'], chain[-1][0])


def receive(worker, root, name):
    match = PUSH_NAME.fullmatch(name)
    if not match:
        raise GdiError('invalid agent push filename')
    repository_id, request_id, checksum, worker_id = match.groups()
    if worker_id != worker.worker_id:
        raise GdiError('agent push is addressed to another worker')
    directory = worker.root / 'agent-pushes' / repository_id / request_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    packet = directory / 'package.zip'
    if not packet.exists():
        temporary = directory / 'incoming.zip'
        root.download('inbox/' + name, temporary)
        if file_digest(temporary) != checksum:
            raise GdiError('agent upload checksum mismatch; source packet retained')
        temporary.replace(packet)
    if packet.is_symlink() or file_digest(packet) != checksum:
        raise GdiError('agent request ID reused with different package bytes')
    with read_package(packet, repository_id, 'push') as (request, quarantine):
        if request['request_id'] != request_id or request['worker_id'] != worker_id:
            raise GdiError('agent request ID differs from its filename')
        journal_path = directory / 'journal.json'
        journal = decode(journal_path.read_bytes()) if journal_path.exists() else {
            'request': request, 'package_sha256': checksum, 'phase': 'PREPARED'}
        if journal.get('request') != request or journal.get('package_sha256') != checksum:
            raise GdiError('durable agent publication differs from the uploaded request')
        if journal.get('phase') not in ('PREPARED', 'PUBLISHING', 'REPLIED'):
            raise GdiError('invalid durable agent publication phase')
        exchange = publisher(worker, repository_id, request['repository_path'])
        git = exchange.git
        transport, _ = exchange.connect('drive')
        if journal.get('receipt') is None:
            current = exchange.publications(transport, repository_id, request['ref'])
            tip = current[-1] if current else None
            published = None
            if (tip and tip[1]['head'] == request['head'] and
                    (not request['full'] or tip[0] != request['base_publication']
                     or tip[1]['bundle_kind'] == 'full')):
                published = tip[0]
            elif journal['phase'] == 'PUBLISHING':
                # Recover the narrow crash window after commit but before durable acknowledgement.
                from .publication_checkpoints import history_chain
                for identity, data in history_chain(current, transport.read):
                    if data['head'] == request['head'] and data['previous'] == request['base_publication']:
                        published = identity
                        break
            base_matches = bool(tip and tip[0] == request['base_publication']
                                and tip[1]['head'] == request['base_head'])
            result = {'agent_exchange_version': VERSION, 'repository_id': repository_id,
                      'request_id': request_id, 'package_sha256': checksum,
                      'ref': request['ref'], 'head': request['head'], 'worker_id': worker.worker_id}
            if published is None and not base_matches:
                result.update(state='CONFLICT', current_head=tip[1]['head'] if tip else None,
                              current_publication=tip[0] if tip else None,
                              error='remote advanced or was replaced; pull its export and reconcile history')
            else:
                if published is None:
                    journal['phase'] = 'PUBLISHING'
                    atomic_write(journal_path, encode(journal))
                    git.import_objects(quarantine, request['head'])
                    git.call('update-ref', request['ref'], request['head'])
                    _, published, _ = exchange.push('drive', request['ref'][11:], full=request['full'])
                else:
                    # Keep the downloadable export available even after recovery or a lost acknowledgement.
                    cache = exchange.restore(transport, repository_id, current)
                    git.import_objects(cache.path, current[-1][1]['head'])
                    git.call('update-ref', request['ref'], current[-1][1]['head'])
                    # An unchanged native push also finishes a checkpoint whose manifest
                    # committed before a crash. Export alone would skip that recovery.
                    same_head = current[-1][1]['head'] == request['head']
                    _, recovered, _ = exchange.push('drive', request['ref'][11:],
                                                     full=request['full'] and same_head)
                    if same_head:
                        published = recovered
                result.update(state='PUBLISHED', publication_id=published, worker_id=worker.worker_id)
            journal.update(phase='REPLIED', receipt=result)
            atomic_write(journal_path, encode(journal))
        result = journal['receipt']
        receipt = directory / 'receipt.json'
        atomic_write(receipt, encode(result))
        if not getattr(transport, 'creates_parents', False):
            transport.mkdir('agent')
        # Retry always sends the same receipt bytes. No read-back is needed on the agent side.
        transport.upload(receipt, receipt_path(repository_id, request_id))
        root.delete_notification('inbox/' + name)
        return result
