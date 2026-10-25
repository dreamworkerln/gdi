"""Quiescent collection of imported work directories, sharing the bundle GC verifier."""

from . import gc, work, work_lifecycle
from .branches import branch_directory
from .ci_protocol import request
from .git import GdiError
from .publication import decode, digest

DEFAULT_KEEP_IMPORTED = 10


def ci_refs(exchange, transport, repository_id):
    if not any(item['Path'] == 'ci' for item in transport.list('')):
        return set()
    result = set()
    for item in transport.list('ci', recursive=True):
        if not item['IsDir'] and item['Path'].endswith('/submission.json'):
            from .inbox import submission
            value, _ = submission(decode(transport.read('ci/' + item['Path'])), exchange.git, repository_id)
            result.add(value['ref'])
        if not item['IsDir'] and item['Path'].endswith('/request.json'):
            value = request(decode(transport.read('ci/' + item['Path'])), exchange.git, repository_id)
            result.add(value['ref'])
    return result



def pending_notifications(exchange, name, repository_id, retiring):
    root = exchange.remote(name)['inbox_root']
    if root is None:
        return []
    shared = exchange.transport_factory(root)
    if not any(item['Path'] == 'inbox' and item['IsDir'] for item in shared.list('')):
        return []
    from .inbox import read
    refs = {value['ref'] for value in retiring.values()}
    selected = []
    for item in shared.list('inbox'):
        if item['IsDir']:
            continue
        event = read(shared, item['Path'])
        if event['repository_id'] == repository_id and event['ref'] in refs:
            selected.append(item['Path'])
    return sorted(selected)

def imported(exchange, transport, repository_id, state, receipts, retired):
    git = exchange.git
    eligible, blocked = [], []
    for ref, chain in state.chains.items():
        if not ref.startswith(work.PREFIX):
            continue
        ident = work.work_id(ref[len(work.PREFIX):])
        if ident in retired:
            continue
        try:
            cache = exchange.restore(transport, repository_id, chain)
            git.import_objects(cache.path, chain[-1][1]['head'])
            value = work.validate(git, ref, chain[-1][1]['head'], repository_id)
            if value['state'] != 'imported' or ident not in receipts:
                blocked.append({'work': ident, 'reason': 'not a confirmed published import'})
                continue
            receipt = receipts[ident]
            if any(receipt[key] != value[name] for key, name in
                   (('ref', 'ref'), ('final', 'final'), ('marker', 'head'), ('target', 'target'))):
                raise GdiError('import receipt differs from current work tip')
            if (receipt['squash'] != value['receipt']['squash'] or receipt['work_publication'] not in dict(chain) or
                    dict(chain)[receipt['work_publication']]['head'] != value['head']):
                raise GdiError('import squash/publication differs from confirmed receipt')
            target_chain = state.chains.get(value['target'])
            if not target_chain or receipt['target_publication'] not in dict(target_chain):
                raise GdiError('confirmed target publication is not retained')
            cache = exchange.restore(transport, repository_id, target_chain)
            git.import_objects(cache.path, target_chain[-1][1]['head'])
            squash = receipt['squash']
            if (not git.has_commit(squash) or not git.ancestor(squash, target_chain[-1][1]['head']) or
                    not git.ancestor(squash, dict(target_chain)[receipt['target_publication']]['head'])):
                raise GdiError('published target no longer contains squash')
            if work.message(git, squash) != value['message'] or work.parents(git, squash) != [value['receipt']['target_before']]:
                raise GdiError('squash message/parent differs from work receipt')
            eligible.append((receipt['order'], ident, value))
        except GdiError as exc:
            blocked.append({'work': ident, 'reason': str(exc)})
    return sorted(eligible), blocked


def projected_state(state, retirements):
    dropped = {value['ref'] for value in retirements.values() if not value['archive']}
    directories = {branch_directory(value['ref']) for value in retirements.values()}
    return gc.Snapshot({path: raw for path, raw in state.metadata.items()
                        if path.split('/', 1)[0] not in {branch_directory(ref) for ref in dropped}},
                       tuple(path for path in state.directories if path not in directories),
                       {ref: chain for ref, chain in state.chains.items() if ref not in dropped},
                       state.bundles, state.lifecycle)


