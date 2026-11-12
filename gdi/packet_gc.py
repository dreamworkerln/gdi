"""Explicit quiescent cleanup; never called from push or packet adoption."""

from pathlib import Path

from . import packet
from .git import GdiError
from .publication import decode, hex_value


def collect(exchange, name, *, apply=False, keep_checkpoints=2, keep_imported=None,
            bundle_only=False, quiescent=False, report=print):
    if type(keep_checkpoints) is not int or keep_checkpoints < 1:
        raise GdiError('keep-checkpoints must be positive')
    if apply and not quiescent:
        raise GdiError('GC --apply requires --quiescent: pause all writers and workers')
    transport, identity = exchange.connect(name)
    settings = exchange.remote(name)
    from .gc import ci_guard
    ci_guard(exchange, transport, identity, inbox_root=settings['inbox_root'])
    heads = [packet.read_head(transport, identity, ref) for ref in exchange.branches(transport, identity)]
    from .work_lifecycle import records, upload
    imports = records(transport, identity, 'imports')
    retired = records(transport, identity, 'retired')
    retiring = {}
    if not bundle_only:
        keep = 10 if keep_imported is None else keep_imported
        if type(keep) is not int or keep < 0:
            raise GdiError('keep-imported must be nonnegative')
        completed = sorted(imports.values(), key=lambda item: item['order'], reverse=True)
        by_ref = {head['ref']: head for head in heads}
        for receipt in completed[keep:]:
            source, target = by_ref.get(receipt['ref']), by_ref.get(receipt['target'])
            if source is None or target is None or source['metadata']['head'] != receipt['marker']:
                continue
            _, cache = packet.load_publication(exchange, transport, identity, target['publication_id'])
            if not cache.git.has_commit(receipt['squash']) or not cache.git.ancestor(receipt['squash'], target['metadata']['head']):
                continue
            retiring[receipt['ref']] = {**{key: receipt[key] for key in
                ('repository_id', 'work', 'ref', 'final', 'marker', 'target', 'squash')},
                'work_version': 2, 'receipt': receipt, 'packets': [source['publication_id']]}
    retained = set()
    for head in heads:
        if head['ref'] in retiring:
            continue
        current = head
        for index in range(keep_checkpoints):
            if current is None:
                break
            retained.add(current['publication_id'])
            previous = current['metadata']['previous']
            if previous is None or index + 1 == keep_checkpoints:
                break
            if index + 2 == keep_checkpoints:
                retained.add(previous)
                break
            current, _ = packet.load_publication(exchange, transport, identity, previous)
    # Every recorded CI request, including completed jobs, pins its exact packet.
    root_names = {row['Path'] for row in transport.list('') if row['IsDir']}
    for item in (transport.list('ci', recursive=True) if 'ci' in root_names else []):
        path = item['Path']
        if not item['IsDir'] and path.startswith('jobs/') and path.endswith(('/submission.json', '/request.json')):
            value = decode(transport.read('ci/' + path))
            req = value.get('request', value)
            if req.get('repository_id') == identity and hex_value(req.get('publication_id'), 64):
                retained.add(req['publication_id'])
    # Work receipts pin exact source and target publications even after import.
    for value in imports.values():
        retained.add(value['target_publication'])
        if value['ref'] not in retiring and value['work'] not in retired:
            retained.add(value['work_publication'])
    candidates = []
    for item in transport.list('packets'):
        path = item['Path']
        if item['IsDir'] or not path.endswith('.zip') or not hex_value(path[:-4], 64):
            raise GdiError('unexpected packet namespace entry')
        if path[:-4] not in retained:
            candidates.append('packets/' + path)
    candidates.sort()
    for ref, value in retiring.items():
        report(('Retire ' if apply else 'Would retire ') + ref)
        if apply:
            if value['work'] not in retired:
                upload(transport, 'work/retired/' + value['work'] + '.json', value)
            transport.delete_head(packet.head_path(ref))
    for path in candidates:
        report(('Remove ' if apply else 'Would remove ') + path)
        if apply:
            transport.delete_packet(path)
    # Work retirement remains an explicit separate operation; packet GC never
    # silently deletes refs, old v3 files, runtime, CI results or work records.
    return {'repository_id': identity, 'apply': apply, 'remove_packets': candidates,
            'retained_publications': sorted(retained), 'retired_works': sorted(retiring)}
