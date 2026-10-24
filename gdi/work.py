"""Numbered agent work branches; the assigned target is never a checkpoint branch."""

from pathlib import Path
import re
import tempfile

from .ci_protocol import atomic_write
from .git import GdiError
from .publication import decode, encode, hex_value

PREFIX = 'refs/heads/gdi-work/'
PART = re.compile(r'^\[([1-9][0-9]*)\.([1-9][0-9]*)(!)?\] (\S[^\n]*)$')
MARKER = re.compile(r'^\[GDI-IMPORTED ([1-9][0-9]*)\]$')
FIELDS = ('GDI-Work', 'GDI-Repository', 'GDI-Target-Ref', 'GDI-Base')


def work_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]*', value):
        raise GdiError('work ID must be a positive integer without leading zeroes; obtain it from the operator')
    return value


def message(git, oid):
    return git.call('cat-file', 'commit', oid).stdout.split('\n\n', 1)[1]


def parents(git, oid):
    return git.text('rev-list', '--parents', '-n', '1', oid).split()[1:]


def field(body, name):
    values = re.findall(r'^' + re.escape(name) + r': (.+)$', body, re.M)
    if len(values) != 1:
        raise GdiError('work metadata requires exactly one ' + name)
    return values[0]


def binding(git, body, repository_id=None):
    result = dict(zip(('work', 'repository_id', 'target', 'base'),
                      (field(body, key) for key in FIELDS)))
    work_id(result['work'])
    if not hex_value(result['repository_id'], 32) or not hex_value(result['base'], 40):
        raise GdiError('invalid work repository ID or base SHA')
    if repository_id is not None and result['repository_id'] != repository_id:
        raise GdiError('work repository ID mismatch')
    if not result['target'].startswith('refs/heads/') or result['target'].startswith(PREFIX):
        raise GdiError('work target must be an ordinary refs/heads/ branch')
    git.ref(result['target'][11:])
    return result


def trailers(value):
    return '\n'.join(key + ': ' + value[name] for key, name in
                     zip(FIELDS, ('work', 'repository_id', 'target', 'base')))


def validate(git, ref, head, repository_id=None):
    """Validate actual parent links and every part, including an optional import marker."""
    if not ref.startswith(PREFIX):
        raise GdiError('expected a gdi-work/<WORK_ID> branch')
    ident = work_id(ref[len(PREFIX):])
    raw = message(git, head)
    marker = MARKER.fullmatch(raw.split('\n', 1)[0])
    receipt = None
    final = head
    if marker:
        links = parents(git, head)
        if marker[1] != ident or len(links) != 1:
            raise GdiError('invalid GDI-IMPORTED parent or work ID')
        final = links[0]
        if git.text('rev-parse', head + '^{tree}') != git.text('rev-parse', final + '^{tree}'):
            raise GdiError('GDI-IMPORTED must be an empty commit')
        receipt = {'marker': head, 'final': field(raw, 'GDI-Final'),
                   'squash': field(raw, 'GDI-Squash'), 'target_before': field(raw, 'GDI-Target-Before')}
        if receipt['final'] != final or not all(hex_value(receipt[k], 40) for k in ('squash', 'target_before')):
            raise GdiError('invalid import receipt SHA')
        raw_final = message(git, final)
        if binding(git, raw, repository_id) != binding(git, raw_final, repository_id):
            raise GdiError('import marker changed work binding')
        raw = raw_final
    value = binding(git, raw, repository_id)
    if value['work'] != ident or not git.has_commit(value['base']):
        raise GdiError('work ID/ref mismatch or missing base')
    cursor = final
    commits = []
    while cursor != value['base']:
        body = message(git, cursor)
        part = PART.fullmatch(body.split('\n', 1)[0])
        if not part or part[1] != ident or binding(git, body, repository_id) != value:
            raise GdiError('foreign commit or inconsistent work binding: ' + cursor)
        links = parents(git, cursor)
        if len(links) != 1:
            raise GdiError('work chain must be linear and single-parent')
        commits.append((cursor, int(part[2]), bool(part[3])))
        cursor = links[0]
    commits.reverse()
    if not commits:
        raise GdiError('work has no numbered checkpoints')
    for index, (oid, number, done) in enumerate(commits, 1):
        if number != index:
            raise GdiError(f'work {ident}: expected part {index}, found {number} at {oid}')
        if done and index != len(commits):
            raise GdiError('final checkpoint must be the last numbered part')
    done = commits[-1][2]
    if receipt and not done:
        raise GdiError('GDI-IMPORTED parent must be a final checkpoint')
    return {**value, 'ref': ref, 'head': head, 'final': final, 'parts': len(commits),
            'state': 'imported' if receipt else 'ready' if done else 'active',
            'message': PART.sub(lambda match: match[4], raw.split('\n', 1)[0]) +
                       ('\n' + raw.split('\n', 1)[1] if '\n' in raw else ''),
            'receipt': receipt}


