"""Offline work commands; snapshots and Git, never rclone or local remote settings."""

from .branches import branch_directory
from .git import GdiError
from .snapshot import Snapshot
from . import work


def start(git, snapshot_path, repository_id, ident):
    before = git.snapshot()
    git.require_clean(before)
    snapshot = Snapshot(snapshot_path, repository_id, ref=git.ref(before[0]))
    if not snapshot.chain or snapshot.chain[-1][1]['head'] != before[1]:
        raise GdiError('start requires the exact assigned branch HEAD from a fresh connector snapshot')
    ref = work.PREFIX + work.work_id(ident)
    from .work_lifecycle import require_active
    require_active(snapshot, repository_id, ref)
    directory = branch_directory(ref)
    if any(item['Path'] == directory for item in snapshot.list('branches')):
        raise GdiError('work ID already exists on Drive; restore that work or obtain a new operator-assigned ID')
    return work.start(git, ident, repository_id)


def status(git, repository_id):
    return work.current(git, repository_id)


def checkpoint(git, repository_id, title, *, body='', final=False):
    return work.checkpoint(git, title, body=body, final=final, repository_id=repository_id)
