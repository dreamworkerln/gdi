"""Worker adoption of one uploaded v4 packet; never rebuild/re-upload Git data."""

from . import packet
from .agent_sync import PUSH_NAME, VERSION, receipt_path
from .ci_protocol import atomic_write
from .git import GdiError
from .publication import decode, encode, file_digest


def receive(worker, root, name, *, present=True):
    match = PUSH_NAME.fullmatch(name)
    if match is None or match[4] != worker.worker_id:
        raise GdiError('invalid packet or another worker addressee')
    repository_id, request_id, identity, _ = match.groups()
    directory = worker.root / 'agent-pushes' / repository_id / request_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'packet.zip'
    if not path.exists():
        incoming = directory / 'incoming.zip'
        root.download('inbox/' + name, incoming)
        if file_digest(incoming) != identity:
            raise GdiError('uploaded packet checksum mismatch')
        incoming.replace(path)
    if path.is_symlink() or file_digest(path) != identity:
        raise GdiError('request ID reused with different packet bytes')
    with packet.read(path, repository_id) as (target, quarantine):
        data = target['metadata']
        if data['request_id'] != request_id or data['worker_id'] != worker.worker_id or data['repository_path'] is None:
            raise GdiError('packet route differs from inbox name')
        journal_path = directory / 'journal.json'
        journal = decode(journal_path.read_bytes()) if journal_path.exists() else {
            'name': name, 'pointer': target, 'phase': 'PREPARED'}
        if journal.get('name') != name or journal.get('pointer') != target:
            raise GdiError('durable request identity changed')
        phase = journal.get('phase')
        if phase not in ('PREPARED', 'MOVING', 'MOVED', 'REPLIED', 'DONE'):
            raise GdiError('invalid packet adoption journal phase')
        if phase == 'DONE':
            if present:
                root.delete_notification('inbox/' + name)
            return journal['receipt']
        repo_url = worker.config['remote_url'] + '/' + data['repository_path']
        transport = worker.transport_factory(repo_url)
        transport.repository = {'version': 4, 'repository_id': repository_id,
                                'object_format': 'sha1', 'work_version': 1}
        if 'receipt' not in journal:
            current = packet.read_head(transport, repository_id, data['ref'])
            same = current is not None and current['publication_id'] == identity
            from .work_lifecycle import require_active
            require_active(transport, repository_id, data['ref'])
            result = {'agent_exchange_version': VERSION, 'repository_id': repository_id,
                      'request_id': request_id, 'package_sha256': identity, 'worker_id': worker.worker_id,
                      'ref': data['ref'], 'head': data['head']}
            base = current['publication_id'] if current is not None else None
            base_head = current['metadata']['head'] if current is not None else None
            if not same and (data['previous'], data['previous_head']) != (base, base_head):
                result.update(state='CONFLICT', current_publication=base, current_head=base_head,
                              error='remote advanced; pull and reconcile history')
            else:
                if current is None:
                    from .publication import validate_repository
                    repository = validate_repository(decode(transport.read('repository.json')))
                    if repository['version'] != 4 or repository['repository_id'] != repository_id:
                        raise GdiError('remote identity changed; upgrade required')
                if not same:
                    phase = journal['phase']
                    if phase == 'MOVING' and transport.packet_exists(packet.packet_path(identity)):
                        journal['phase'] = 'MOVED'
                        journal['adopted'] = True
                    if journal['phase'] != 'MOVED':
                        journal['phase'] = 'MOVING'
                        atomic_write(journal_path, encode(journal))
                        transport.move_packet(worker.config['remote_url'], 'inbox/' + name, packet.packet_path(identity))
                        journal['phase'] = 'MOVED'
                        journal['adopted'] = True
                        atomic_write(journal_path, encode(journal))
                    packet.write_head(transport, target)
                result.update(state='PUBLISHED', publication_id=identity, pointer=target)
            journal.update(phase='REPLIED', receipt=result)
            atomic_write(journal_path, encode(journal))
        result = journal['receipt']
        receipt = directory / 'receipt.json'
        atomic_write(receipt, encode(result))
        if not getattr(transport, 'creates_parents', False):
            transport.mkdir('agent')
        transport.upload(receipt, receipt_path(repository_id, request_id))
        if not journal.get('adopted') and present:
            root.delete_notification('inbox/' + name)
        journal['phase'] = 'DONE'
        atomic_write(journal_path, encode(journal))
        return result


def recover(worker, root):
    """Moved packets no longer occur in inbox; recover them from local journals."""
    recovered = set()
    for path in sorted((worker.root / 'agent-pushes').glob('*/*/journal.json')):
        journal = decode(path.read_bytes())
        if journal.get('phase') != 'DONE' and worker.inbox_retry_ready(journal['name']):
            recovered.add(journal['name'])
            try:
                receive(worker, root, journal['name'], present=False)
                worker.inbox_retries.pop(journal['name'], None)
            except (GdiError, OSError) as exc:
                worker.inbox_retry_failed(journal['name'])
                import logging
                logging.getLogger('gdi.worker').error('agent recovery=%s: %s', path, exc)
    return recovered
