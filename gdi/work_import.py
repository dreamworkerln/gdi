"""Operator-side squash plans using verified Exchange objects and native Git merges."""

from pathlib import Path
import tempfile

from .branches import directory_ref
from .ci_protocol import atomic_write
from .git import GdiError, Git
from .publication import decode, digest, encode, hex_value
from . import work


def merged_tree(git, before, final):
    # Подготовка в отдельном worktree не оставляет MERGE_HEAD/index в checkout оператора.
    with tempfile.TemporaryDirectory(prefix='gdi-import-') as tmp:
        path = Path(tmp) / 'merge'
        git.call('worktree', 'add', '--quiet', '--detach', str(path), before)
        try:
            prepared = Git(path, isolated=git.isolated)
            result = prepared.call('merge', '--squash', '--no-commit', '--no-stat', final, allowed=(0, 1))
            if result.returncode:
                conflicts = prepared.call('diff', '--name-only', '--diff-filter=U', '-z').stdout
                names = ', '.join(repr(name) for name in conflicts.split('\0') if name)
                raise GdiError('squash import conflicts; target was not changed: ' +
                               (names or result.stderr.strip()))
            return prepared.text('write-tree')
        finally:
            git.call('worktree', 'remove', '--force', str(path))


def existing_squash(git, value, target_head):
    found = []
    for oid in git.text('rev-list', '--first-parent', target_head, '^' + value['base']).splitlines():
        if work.message(git, oid) != value['message']:
            continue
        parents = work.parents(git, oid)
        if len(parents) != 1 or not git.ancestor(value['base'], parents[0]):
            continue
        if merged_tree(git, parents[0], value['final']) != git.text('rev-parse', oid + '^{tree}'):
            raise GdiError('target contains the work message with a different result; resolve the ambiguous import manually')
        found.append(oid)
    if len(found) > 1:
        raise GdiError('multiple squash commits match this work; refusing duplicate/ambiguous import')
    return found[0] if found else None


def candidates(exchange, name):
    git = exchange.git
    before = git.snapshot()
    git.require_clean(before)
    target = git.ref(before[0])
    if target.startswith(work.PREFIX) or before[1] is None:
        raise GdiError('import requires the assigned ordinary target branch')
    transport, repository_id = exchange.connect(name)
    choices, rejected = [], []
    for item in transport.list('branches'):
        if not item['IsDir']:
            raise GdiError('unexpected file in branch namespace')
        ref = directory_ref(item['Path'])
        if not ref.startswith(work.PREFIX):
            continue
        try:
            head, publication, _ = exchange.fetch(name, ref[11:])
            value = work.validate(git, ref, head, repository_id)
            if value['state'] != 'ready':
                rejected.append({'ref': ref, 'reason': value['state']})
                continue
            if value['target'] != target:
                rejected.append({'ref': ref, 'reason': 'assigned to ' + value['target']})
                continue
            if not git.ancestor(value['base'], before[1]):
                raise GdiError('target no longer contains the assigned work base')
            choices.append({**value, 'publication_id': publication})
        except GdiError as exc:
            rejected.append({'ref': ref, 'reason': str(exc)})
    if git.snapshot() != before:
        raise GdiError('checkout changed during discovery; run import again')
    return {'target': target, 'target_head': before[1], 'repository_id': repository_id,
            'choices': sorted(choices, key=lambda value: int(value['work'])), 'rejected': rejected}


def prepare(exchange, name, value, target_head, output=None):
    git = exchange.git
    before = git.snapshot()
    git.require_clean(before)
    if git.ref(before[0]) != value['target'] or before[1] != target_head:
        raise GdiError('target changed during selection; create a new import plan')
    head, publication, _ = exchange.fetch(name, value['ref'][11:])
    if head != value['final'] or publication != value['publication_id']:
        raise GdiError('work publication changed during selection; create a new plan')
    source = work.validate(git, value['ref'], head, value['repository_id'])
    if source['state'] != 'ready' or source['target'] != value['target']:
        raise GdiError('selected work is no longer ready for this target')
    prior = existing_squash(git, source, target_head)
    tree = git.text('rev-parse', prior + '^{tree}') if prior else merged_tree(git, target_head, head)
    settings = exchange.remote(name)
    plan = {'plan_version': 1, 'kind': 'work-import', 'repository_id': source['repository_id'],
            'remote': name, 'url': settings['url'], 'target': source['target'], 'target_head': target_head,
            'source_ref': source['ref'], 'source_head': head, 'publication_id': publication,
            'work': source['work'], 'base': source['base'], 'message': source['message'],
            'tree': tree, 'existing_squash': prior}
    ident = digest(encode(plan))
    plan['plan_id'] = ident
    path = Path(output) if output else git.gdi_dir() / 'imports' / 'plans' / (ident + '.json')
    if path.exists() and path.read_bytes() != encode(plan):
        raise GdiError('output already contains another import plan; preserve it and choose a new path')
    atomic_write(path, encode(plan))
    return {**plan, 'plan_path': str(path.resolve()),
            'files': git.changed_files(target_head, tree), 'next_step': 'apply_saved_plan'}


