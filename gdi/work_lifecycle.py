"""Opt-in work lifecycle metadata shared by online readers and connector snapshots."""

from pathlib import Path
import tempfile

from .branches import branch_directory
from .git import GdiError
from .publication import decode, digest, encode, hex_value, validate_publications, validate_repository
from . import work


def repository(transport, repository_id):
    value = getattr(transport, 'repository', None)
    if value is None:
        value = validate_repository(decode(transport.read('repository.json')))
    if value['repository_id'] != repository_id:
        raise GdiError('work lifecycle repository ID mismatch')
    return value


def enabled(transport, repository_id):
    return repository(transport, repository_id).get('work_version') == 1


def validate_record(value, repository_id, ident, kind):
    if not isinstance(value, dict):
        raise GdiError('work lifecycle record must be an object')
    common = {'work_version', 'repository_id', 'work', 'ref', 'final', 'marker', 'target', 'squash'}
    extra = {'order', 'target_publication', 'work_publication', 'confirmed_at'} if kind == 'imports' else {
        'receipt', 'archive', 'manifests', 'bundles'}
    if (set(value) != common | extra or type(value['work_version']) is not int or value['work_version'] != 1
            or value['repository_id'] != repository_id or value['work'] != ident or value['ref'] != work.PREFIX + ident):
        raise GdiError('invalid work ' + kind + ' record')
    if not isinstance(value['target'], str) or not value['target'].startswith('refs/heads/') or value['target'].startswith(work.PREFIX):
        raise GdiError('invalid work lifecycle target')
    branch_directory(value['target'])
    if any(not hex_value(value[key], 40) for key in ('final', 'marker', 'squash')):
        raise GdiError('invalid work lifecycle commit SHA')
    if kind == 'imports':
        from datetime import datetime
        if (type(value['order']) is not int or value['order'] < 1 or
                not all(hex_value(value[k], 64) for k in ('target_publication', 'work_publication'))):
            raise GdiError('invalid confirmed import order/publications')
        try:
            if datetime.fromisoformat(value['confirmed_at']).tzinfo is None:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise GdiError('invalid import confirmation timestamp') from exc
    else:
        if type(value['archive']) is not bool or not isinstance(value['manifests'], dict) or not value['manifests'] or not isinstance(value['bundles'], dict):
            raise GdiError('invalid retirement metadata')
        validate_record(value['receipt'], repository_id, ident, 'imports')
        if any(value[key] != value['receipt'][key] for key in common):
            raise GdiError('retirement differs from confirmed import')
        for publication, raw in value['manifests'].items():
            if not hex_value(publication, 64) or (not isinstance(raw, str) if value['archive'] else raw is not None):
                raise GdiError('invalid retired publication metadata')
            if value['archive'] and digest(raw.encode('utf-8')) != publication:
                raise GdiError('retired publication checksum mismatch')
        for name, size in value['bundles'].items():
            if not name.endswith('.bundle') or not hex_value(name[:-7], 64) or type(size) is not int or size < 0:
                raise GdiError('invalid retired bundle metadata')
    return value


def records(transport, repository_id, kind):
    if not enabled(transport, repository_id):
        return {}
    result = {}
    for item in transport.list('work/' + kind):
        name = item['Path']
        if item['IsDir'] or not name.endswith('.json'):
            raise GdiError('unexpected work lifecycle path: ' + name)
        ident = work.work_id(name[:-5])
        if ident in result:
            raise GdiError('duplicate work lifecycle record')
        result[ident] = validate_record(decode(transport.read('work/' + kind + '/' + name)), repository_id, ident, kind)
    if kind == 'imports' and len({value['order'] for value in result.values()}) != len(result):
        raise GdiError('conflicting import order: use one importer/coordinator per repository')
    return result