def collect(exchange, name, *, apply=False, keep_imported=DEFAULT_KEEP_IMPORTED,
            keep_checkpoints=2, quiescent=False, report=print):
    if type(keep_imported) is not int or keep_imported < 0:
        raise GdiError('keep-imported must be a nonnegative integer')
    if type(keep_checkpoints) is not int or keep_checkpoints < 1:
        raise GdiError('keep-checkpoints must be a positive integer')
    if apply and not quiescent:
        raise GdiError('work GC --apply requires --quiescent: pause ALL clients and workers')
    transport, repository_id = exchange.connect(name)
    if not work_lifecycle.enabled(transport, repository_id):
        raise GdiError('activate work lifecycle with remote enable-work before collecting work branches')
    state = gc.snapshot(exchange, transport, repository_id)
    receipts = work_lifecycle.records(transport, repository_id, 'imports')
    old = work_lifecycle.records(transport, repository_id, 'retired')
    eligible, blocked = imported(exchange, transport, repository_id, state, receipts, old)
    chosen = eligible[:-keep_imported] if keep_imported else eligible
    protected_ci = ci_refs(exchange, transport, repository_id)
    retiring = dict(old)
    for _, ident, value in chosen:
        prefix = branch_directory(value['ref']) + '/'
        metadata = {path[len(prefix):-5]: raw for path, raw in state.metadata.items() if path.startswith(prefix)}
        archive = value['ref'] in protected_ci
        bundles = {data['bundle_sha256'] + '.bundle': data['bundle_bytes'] for _, data in state.chains[value['ref']]}
        retiring[ident] = {'work_version': 1, 'repository_id': repository_id, 'work': ident,
                          'ref': value['ref'], 'final': value['final'], 'marker': value['head'],
                          'target': value['target'], 'squash': value['receipt']['squash'], 'receipt': receipts[ident],
                          'archive': archive, 'manifests': {pub: raw.decode('utf-8') if archive else None
                                                         for pub, raw in metadata.items()}, 'bundles': bundles}
    for ident, value in retiring.items():
        work_lifecycle.validate_record(value, repository_id, ident, 'retired')
        from .publication import encode
        decode(encode(value))
        if value['archive']:
            work_lifecycle.archived_chain(value)
    projected = projected_state(state, retiring)
    bundle_plan = gc.plan_collection(projected, repository_id, keep_checkpoints)
    needed = {dict(projected.chains[branch.ref])[pub]['bundle_sha256'] + '.bundle'
              for branch in bundle_plan.branches for pub in branch.retained}
    disposable = {name: size for value in retiring.values() for name, size in value['bundles'].items()}
    for path, expected in disposable.items():
        if path in state.bundles and state.bundles[path] != expected:
            raise GdiError('retired bundle size changed: ' + path)
    bundle_names = ({item.path[len('bundles/'):] for item in bundle_plan.candidates} |
                    {path for path in disposable if path in state.bundles and path not in needed})
    pending_directories = {item['Path'] for item in transport.list('branches') if item['IsDir']}
    removals = [value for value in retiring.values() if branch_directory(value['ref']) in pending_directories]
    inbox_state = pending_notifications(exchange, name, repository_id, retiring)
    plan = {'repository_id': repository_id, 'keep_imported': keep_imported,
            'retire': [ident for _, ident, _ in chosen], 'blocked': blocked,
            'remove_directories': ['branches/' + branch_directory(value['ref']) for value in removals],
            'remove_bundles': ['bundles/' + path for path in sorted(bundle_names)],
            'ci_archives': sorted(ident for ident, value in retiring.items() if value['archive']),
            'pending_notifications': inbox_state}
    report(f"Work GC: keep {keep_imported} confirmed imported branches; retire {plan['retire']}")
    for path in plan['remove_directories'] + plan['remove_bundles']:
        report('  Candidate: ' + path)
    if not apply:
        report('Dry run: no remote writes; apply requires coordinated --quiescent pause')
        return plan
    if inbox_state:
        raise GdiError('pending work notifications: let the normal worker process inbox, then pause clients and retry GC')
    ci_state = gc.ci_guard(exchange, transport, repository_id, inbox_root=exchange.remote(name).get('inbox_root'))
    gc.verify_retained(exchange, transport, repository_id, projected, bundle_plan, report)
    latest, latest_id = exchange.connect(name)
    if (latest_id != repository_id or gc.snapshot(exchange, latest, repository_id) != state or
            gc.ci_guard(exchange, latest, repository_id, inbox_root=exchange.remote(name).get('inbox_root')) != ci_state or
            pending_notifications(exchange, name, repository_id, retiring)):
        raise GdiError('remote/CI changed during work GC verification; nothing deleted')
    # Retirement публикуется раньше удаления и делает повтор после partial failure безопасным.
    for _, ident, _ in chosen:
        work_lifecycle.upload(latest, 'work/retired/' + ident + '.json', retiring[ident])
    if work_lifecycle.records(latest, repository_id, 'retired') != retiring:
        raise GdiError('retirement metadata changed; keep all clients paused')
    for value in removals:
        directory = 'branches/' + branch_directory(value['ref'])
        for item in latest.list(directory):
            publication = item['Path'][:-5] if item['Path'].endswith('.json') else ''
            if item['IsDir'] or publication not in value['manifests']:
                raise GdiError('unexpected file appeared in retired work directory; keep clients paused')
            path = directory + '/' + item['Path']
            if digest(latest.read(path)) != publication:
                raise GdiError('retired manifest changed before deletion')
            latest.delete_work_manifest(path)
        latest.remove_work_directory(directory)
    for path in sorted(bundle_names):
        latest.delete_bundle('bundles/' + path)
    final, final_id = exchange.connect(name)
    after = gc.snapshot(exchange, final, repository_id)
    expected_bundles = {path: size for path, size in state.bundles.items() if path not in bundle_names}
    if (final_id != repository_id or after.chains != projected.chains or after.metadata != projected.metadata or
            after.bundles != expected_bundles or gc.ci_guard(exchange, final, repository_id, inbox_root=exchange.remote(name).get('inbox_root')) != ci_state or
            work_lifecycle.records(final, repository_id, 'retired') != retiring or
            pending_notifications(exchange, name, repository_id, retiring)):
        raise GdiError('work GC final state differs; keep clients paused and retry/inspect')
    present = {item['Path'] for item in final.list('branches')}
    if any(branch_directory(value['ref']) in present for value in retiring.values()):
        raise GdiError('retired work folder still exists; keep clients paused and retry')
    report('Work GC complete: retired branch folders removed; CI archives and dependencies retained')
    return plan
