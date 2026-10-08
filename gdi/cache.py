"""Disposable verified object cache, private to a local repository and remote ID."""

from pathlib import Path
import os
import shutil

from .git import GdiError, Git


class VerifiedCache:
    def __init__(self, owner, repository_id):
        self.root = owner.common_dir() / "gdi-cache" / repository_id
        self.path = self.root / "repository.git"
        self.git = Git(self.path, isolated=True)

    def initialize(self):
        if not (self.path / "HEAD").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            self.git.call("init", "--quiet", "--bare", "--object-format=sha1", "--template=")
            # Every accepted publication has a retaining ref. No background maintenance
            # may race an operation that seeds a quarantine repository from this cache.
            self.git.call("config", "gc.auto", "0")
            self.git.call("config", "maintenance.auto", "false")

    def clear(self):
        if self.root.exists():
            shutil.rmtree(self.root)

    def contains(self, publication_id, head):
        self.initialize()
        try:
            cached = self.git.oid("refs/gdi/publications/" + publication_id)
            if cached is None:
                return False
            if cached != head:
                raise GdiError("cache publication ref mismatch")
            self.git.call("fsck", "--connectivity-only", "--no-dangling", "--no-reflogs", head)
        except GdiError:
            # The cache is not the source of truth. Discard broken local state and
            # reconstruct it from verified remote checkpoints and deltas.
            self.clear()
            self.initialize()
            return False
        return True

    def accept(self, publication_id, head, quarantine):
        self.initialize()
        self.git.import_objects(Path(quarantine), head)
        self.git.call("update-ref", "refs/gdi/publications/" + publication_id, head)

    def seed(self, quarantine):
        # Borrow only immutable objects while this gdi operation holds the local
        # repository lock. Quarantine refs/config/index remain private. Git fetch
        # copies accepted new objects back; no alternates are retained in the cache.
        raw = os.fsencode(self.path / "objects")
        quoted = b'"' + b"".join(
            (f"\\{byte:03o}".encode("ascii") if byte < 32 or byte >= 127 or byte in (34, 92)
             else bytes([byte])) for byte in raw) + b'"\n'
        (Path(quarantine) / "objects/info/alternates").write_bytes(quoted)
