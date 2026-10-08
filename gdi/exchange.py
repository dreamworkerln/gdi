"""Protocol v2: verified full checkpoints and incremental publication chains."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import tempfile
import uuid

from .git import GdiError, Git
from .cache import VerifiedCache
from .transport import Rclone, validate_url


PROTOCOL_VERSION = 2
CHECKPOINT_EVERY = 20


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise GdiError(f"duplicate metadata key: {key}")
            result[key] = value
        return result
    if len(data) > 1024 * 1024:
        raise GdiError("metadata exceeds 1 MiB")
    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GdiError("invalid metadata JSON") from exc
    if not isinstance(value, dict):
        raise GdiError("metadata must be a JSON object")
    return value


def hex_value(value, length):
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def remote_name(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise GdiError("remote name must start with an ASCII letter and contain letters, digits, '_' or '-'")
    return name


def validate_repository(data):
    if (set(data) != {"version", "repository_id", "object_format"} or
            type(data.get("version")) is not int or data["version"] != PROTOCOL_VERSION or
            not hex_value(data.get("repository_id"), 32) or data.get("object_format") != "sha1"):
        raise GdiError("unsupported repository metadata: expected protocol v2, SHA-1; use a new empty remote folder for v1 data")
    return data


def bundle_prerequisites(path):
    """Read the actual header; a merge may introduce several prerequisites."""
    prerequisites = []
    size = 0
    with Path(path).open("rb") as handle:
        signature = handle.readline(128)
        if signature not in (b"# v2 git bundle\n", b"# v3 git bundle\n"):
            raise GdiError("unsupported Git bundle header")
        while True:
            raw = handle.readline(1024 * 1024 + 1)
            size += len(raw)
            if not raw or size > 1024 * 1024:
                raise GdiError("invalid or oversized Git bundle header")
            if raw == b"\n":
                break
            if not raw.endswith(b"\n"):
                raise GdiError("invalid Git bundle header line")
            if raw.startswith(b"@"):
                if signature != b"# v3 git bundle\n" or raw != b"@object-format=sha1\n":
                    raise GdiError("unsupported bundle capability (only unfiltered SHA-1 is supported)")
            elif raw.startswith(b"-"):
                oid = raw[1:].split(b" ", 1)[0].decode("ascii")
                if not hex_value(oid, 40) or oid in prerequisites:
                    raise GdiError("invalid or duplicate bundle prerequisite")
                prerequisites.append(oid)
    return sorted(prerequisites)


class Exchange:
    def __init__(self, git, transport_factory=Rclone):
        self.git = git
        self.transport_factory = transport_factory
        self.last_publication = None

    def key(self, name):
        return "gdi.remote." + remote_name(name)

    def remotes(self):
        output = self.git.call("config", "--local", "--get-regexp", r"^gdi\.remote\..*\.url$",
                               allowed=(0, 1)).stdout
        return [(line.split(" ", 1)[0][len("gdi.remote."):-len(".url")],
                 line.split(" ", 1)[1]) for line in output.splitlines()]

    def add(self, name, url, *, initialize=False, expected_id=None):
        key = self.key(name)
        validate_url(url)
        if self.git.config(key + ".url") is not None:
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
            transport.mkdir("updates")
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
        self.git.call("config", "--local", key + ".url", url)
        try:
            self.git.call("config", "--local", key + ".repositoryid", repository["repository_id"])
        except GdiError:
            self.git.call("config", "--local", "--remove-section", key)
            raise
        return repository["repository_id"]

    def remove(self, name):
        key = self.key(name)
        if self.git.config(key + ".url") is None:
            raise GdiError(f"unknown gdi remote: {name}")
        self.git.call("config", "--local", "--remove-section", key)

    def clear_cache(self, name):
        repository_id = self.git.config(self.key(name) + ".repositoryid")
        if not hex_value(repository_id, 32):
            raise GdiError(f"unknown or invalid gdi remote: {name}")
        VerifiedCache(self.git, repository_id).clear()

    def connect(self, name):
        key = self.key(name)
        url = self.git.config(key + ".url")
        repository_id = self.git.config(key + ".repositoryid")
        if url is None or not hex_value(repository_id, 32):
            raise GdiError(f"missing remote configuration: gdi remote add {name} <rclone:path>")
        if self.git.config(f"remote.{name}.url") is not None:
            raise GdiError(f"Git remote {name} conflicts with the gdi tracking namespace")
        transport = self.transport_factory(url)
        repository = validate_repository(decode(transport.read("repository.json")))
        if repository["repository_id"] != repository_id:
            raise GdiError("repository ID mismatch; remote was replaced or configuration points to another project")
        return transport, repository_id

    def publications(self, transport, repository_id, ref, *, listing=None, metadata=None):
        prefix = digest(ref.encode("utf-8")) + "/"
        records = {}
        if listing is None:
            listing = transport.list("updates", recursive=True)
        for item in listing:
            path = item["Path"]
            if item["IsDir"] or not path.startswith(prefix) or not path.endswith(".json"):
                continue
            publication_id = path[len(prefix):-len(".json")]
            if not hex_value(publication_id, 64):
                raise GdiError("invalid publication filename")
            raw = transport.read("updates/" + path) if metadata is None else metadata[path]
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

    def gc(self, name, *, apply=False, keep_checkpoints=2, quiescent=False, report=print):
        from .gc import collect
        return collect(self, name, apply=apply, keep_checkpoints=keep_checkpoints,
                       quiescent=quiescent, report=report)

    @contextmanager
    def verified(self, bundle, publication, ref, cache, previous=None):
        with tempfile.TemporaryDirectory(prefix="gdi-verify-") as tmp:
            root = Path(tmp)
            if file_digest(bundle) != publication["bundle_sha256"]:
                raise GdiError("bundle SHA256 mismatch")
            if Path(bundle).stat().st_size != publication["bundle_bytes"]:
                raise GdiError("bundle size mismatch")
            prerequisites = bundle_prerequisites(bundle)
            if prerequisites != publication["prerequisites"]:
                raise GdiError("bundle prerequisites do not match publication")
            quarantine = root / "repository"
            quarantine.mkdir()
            git = Git(quarantine, isolated=True)
            git.call("init", "--quiet", "--bare", "--object-format=sha1", "--template=")
            if publication["bundle_kind"] == "incremental":
                cache.seed(quarantine)
                for oid in prerequisites:
                    if not git.has_commit(oid) or not git.ancestor(oid, publication["base_head"]):
                        raise GdiError("bundle prerequisite is not in the declared base history")
            git.call("bundle", "verify", str(bundle))
            heads = git.call("bundle", "list-heads", str(bundle)).stdout.splitlines()
            if heads != [publication["head"] + " " + ref]:
                raise GdiError("bundle ref or exact HEAD does not match publication")
            git.call("-c", "fetch.fsckObjects=true", "fetch", "--no-tags", "--no-write-fetch-head",
                     "--", str(bundle), "+" + ref + ":refs/heads/incoming")
            if git.text("cat-file", "-t", publication["head"]) != "commit":
                raise GdiError("publication HEAD is not a commit")
            if previous is not None:
                if not git.has_commit(previous["head"]) or not git.ancestor(previous["head"], publication["head"]):
                    raise GdiError("publication history is not a fast-forward")
            git.check_payload(publication["head"])
            yield quarantine

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
                self.last_publication = tip[1]
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
            bundle = Path(tmp) / "source.bundle"
            exclusions = [] if is_full else ["^" + tip[1]["head"]]
            self.git.call("bundle", "create", str(bundle), ref, *exclusions)
            if self.git.call("bundle", "list-heads", str(bundle)).stdout.splitlines() != [head + " " + ref]:
                raise GdiError("branch changed while creating bundle; retry push")
            self.git.call("bundle", "verify", str(bundle))
            data = {"version": PROTOCOL_VERSION, "repository_id": repository_id, "ref": ref, "head": head,
                    "bundle_sha256": file_digest(bundle), "previous": tip[0] if tip else None,
                    "nonce": uuid.uuid4().hex, "bundle_bytes": bundle.stat().st_size,
                    "bundle_kind": "full" if is_full else "incremental",
                    "base_publication": None if is_full else tip[0],
                    "base_head": None if is_full else tip[1]["head"],
                    "prerequisites": bundle_prerequisites(bundle)}
            raw = encode(data)
            publication_id = digest(raw)
            manifest = Path(tmp) / "publication.json"
            manifest.write_bytes(raw)
            # Recheck before exposing a publication. This detects observed competition,
            # but is not a compare-and-swap or a distributed lock.
            if self.publications(transport, repository_id, ref) != chain:
                raise GdiError("remote changed during push; retry after the other publisher finishes")
            with self.verified(bundle, data, ref, cache, tip[1] if tip else None) as quarantine:
                transport.upload(bundle, "bundles/" + data["bundle_sha256"] + ".bundle")
                directory = "updates/" + digest(ref.encode("utf-8"))
                transport.mkdir(directory)
                transport.upload(manifest, directory + "/" + publication_id + ".json")
                latest = self.publications(transport, repository_id, ref)
                if not latest or latest[-1][0] != publication_id:
                    raise GdiError("publication uploaded, but remote advanced concurrently; inspect with fetch")
                cache.accept(publication_id, head, quarantine)
        self.last_publication = data
        return head, publication_id, True

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