def load_plan(path):
    value = decode(Path(path).read_bytes())
    keys = {'plan_version', 'kind', 'repository_id', 'remote', 'url', 'target', 'target_head',
            'source_ref', 'source_head', 'publication_id', 'work', 'base', 'message', 'tree',
            'existing_squash', 'plan_id'}
    if set(value) != keys or value['plan_version'] != 1 or type(value['plan_version']) is not int or value['kind'] != 'work-import':
        raise GdiError('unsupported import plan')
    if value['plan_id'] != digest(encode({key: item for key, item in value.items() if key != 'plan_id'})):
        raise GdiError('import plan checksum mismatch')
    for name, length in (('repository_id', 32), ('target_head', 40), ('source_head', 40),
                         ('base', 40), ('tree', 40), ('publication_id', 64), ('plan_id', 64)):
        if not hex_value(value[name], length):
            raise GdiError('invalid import plan ' + name)
    if value['existing_squash'] is not None and not hex_value(value['existing_squash'], 40):
        raise GdiError('invalid existing squash SHA')
    work.work_id(value['work'])
    if value['source_ref'] != work.PREFIX + value['work']:
        raise GdiError('import plan work/ref mismatch')
    return value



def install(git, plan, squash):
    now = git.snapshot()
    git.require_clean(now)
    if now[:2] == (plan['target'][11:], squash):
        return
    if now[:2] != (plan['target'][11:], plan['target_head']):
        raise GdiError('target branch or HEAD changed; refusing import')
    # Применяем уже подготовленный single-parent commit штатным Git fast-forward.
    # Как и обычный pull, операция не поддерживает сторонних Git writers в checkout.
    git.call('merge', '--ff-only', '--no-autostash', '--no-stat', squash)
    final = git.snapshot()
    git.require_clean(final)
    if final[:2] != (plan['target'][11:], squash):
        raise GdiError('unexpected checkout after import; inspect concurrent Git activity')


def journal_path(git, plan):
    return git.gdi_dir() / 'imports' / plan['repository_id'] / (plan['work'] + '.json')


