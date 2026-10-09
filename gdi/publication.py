"""Transport-independent protocol v3 publication preparation and verification."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import uuid

from .branches import branch_directory
from .diagnostics import phase
from .git import GdiError, Git


PROTOCOL_VERSION = 3


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


def validate_repository(data):
    if (set(data) != {"version", "repository_id", "object_format"} or
            type(data.get("version")) is not int or data["version"] != PROTOCOL_VERSION or
            not hex_value(data.get("repository_id"), 32) or data.get("object_format") != "sha1"):
        raise GdiError("unsupported repository metadata: expected protocol v3, SHA-1; recreate the remote in an empty folder")
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


@dataclass(frozen=True)
class PreparedPublication:
    """Local artifacts and exact canonical metadata; the caller owns their lifetime."""

    bundle: Path
    manifest: Path
    manifest_bytes: bytes

    @property
    def data(self):
        # Return a fresh value so callers cannot mutate the saved canonical metadata.
        return decode(self.manifest_bytes)

    @property
    def publication_id(self):
        return digest(self.manifest_bytes)

    @property
    def bundle_relative(self):
        return 'bundles/' + self.data['bundle_sha256'] + '.bundle'

    @property
    def manifest_relative(self):
        return 'branches/' + branch_directory(self.data['ref']) + '/' + self.publication_id + '.json'


def prepare_publication(git, repository_id, ref, head, directory, *, previous=None,
                        incremental=False, nonce=None):
    """Create local bundle/manifest. Call verify_bundle before publishing either file.

    previous is the (ID, metadata) tip of an already validated publication chain.
    This function performs no remote I/O and does not move refs or create commits.
    The output directory is caller-owned; existing artifacts are never overwritten.
    """
    if not hex_value(repository_id, 32) or not hex_value(head, 40):
        raise GdiError('invalid repository ID or publication HEAD')
    branch_directory(ref)
    git.ref(ref[len('refs/heads/'):])
    if git.oid(ref) != head:
        raise GdiError('branch changed while preparing publication; retry push')
    if type(incremental) is not bool:
        raise GdiError('incremental must be boolean')
    if nonce is None:
        nonce = uuid.uuid4().hex
    if not hex_value(nonce, 32):
        raise GdiError('publication nonce must be 32 lowercase hexadecimal characters')
    if incremental and previous is None:
        raise GdiError('incremental publication requires a previous publication')
    if previous is not None:
        if (not isinstance(previous, (tuple, list)) or len(previous) != 2
                or not isinstance(previous[1], dict)):
            raise GdiError('previous publication must contain its ID and validated metadata')
        previous_id, base = previous
        if (not hex_value(previous_id, 64) or base.get('repository_id') != repository_id
                or base.get('ref') != ref or not hex_value(base.get('head'), 40)):
            raise GdiError('previous publication identity does not match the requested publication')
        if not git.ancestor(base['head'], head):
            raise GdiError('push is not a fast-forward; fetch/pull and reconcile history first')
        if incremental and base['head'] == head:
            raise GdiError('incremental publication must advance HEAD')
    git.check_payload(head)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    bundle, manifest = root / 'source.bundle', root / 'publication.json'
    if any(path.exists() or path.is_symlink() for path in (bundle, manifest)):
        raise GdiError('publication output already exists; reuse its artifacts or choose a new directory')
    with phase('create/check Git bundle'):
        exclusions = ['^' + previous[1]['head']] if incremental else []
        git.call('bundle', 'create', str(bundle), ref, *exclusions)
        if git.call('bundle', 'list-heads', str(bundle)).stdout.splitlines() != [head + ' ' + ref]:
            raise GdiError('branch changed while creating bundle; retry push')
        git.call('bundle', 'verify', str(bundle))
    data = {'version': PROTOCOL_VERSION, 'repository_id': repository_id, 'ref': ref, 'head': head,
            'bundle_sha256': file_digest(bundle), 'previous': previous[0] if previous else None,
            'nonce': nonce, 'bundle_bytes': bundle.stat().st_size,
            'bundle_kind': 'incremental' if incremental else 'full',
            'base_publication': previous[0] if incremental else None,
            'base_head': previous[1]['head'] if incremental else None,
            'prerequisites': bundle_prerequisites(bundle)}
    raw = encode(data)
    with manifest.open('xb') as handle:
        handle.write(raw)
    return PreparedPublication(bundle, manifest, raw)


@contextmanager
def verify_bundle(bundle, publication, ref, cache=None, previous=None):
    """Verify objects for validated metadata without network access or accepting refs.

    The caller validates publication identity/schema and the metadata chain first.
    Full bundles need no cache; incremental bundles borrow only verified base objects.
    The quarantine exists until the context exits, so the caller can accept its objects.
    """
    if publication['bundle_kind'] == 'incremental' and cache is None:
        raise GdiError('incremental verification requires a verified base cache')
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



def validate_publications(repository_id, ref, listing, metadata):
    """Validate exact manifest bytes and a complete listing, independent of transport."""
    prefix = branch_directory(ref) + "/"
    if not isinstance(listing, list) or any(not isinstance(item, dict) or
            not isinstance(item.get('Path'), str) or type(item.get('IsDir')) is not bool
            for item in listing):
        raise GdiError('invalid publication listing')
    names = [item['Path'] for item in listing]
    if len(names) != len(set(names)):
        raise GdiError('duplicate publication paths')
    selected = {item['Path'] for item in listing if not item['IsDir'] and
                item['Path'].startswith(prefix) and item['Path'].endswith('.json')}
    if not isinstance(metadata, dict) or not selected.issubset(metadata):
        raise GdiError('incomplete publication metadata')
    records = {}
    for item in listing:
        path = item["Path"]
        if item["IsDir"] or not path.startswith(prefix) or not path.endswith(".json"):
            continue
        publication_id = path[len(prefix):-len(".json")]
        if not hex_value(publication_id, 64):
            raise GdiError("invalid publication filename")
        raw = metadata[path]
        data = validate_manifest(raw, publication_id, repository_id, ref)
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


def validate_manifest(raw, publication_id, repository_id, ref):
    """Validate a single manifest; chain completeness is checked separately."""
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
    return data


def bundle_sequence(chain, contains=None):
    """Checkpoint/base replay order for an already validated chain."""
    if not chain:
        return []
    records, pending = dict(chain), []
    current = chain[-1][0]
    while True:
        data = records[current]
        if contains is not None and contains(current, data['head']):
            break
        pending.append((current, data))
        if data['bundle_kind'] == 'full':
            break
        current = data['base_publication']
    return list(reversed(pending))
