"""Protocol v3: verified full checkpoints and incremental publication chains."""

from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
import uuid

from .git import GdiError, Git
from .branches import branch_directory
from .cache import VerifiedCache
from .transport import Rclone, validate_url
from .diagnostics import note, phase, timed
# Keep existing import locations available while sharing the implementation.
from .publication import (PROTOCOL_VERSION, bundle_prerequisites, decode, digest,
                          encode, file_digest, hex_value, prepare_publication,
                          validate_repository, verify_bundle)


CHECKPOINT_EVERY = 20


def remote_name(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise GdiError("remote name must start with an ASCII letter and contain letters, digits, '_' or '-'")
    return name


class Exchange:
    def __init__(self, git, transport_factory=Rclone):
        self.git = git
        self.transport_factory = transport_factory
        self.last_publication = None

    def key(self, name):
        return "gdi.remote." + remote_name(name)

    def remotes(self):
        from .local_config import load
        return sorted((name, value["url"]) for name, value in load(self.git)["remotes"].items())

    def remote(self, name):
        from .local_config import get
        return get(self.git, name)

    @timed('initialize/join remote')
    def add(self, name, url, *, initialize=False, expected_id=None, inbox_root=None):
        from .local_config import load, save, inferred_root, repository_path
        remote_name(name)
        validate_url(url)
        inbox_root = inbox_root if inbox_root is not None else inferred_root(url)
        if inbox_root is not None:
            repository_path(inbox_root, url)
        settings = load(self.git)
        if name in settings["remotes"]:
            raise GdiError(f"gdi remote {name} already exists")
        if self.git.config(f"remote.{name}.url") is not None:
            raise GdiError(f"Git remote {name} already uses this tracking namespace; choose another name")
        if expected_id is not None and not hex_value(expected_id, 32):
            raise GdiError("repository ID must be 32 lowercase hexadecimal characters")
        if initialize and expected_id is not None:
            raise GdiError("--repository-id is for joining an existing remote; omit it with --init")
        transport = self.transport_factory(url)
        if initialize:
            transport.mkdir("")
            if transport.list(""):
                raise GdiError("--init requires an empty dedicated folder; initialize it once only")
            repository = {"version": PROTOCOL_VERSION, "repository_id": uuid.uuid4().hex, "object_format": "sha1"}
            transport.mkdir("bundles")
            transport.mkdir("branches")
            with tempfile.TemporaryDirectory(prefix="gdi-init-") as tmp:
                path = Path(tmp) / "repository.json"
                path.write_bytes(encode(repository))
                transport.upload(path, "repository.json")
            if decode(transport.read("repository.json")) != repository:
                raise GdiError("repository identity changed during initialization; do not initialize concurrently")
        else:
            repository = validate_repository(decode(transport.read("repository.json")))
        if expected_id is not None and repository["repository_id"] != expected_id:
            raise GdiError("repository ID mismatch")
        settings["remotes"][name] = {"url": url, "repository_id": repository["repository_id"], "inbox_root": inbox_root}
        if settings['default_remote'] is None and len(settings['remotes']) == 1:
            settings['default_remote'] = name
        save(self.git, settings)
        return repository["repository_id"]

    def remove(self, name):
        from .local_config import load, save
        remote_name(name)
        settings = load(self.git)
        if name not in settings["remotes"]:
            raise GdiError(f"unknown gdi remote: {name}")
        del settings["remotes"][name]
        if settings["default_remote"] == name:
            settings["default_remote"] = next(iter(settings["remotes"])) if len(settings["remotes"]) == 1 else None
        save(self.git, settings)

    def clear_cache(self, name):
        VerifiedCache(self.git, self.remote(name)["repository_id"]).clear()

    @timed('verify remote identity')
    def connect(self, name):
        settings = self.remote(name)
        repository_id = settings["repository_id"]
        if self.git.config(f"remote.{name}.url") is not None:
            raise GdiError(f"Git remote {name} conflicts with the gdi tracking namespace")
        transport = self.transport_factory(settings["url"])
        self.verify_identity(transport, repository_id)
        return transport, repository_id

    def verify_identity(self, transport, repository_id):
        repository = validate_repository(decode(transport.read('repository.json')))
        if repository['repository_id'] != repository_id:
            raise GdiError('repository ID mismatch; remote was replaced or configuration points to another project')

    @timed('publish inbox notification')
    def notify_publication(self, name, publication_id):
        from .inbox import notification, publish
        from .local_config import repository_path
        from .ci_protocol import atomic_write
        settings = self.remote(name)
        if settings["inbox_root"] is not None:
            root = self.transport_factory(settings["inbox_root"])
            value = notification(settings["repository_id"], repository_path(settings["inbox_root"], settings["url"]),
                                 self.last_publication, publication_id)
            raw = encode({'root': settings['inbox_root'], 'notification': value})
            receipt = self.git.gdi_dir() / 'notifications' / (digest(raw) + '.json')
            # Successful remote delivery precedes the durable receipt. Missing
            # receipts retry the same immutable event; consumed events need not
            # be recreated on every unchanged push. The root is part of the key.
            if receipt.exists() and receipt.read_bytes() == raw:
                note('publication notification already delivered', publication_id=publication_id)
                return
            publish(root, value)
            atomic_write(receipt, raw)

    @timed('read/verify publication chain')
    def publications(self, transport, repository_id, ref, *, listing=None, metadata=None):
        prefix = branch_directory(ref) + "/"
        records = {}
        if listing is None:
            selected_listing = getattr(transport, 'publication_listing', None)
            listing = (selected_listing(ref) if callable(selected_listing)
                       else transport.list("branches", recursive=True))
        if metadata is None and callable(getattr(transport, 'read_many', None)):
            selected = [item['Path'] for item in listing if not item['IsDir'] and
                        item['Path'].startswith(prefix) and item['Path'].endswith('.json')]
            for path in selected:
                if not hex_value(path[len(prefix):-len('.json')], 64):
                    raise GdiError('invalid publication filename')
            downloaded = transport.read_many('branches/' + path for path in selected)
            if set(downloaded) != {'branches/' + path for path in selected}:
                raise GdiError('incomplete batch publication metadata')
            metadata = {path: downloaded['branches/' + path] for path in selected}
        for item in listing:
            path = item["Path"]
            if item["IsDir"] or not path.startswith(prefix) or not path.endswith(".json"):
                continue
            publication_id = path[len(prefix):-len(".json")]
            if not hex_value(publication_id, 64):
                raise GdiError("invalid publication filename")
            raw = transport.read("branches/" + path) if metadata is None else metadata[path]
            if digest(raw) != publication_id:
                raise GdiError("publication metadata checksum mismatch")
            data = decode(raw)
            required = {"version", "repository_id", "ref", "head", "bundle_sha256", "previous", "nonce",
                        "bundle_kind", "base_publication", "base_head", "prerequisites", "bundle_bytes"}
            if (set(data) != required or type(data.get("version")) is not int or data["version"] != PROTOCOL_VERSION or
                    data.get("repository_id") != repository_id or data.get("ref") != ref or
                    not hex_value(data.get("head"), 40) or not hex_value(data.get("bundle_sha256"), 64) or
                    not hex_value(data.get("nonce"), 32) or
                    type(data.get("bundle_bytes")) is not int or data["bundle_bytes"] <= 0 or
                    not isinstance(data.get("prerequisites"), list) or
                    any(not hex_value(oid, 40) for oid in data["prerequisites"]) or
                    data["prerequisites"] != sorted(set(data["prerequisites"])) or
                    (data.get("previous") is not None and not hex_value(data["previous"], 64))):
                raise GdiError("invalid publication metadata, repository ID, or ref")
            if data["bundle_kind"] == "full":
                if data["base_publication"] is not None or data["base_head"] is not None or data["prerequisites"]:
                    raise GdiError("full publication must not have a base or prerequisites")
            elif data["bundle_kind"] == "incremental":
                if (not hex_value(data["base_publication"], 64) or not hex_value(data["base_head"], 40)
                        or not data["prerequisites"] or data["base_head"] == data["head"]):
                    raise GdiError("invalid incremental publication base or prerequisites")
            else:
                raise GdiError("unsupported publication bundle_kind")
            if publication_id in records:
                raise GdiError("duplicate publication")
            records[publication_id] = data
        if not records:
            return []
        children = {}
        for publication_id, data in records.items():
            parent = data["previous"]
            if parent is not None and parent not in records:
                raise GdiError("incomplete publication chain: predecessor is missing; retry after upload completes")
            if parent in children:
                raise GdiError("conflicting publications: concurrent push detected; no tip was selected")
            children[parent] = publication_id
        current = children.get(None)
        seen = set()
        chain = []
        while current is not None and current not in seen:
            data = records[current]
            if data["bundle_kind"] == "incremental":
                base = data["base_publication"]
                if base not in seen or records[base]["head"] != data["base_head"]:
                    raise GdiError("incremental base must identify an earlier publication with the exact base HEAD")
            seen.add(current)
            chain.append((current, data))
            current = children.get(current)
        if current is not None or len(seen) != len(records):
            raise GdiError("invalid publication chain: cycle or disconnected records")
        return chain

    @timed('log')
    def log(self, name, branch=None, *, limit=20):
        """Fresh, verified publication history without downloading bundles."""
        if type(limit) is not int or limit < 1:
            raise GdiError('log count must be a positive integer')
        branch = branch if branch is not None else self.git.branch()
        ref = self.git.ref(branch)
        transport, repository_id = self.connect(name)
        chain = self.publications(transport, repository_id, ref)
        records = []
        subjects = {}
        for publication_id, data in reversed(chain[-limit:]):
            head = data['head']
            if head not in subjects:
                subjects[head] = (self.git.text('show', '--no-patch', '--format=%s', head)
                                  if self.git.has_commit(head) else None)
            records.append({'publication_id': publication_id, 'head': head,
                            'bundle_kind': data['bundle_kind'], 'bundle_bytes': data['bundle_bytes'],
                            'subject': subjects[head]})
        return {'remote': name, 'url': self.remote(name)['url'], 'repository_id': repository_id,
                'branch': branch, 'total': len(chain), 'publications': records}

    def gc(self, name, *, apply=False, keep_checkpoints=2, quiescent=False, report=print):
        from .gc import collect
        return collect(self, name, apply=apply, keep_checkpoints=keep_checkpoints,
                       quiescent=quiescent, report=report)

    @contextmanager
    def verified(self, bundle, publication, ref, cache, previous=None):
        with verify_bundle(bundle, publication, ref, cache, previous) as quarantine:
            yield quarantine

    @timed('restore verified Git objects')
    def restore(self, transport, repository_id, chain):
        """Find a verified local base or the latest full checkpoint, then replay."""
        cache = VerifiedCache(self.git, repository_id)
        records = dict(chain)
        pending = []
        current = chain[-1][0]
        while True:
            data = records[current]
            if cache.contains(current, data["head"]):
                break
            pending.append((current, data))
            if data["bundle_kind"] == "full":
                break
            current = data["base_publication"]
        for publication_id, data in reversed(pending):
            with tempfile.TemporaryDirectory(prefix="gdi-download-") as tmp:
                bundle = Path(tmp) / "source.bundle"
                transport.download("bundles/" + data["bundle_sha256"] + ".bundle", bundle)
                previous = records.get(data["previous"])
                with self.verified(bundle, data, data["ref"], cache, previous) as quarantine:
                    cache.accept(publication_id, data["head"], quarantine)
        return cache

    @timed('push')
    def push(self, name, branch=None, *, full=False, checkpoint_every=CHECKPOINT_EVERY):
        if type(checkpoint_every) is not int or checkpoint_every < 1:
            raise GdiError("checkpoint interval must be a positive integer")
        branch = branch if branch is not None else self.git.branch()
        ref = self.git.ref(branch)
        head = self.git.oid(ref)
        if head is None:
            raise GdiError(f"branch {branch} has no commit to publish")
        self.git.check_payload(head)
        transport, repository_id = self.connect(name)
        chain = self.publications(transport, repository_id, ref)
        tip = chain[-1] if chain else None
        cache = VerifiedCache(self.git, repository_id)
        if tip is not None:
            # A new full checkpoint can prove the exact old tip and its history
            # using local objects, even if an old remote bundle and the private
            # cache have both been lost. The new full bundle is verified in an
            # empty quarantine before any upload.
            local_full = (full and self.git.has_commit(tip[1]["head"]) and
                          (head != tip[1]["head"] or tip[1]["bundle_kind"] != "full"))
            if not local_full:
                cache = self.restore(transport, repository_id, chain)
                self.git.import_objects(cache.path, tip[1]["head"])
            if head == tip[1]["head"] and (not full or tip[1]["bundle_kind"] == "full"):
                self.verify_identity(transport, repository_id)
                self.last_publication = tip[1]
                self.notify_publication(name, tip[0])
                return head, tip[0], False
            if not self.git.ancestor(tip[1]["head"], head):
                raise GdiError("push is not a fast-forward; fetch/pull and reconcile history first")
        deltas = 0
        for _, data in reversed(chain):
            if data["bundle_kind"] == "full":
                break
            deltas += 1
        is_full = full or tip is None or deltas >= checkpoint_every - 1
        with tempfile.TemporaryDirectory(prefix="gdi-push-") as tmp:
            prepared = prepare_publication(self.git, repository_id, ref, head, tmp,
                                           previous=tip, incremental=not is_full)
            data = prepared.data
            publication_id = prepared.publication_id
            # Recheck before exposing a publication. This detects observed competition,
            # but is not a compare-and-swap or a distributed lock.
            self.verify_identity(transport, repository_id)
            if self.publications(transport, repository_id, ref) != chain:
                raise GdiError("remote changed during push; retry after the other publisher finishes")
            with self.verified(prepared.bundle, data, ref, cache, tip[1] if tip else None) as quarantine:
                with phase('upload Git bundle'):
                    transport.upload(prepared.bundle, prepared.bundle_relative)
                directory = prepared.manifest_relative.rpartition('/')[0]
                with phase('publish manifest'):
                    # Bundle transfer may outlive a folder replacement. Resolve
                    # the current URL again before committing metadata to Drive.
                    self.verify_identity(transport, repository_id)
                    if not getattr(transport, 'creates_parents', False):
                        transport.mkdir(directory)
                    transport.upload(prepared.manifest, prepared.manifest_relative)
                self.verify_identity(transport, repository_id)
                latest = self.publications(transport, repository_id, ref)
                if not latest or latest[-1][0] != publication_id:
                    raise GdiError("publication uploaded, but remote advanced concurrently; inspect with fetch")
                cache.accept(publication_id, head, quarantine)
        self.last_publication = data
        self.notify_publication(name, publication_id)
        return head, publication_id, True

    @timed('fetch')
    def fetch(self, name, branch=None):
        branch = branch if branch is not None else self.git.branch()
        ref = self.git.ref(branch)
        transport, repository_id = self.connect(name)
        chain = self.publications(transport, repository_id, ref)
        if not chain:
            raise GdiError(f"no completed publication for {ref}")
        tip = chain[-1]
        tracking = "refs/remotes/" + remote_name(name) + "/" + branch
        old = self.git.oid(tracking)
        cache = self.restore(transport, repository_id, chain)
        self.git.import_objects(cache.path, tip[1]["head"])
        self.git.call("update-ref", "-m", "gdi fetch", tracking, tip[1]["head"], old or "0" * 40)
        return tip[1]["head"], tip[0], tracking

    @timed('pull')
    def pull(self, name):
        before = self.git.snapshot()
        self.git.require_clean(before)
        head, publication_id, tracking = self.fetch(name, before[0])
        if before[1] is not None and not self.git.ancestor(before[1], head):
            raise GdiError("pull is not a fast-forward; working branch unchanged (fetched ref is available for inspection)")
        now = self.git.snapshot()
        self.git.require_clean(now)
        if now != before:
            raise GdiError("branch or HEAD changed during pull; refusing to apply")
        if before[1] != head:
            # Use the verified immutable OID, not a tracking ref another Git process
            # could change. Ordinary Git processes must not mutate this worktree concurrently.
            self.git.call("merge", "--ff-only", "--no-autostash", head)
        if self.git.branch() != before[0] or self.git.oid("HEAD") != head:
            raise GdiError("unexpected branch/HEAD after fast-forward; inspect concurrent Git activity")
        return head, publication_id, tracking