def retirement(transport, repository_id, ident):
    if not enabled(transport, repository_id):
        return None
    listing = transport.list('work/retired')
    for item in listing:
        if item['IsDir'] or not item['Path'].endswith('.json'):
            raise GdiError('unexpected work retirement path')
        work.work_id(item['Path'][:-5])
    if not any(item['Path'] == ident + '.json' for item in listing):
        return None
    return validate_record(decode(transport.read('work/retired/' + ident + '.json')), repository_id, ident, 'retired')


def require_active(transport, repository_id, ref):
    if ref.startswith(work.PREFIX):
        ident = work.work_id(ref[len(work.PREFIX):])
        if retirement(transport, repository_id, ident) is not None:
            raise GdiError('work branch is retired; do not resurrect it or reuse its Work-ID: ' + ref)


def archived_chain(value):
    if not value['archive']:
        return []
    prefix = branch_directory(value['ref']) + '/'
    metadata = {prefix + key + '.json': raw.encode('utf-8') for key, raw in value['manifests'].items()}
    listing = [{'Path': key, 'IsDir': False} for key in metadata]
    chain = validate_publications(value['repository_id'], value['ref'], listing, metadata)
    if not chain or chain[-1][1]['head'] != value['marker']:
        raise GdiError('retired archive does not reconstruct the recorded marker')
    return chain


def upload(transport, path, value):
    raw = encode(value)
    decode(raw)  # Лимит metadata проверяется до upload, не после порчи namespace.
    with tempfile.TemporaryDirectory(prefix='gdi-work-metadata-') as tmp:
        source = Path(tmp) / 'metadata.json'
        source.write_bytes(raw)
        transport.upload(source, path)
    if transport.read(path) != encode(value):
        raise GdiError('work lifecycle upload not confirmed; keep clients paused and retry')


def enable(exchange, name, *, quiescent=False, all_clients_updated=False):
    if not quiescent or not all_clients_updated:
        raise GdiError('enable-work requires --quiescent --all-clients-updated; upgrade old clients first')
    transport, repository_id = exchange.connect(name)
    before = transport.read('repository.json')
    value = validate_repository(decode(before))
    for folder in ('work', 'work/imports', 'work/retired'):
        transport.mkdir(folder)
    if value.get('work_version') == 1:
        records(transport, repository_id, 'imports')
        records(transport, repository_id, 'retired')
        return {'repository_id': repository_id, 'work_version': 1, 'state': 'already_enabled'}
    if any(transport.list(folder) for folder in ('work/imports', 'work/retired')):
        raise GdiError('work metadata exists without activation; inspect before enabling')
    value['work_version'] = 1
    if transport.read('repository.json') != before:
        raise GdiError('repository changed during work activation')
    with tempfile.TemporaryDirectory(prefix='gdi-enable-work-') as tmp:
        source = Path(tmp) / 'repository.json'
        source.write_bytes(encode(value))
        transport.enable_work(source)
    if transport.read('repository.json') != encode(value):
        raise GdiError('activation not confirmed; keep clients paused, refresh snapshots and retry')
    return {'repository_id': repository_id, 'work_version': 1, 'state': 'enabled'}


def confirm_import(exchange, name, value, target_publication, work_publication):
    transport, repository_id = exchange.connect(name)
    if not enabled(transport, repository_id):
        return None
    prior = records(transport, repository_id, 'imports')
    ident = value['work']
    common = {'work_version': 1, 'repository_id': repository_id, 'work': ident, 'ref': value['source_ref'],
              'final': value['source_head'], 'marker': value['marker'], 'target': value['target'], 'squash': value['squash']}
    if ident in prior:
        if any(prior[ident][key] != item for key, item in common.items()):
            raise GdiError('confirmed import differs; do not reuse a Work-ID')
        return prior[ident]
    from .ci_protocol import now
    receipt = {**common, 'order': max((item['order'] for item in prior.values()), default=0) + 1,
               'confirmed_at': now(), 'target_publication': target_publication, 'work_publication': work_publication}
    upload(transport, 'work/imports/' + ident + '.json', receipt)
    records(transport, repository_id, 'imports')
    return receipt