def start(git, ident, repository_id):
    ident = work_id(ident)
    if not hex_value(repository_id, 32):
        raise GdiError('a trusted repository ID is required')
    before = git.snapshot()
    git.require_clean(before)
    target = git.ref(before[0])
    if target.startswith(PREFIX) or before[1] is None:
        raise GdiError('start work from the assigned ordinary branch with a committed HEAD')
    ref = PREFIX + ident
    if git.oid(ref) is not None:
        raise GdiError('work branch already exists; resume it instead of reusing its ID')
    value = {'work': ident, 'repository_id': repository_id, 'target': target, 'base': before[1]}
    path = git.gdi_dir() / 'work' / (ident + '.json')
    if path.exists():
        raise GdiError('work ID already allocated locally; obtain a new operator-assigned ID')
    atomic_write(path, encode(value))
    git.call('switch', '-c', ref[11:], before[1])
    return {**value, 'ref': ref, 'head': before[1], 'state': 'started', 'next_part': 1}


def current(git, repository_id=None):
    ref = git.ref(git.branch())
    if not ref.startswith(PREFIX):
        raise GdiError('checkpoints require a separate work branch; run gdi agent work start WORK_ID first')
    ident = work_id(ref[len(PREFIX):])
    head = git.oid(ref)
    path = git.gdi_dir() / 'work' / (ident + '.json')
    if path.exists():
        value = decode(path.read_bytes())
        if set(value) != {'work', 'repository_id', 'target', 'base'}:
            raise GdiError('invalid local work context')
        binding(git, trailers(value), repository_id)
        if value['work'] != ident:
            raise GdiError('local work context/ref mismatch')
        if head == value['base']:
            return {**value, 'ref': ref, 'head': head, 'state': 'started', 'next_part': 1}
    value = validate(git, ref, head, repository_id)
    return {**value, 'next_part': value['parts'] + 1}


def checkpoint(git, title, *, body='', final=False, repository_id=None):
    value = current(git, repository_id)
    if value['state'] not in ('active', 'started'):
        raise GdiError('work is final or imported; use a new work ID for another task')
    if not title.strip() or '\n' in title or '\r' in title:
        raise GdiError('checkpoint title must be one nonempty line')
    if any(re.search(r'^' + re.escape(key) + ':', body, re.M) for key in FIELDS):
        raise GdiError('do not supply generated work metadata in the checkpoint body')
    # Агент сам выбирает и stage-ит файлы; команда не делает git add -A за него.
    if git.call('diff', '--quiet', allowed=(0, 1)).returncode:
        raise GdiError('unstaged changes remain; review and stage the intended checkpoint first')
    untracked = git.call('ls-files', '--others', '--exclude-standard', '--', '.', ':(exclude).gdi').stdout
    if untracked:
        raise GdiError('untracked files remain; review them before checkpointing')
    # require_clean проверяет незавершённые операции отдельно от staged diff.
    git.require_clean((git.branch(), git.oid('HEAD'), ''))
    text = f"[{value['work']}.{value['next_part']}{'!' if final else ''}] {title.strip()}\n\n"
    text += (body.rstrip() + '\n\n' if body else '') + trailers(value) + '\n'
    with tempfile.TemporaryDirectory(prefix='gdi-work-message-') as tmp:
        path = Path(tmp) / 'message'
        path.write_text(text, encoding='utf-8')
        git.call('commit', '--allow-empty', '--cleanup=verbatim', '-F', str(path))
    return validate(git, value['ref'], git.oid(value['ref']), value['repository_id'])


def commit_tree(git, tree, parent, text):
    with tempfile.TemporaryDirectory(prefix='gdi-commit-message-') as tmp:
        path = Path(tmp) / 'message'
        path.write_text(text, encoding='utf-8')
        return git.text('commit-tree', tree, '-p', parent, '-F', str(path))


def import_marker(git, value, squash, target_before):
    text = f"[GDI-IMPORTED {value['work']}]\n\n" + trailers(value) + '\n'
    text += f"GDI-Final: {value['final']}\nGDI-Squash: {squash}\nGDI-Target-Before: {target_before}\n"
    return commit_tree(git, git.text('rev-parse', value['final'] + '^{tree}'), value['final'], text)