def apply(exchange, path):
    git = exchange.git
    plan = load_plan(path)
    settings = exchange.remote(plan['remote'])
    if settings['repository_id'] != plan['repository_id'] or settings['url'] != plan['url']:
        raise GdiError('connection identity or URL changed since plan; refusing import')
    if git.ref(git.branch()) != plan['target']:
        raise GdiError('current branch is not the assigned import target')
    state_path = journal_path(git, plan)
    journal = decode(state_path.read_bytes()) if state_path.exists() else None
    head, publication, _ = exchange.fetch(plan['remote'], plan['source_ref'][11:])
    if journal and head == journal.get('marker'):
        value = work.validate(git, plan['source_ref'], head, plan['repository_id'])
        if value['receipt']['squash'] != journal['squash']:
            raise GdiError('published import marker differs from pending state')
    elif head != plan['source_head'] or publication != plan['publication_id']:
        raise GdiError('source changed since import plan; create a new plan')
    if journal and journal.get('plan_id') != plan['plan_id']:
        immutable = ('repository_id', 'remote', 'url', 'target', 'source_ref', 'source_head', 'work', 'base', 'message')
        if any(journal.get(key) != plan[key] for key in immutable):
            raise GdiError('new plan changes a pending work binding; preserve it and reconcile manually')
        snapshot = git.snapshot()
        git.require_clean(snapshot)
        if snapshot[:2] != (plan['target'][11:], plan['target_head']):
            raise GdiError('target changed since replacement plan')
        if plan['existing_squash'] == journal['squash']:
            # Применённый squash не повторяем: новый plan продолжает ту же delivery.
            journal = {**plan, 'squash': journal['squash'], 'marker': journal['marker'], 'state': journal['state']}
            atomic_write(state_path, encode(journal))
        elif (plan['existing_squash'] is None and head == plan['source_head'] and
              not git.ancestor(journal['squash'], plan['target_head']) and
              git.oid(plan['source_ref']) != journal['marker']):
            # Неприменённый prepared commit уступает новому HEAD оператора;
            # старый journal сохраняется, не сбрасываются пользовательские refs.
            atomic_write(state_path.parent / 'superseded' / (journal['plan_id'] + '.json'), encode(journal))
            journal = None
        else:
            raise GdiError('pending import was applied or source changed; reconcile before replacing its plan')
    value = work.validate(git, plan['source_ref'], plan['source_head'], plan['repository_id'])
    if (value['state'], value['target'], value['base'], value['message']) != ('ready', plan['target'], plan['base'], plan['message']):
        raise GdiError('work binding or canonical message differs from plan')
    local = git.oid(plan['source_ref'])
    if local is not None and local not in (plan['source_head'], journal.get('marker') if journal else None):
        raise GdiError('local work branch has advanced; preserve it and reconcile before importing')
    if 'branch ' + plan['source_ref'] + '\n' in git.call('worktree', 'list', '--porcelain').stdout:
        raise GdiError('work branch is checked out in another worktree; release it before importing')
    if journal is None:
        before = git.snapshot()
        git.require_clean(before)
        if before[:2] != (plan['target'][11:], plan['target_head']):
            raise GdiError('target changed since plan; create a new plan')
        squash = plan['existing_squash']
        if squash is None:
            if merged_tree(git, plan['target_head'], plan['source_head']) != plan['tree']:
                raise GdiError('merge result differs from saved plan')
            squash = work.commit_tree(git, plan['tree'], plan['target_head'], plan['message'])
        else:
            if not git.ancestor(squash, plan['target_head']):
                raise GdiError('prior squash no longer belongs to target history')
        marker = work.import_marker(git, value, squash, work.parents(git, squash)[0])
        journal = {**plan, 'squash': squash, 'marker': marker, 'state': 'pending_publication'}
        # Commit refs сохраняют prepared objects даже при обычном git gc после crash.
        git.call('update-ref', 'refs/gdi/imports/' + plan['work'] + '/squash', squash)
        git.call('update-ref', 'refs/gdi/imports/' + plan['work'] + '/marker', marker)
        atomic_write(state_path, encode(journal))
    if not plan['existing_squash']:
        install(git, plan, journal['squash'])
    elif git.oid('HEAD') != plan['target_head']:
        raise GdiError('target changed during recovery; inspect pending import')
    current = git.oid(plan['source_ref'])
    if current != journal['marker']:
        git.call('update-ref', '-m', 'gdi import receipt', plan['source_ref'], journal['marker'], current or '0' * 40)
    return {'work': plan['work'], 'target': plan['target'], 'squash': journal['squash'],
            'marker': journal['marker'], 'state': journal['state'], 'next_step': 'gdi push',
            'journal_path': str(state_path)}


def publish_pending(exchange, name, published_head, target, target_publication):
    """Complete work receipts only after the target publication is confirmed."""
    git = exchange.git
    settings = exchange.remote(name)
    root = git.gdi_dir() / 'imports' / settings['repository_id']
    completed = []
    for path in sorted(root.glob('*.json')):
        value = decode(path.read_bytes())
        if value.get('target') != target or value.get('state') == 'published':
            continue
        if value.get('remote') != name or value.get('url') != settings['url']:
            raise GdiError('pending import connection changed; refusing receipt publication')
        if not git.ancestor(value['squash'], published_head):
            raise GdiError('published target does not contain pending squash; work marker was not published')
        if git.oid(value['source_ref']) != value['marker']:
            raise GdiError('pending work marker changed locally; inspect it before publishing')
        imported = work.validate(git, value['source_ref'], value['marker'], settings['repository_id'])
        if imported['state'] != 'imported' or imported['receipt']['squash'] != value['squash']:
            raise GdiError('pending import receipt is invalid')
        _, marker_publication, _ = exchange.push(name, value['source_ref'][11:])
        from .work_lifecycle import confirm_import
        confirm_import(exchange, name, value, target_publication, marker_publication)
        value['state'] = 'published'
        atomic_write(path, encode(value))
        completed.append({'work': value['work'], 'marker': value['marker'], 'squash': value['squash']})
    return completed


def push(exchange, name, branch=None, **options):
    """Operator orchestration; Exchange itself stays independent of import journals."""
    target = exchange.git.ref(branch if branch is not None else exchange.git.branch())
    result = exchange.push(name, branch, **options)
    publication = exchange.last_publication
    try:
        completed = publish_pending(exchange, name, result[0], target, result[1]) if not target.startswith(work.PREFIX) else []
    finally:
        # Источник последующего CI — target publication, не служебный marker push.
        exchange.last_publication = publication
    return result, completed
