"""Local initialization and verified online clones for the ordinary CLI."""

import os
from pathlib import Path
import tempfile

from .branches import directory_ref
from .exchange import Exchange
from .git import GdiError, Git
from .local_config import load, save
from .transport import Rclone, validate_url


def initialize(path='.', *, branch=None):
    target = Path(path).expanduser().absolute()
    if target.is_symlink():
        raise GdiError('init destination must not be a symlink')
    target.mkdir(parents=True, exist_ok=True)
    git = Git(target)
    if branch is not None:
        git.ref(branch)
    marker = target / '.git'
    if marker.is_symlink():
        raise GdiError('.git must not be a symlink')
    if not marker.exists():
        bare = git.call('rev-parse', '--is-bare-repository', allowed=(0, 128))
        if bare.returncode == 0 and bare.stdout.strip() == 'true':
            raise GdiError('gdi requires a git worktree; bare user repositories are unsupported')
        options = ('--initial-branch=' + branch,) if branch is not None else ()
        git.call('init', '--quiet', '--object-format=sha1', '--template=', *options)
    git = Git.discover(target)
    if git.call('ls-files', '--', '.gdi').stdout:
        raise GdiError('.gdi is local metadata and must not be tracked by git')
    with git.lock():
        settings = load(git)
        if not (git.gdi_dir() / 'config.json').exists():
            save(git, settings)
        exclude = Path(git.text('rev-parse', '--git-path', 'info/exclude'))
        if not exclude.is_absolute():
            exclude = git.path / exclude
        if exclude.is_symlink():
            raise GdiError('git info/exclude must not be a symlink')
        previous = exclude.read_bytes() if exclude.exists() else b''
        if previous.splitlines()[-1:] != [b'/.gdi/']:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open('ab') as output:
                output.write((b'\n' if previous and not previous.endswith(b'\n') else b'') + b'/.gdi/\n')
        if git.call('check-ignore', '-q', '--', '.gdi/config.json', allowed=(0, 1)).returncode:
            raise GdiError('project ignore rules reinclude .gdi; add /.gdi/ to .gitignore before using GDI')
    return git


def clone(url, destination=None, *, branch=None, expected_id=None, inbox_root=None,
          transport_factory=Rclone):
    validate_url(url)
    target = Path(destination if destination is not None else url.split(':', 1)[1].rsplit('/', 1)[-1])
    target = target.expanduser().absolute()

    def check_destination():
        if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise GdiError('clone destination must be absent or an empty directory; existing files are preserved')
        # Замена inode текущего каталога оставила бы вызывающую оболочку в удалённой папке.
        if target.exists() and target.samefile(Path.cwd()):
            raise GdiError('clone destination must not be the current directory; choose a new or another empty directory')

    check_destination()
    target.parent.mkdir(parents=True, exist_ok=True)
    # До полной проверки данные находятся рядом с целью; отказ не оставляет частичный checkout.
    with tempfile.TemporaryDirectory(prefix='.gdi-clone-', dir=target.parent) as temporary:
        source = Path(temporary) / 'repository'
        git = initialize(source, branch=branch)
        exchange = Exchange(git, transport_factory)
        with git.lock():
            identity = exchange.add('drive', url, expected_id=expected_id, inbox_root=inbox_root)
            transport, repository_id = exchange.connect('drive')
            published = []
            for entry in transport.list('branches'):
                if not entry['IsDir']:
                    raise GdiError('remote branches must contain only branch directories')
                ref = directory_ref(entry['Path'])
                git.ref(ref[11:])
                if exchange.publications(transport, repository_id, ref):
                    published.append(ref[11:])
            published.sort()
            selected = branch
            if selected is not None and selected not in published:
                raise GdiError('no completed publication for refs/heads/' + selected)
            if selected is None:
                if len(published) > 1:
                    raise GdiError('remote has multiple published branches; choose --branch NAME: ' +
                                   ', '.join(published))
                selected = published[0] if published else None
            for name in published:
                exchange.fetch('drive', name)
            if selected is not None:
                git.call('checkout', '--quiet', '--no-track', '-b', selected, 'refs/remotes/drive/' + selected)
            if git.call('check-ignore', '-q', '--', '.gdi/config.json', allowed=(0, 1)).returncode:
                raise GdiError('project ignore rules reinclude .gdi; add /.gdi/ to the source .gitignore before cloning')
            exchange.verify_identity(transport, identity)
            result = {'destination': str(target), 'repository_id': identity, 'branch': git.branch(),
                      'head': git.oid('HEAD'), 'branches': published}
        check_destination()
        os.rename(source, target)
    return result
