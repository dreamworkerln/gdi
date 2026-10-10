"""Fresh publication status without downloading bundles or changing Git refs."""

from .git import GdiError
from .local_config import load
from .terminal import colorize


def inspect(exchange, name=None):
    git = exchange.git
    config = load(git)
    symbolic = git.call('symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1))
    branch = symbolic.stdout.strip().removeprefix('refs/heads/') if symbolic.returncode == 0 else None
    head = git.oid('HEAD')
    dirty = bool(git.call('status', '--porcelain=v1', '--untracked-files=all', '--', '.', ':(exclude).gdi').stdout)
    names = [name] if name is not None else sorted(config['remotes'])
    if name is not None:
        exchange.remote(name)
    connections = []
    for selected in names:
        settings = config['remotes'][selected]
        value = {'name': selected, 'default': selected == config['default_remote'],
                 'url': settings['url'], 'repository_id': settings['repository_id'],
                 'publication_id': None, 'published_head': None}
        try:
            transport, repository_id = exchange.connect(selected)
            chain = exchange.publications(transport, repository_id, git.ref(branch)) if branch else []
            if branch is None:
                value['state'] = 'detached'
            elif not chain:
                value['state'] = 'unpublished'
            else:
                value['publication_id'], publication = chain[-1]
                remote_head = value['published_head'] = publication['head']
                if head == remote_head:
                    value['state'] = 'published'
                elif head is None:
                    value['state'] = 'behind'
                elif not git.has_commit(remote_head):
                    value['state'] = 'unknown'
                elif git.ancestor(remote_head, head):
                    value['state'] = 'ahead'
                elif git.ancestor(head, remote_head):
                    value['state'] = 'behind'
                else:
                    value['state'] = 'diverged'
        except (GdiError, OSError, UnicodeError) as exc:
            value.update(state='error', error=str(exc))
        connections.append(value)
    return {'branch': branch, 'head': head, 'dirty': dirty,
            'default_remote': config['default_remote'], 'connections': connections}


def display(value):
    print('Branch: ' + (value['branch'] or '(detached HEAD)'))
    print('HEAD: ' + (value['head'] or '(no commits)'))
    worktree = 'changes present' if value['dirty'] else 'clean'
    worktree = colorize(worktree, 31 if value['dirty'] else 32)
    print('Worktree: ' + worktree)
    if not value['connections']:
        print('Connections: none; gdi remote add drive <rclone:path> --init')
    descriptions = {'published': 'Nothing to push.', 'unpublished': 'branch is not published',
                    'ahead': 'local HEAD is ahead; push needed', 'behind': 'local HEAD is behind; pull needed',
                    'diverged': 'histories diverged', 'unknown': 'remote HEAD is missing locally; fetch to compare',
                    'detached': 'switch to a branch to inspect its publication', 'error': 'remote check failed'}
    for remote in value['connections']:
        print(f"Connection: {remote['name']}{' (default)' if remote['default'] else ''} — {remote['url']}")
        print('  Repository ID: ' + remote['repository_id'])
        state = descriptions[remote['state']]
        if remote['state'] in ('ahead', 'published'):
            state = colorize(state, 33)
        print('  State: ' + state)
        if remote['publication_id']:
            print('  Published HEAD: ' + remote['published_head'])
            print('  Publication: ' + remote['publication_id'])
        if 'error' in remote:
            print('  Error: ' + remote['error'])
