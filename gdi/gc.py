"""Manual remote bundle collection during a coordinated pause of all clients.

Metadata remains immutable and complete. This is deliberately not an online GC:
snapshot checks detect observed changes but cannot replace a distributed lock.
"""

from dataclasses import dataclass
from pathlib import Path
import re
import tempfile

from .branches import branch_directory, directory_ref
from .cache import VerifiedCache
from .exchange import decode, digest, encode
from .git import GdiError, Git


@dataclass(frozen=True)
class BundleFile:
    path: str
    size: int


@dataclass(frozen=True)
class BranchPlan:
    ref: str
    head: str
    publication: str
    checkpoints: tuple
    retained: tuple


@dataclass(frozen=True)
class GcPlan:
    repository_id: str
    keep_checkpoints: int
    branches: tuple
    candidates: tuple
    orphans: tuple
    retained_bundles: int

    @property
    def reclaim_bytes(self):
        return sum(bundle.size for bundle in self.candidates)


@dataclass(frozen=True)
class Snapshot:
    metadata: dict
    directories: tuple
    chains: dict
    bundles: dict
    lifecycle: tuple = ()


def snapshot(exchange, transport, repository_id):
    """Read every branch from one listing, rejecting unknown metadata/bundle paths."""
    from .work_lifecycle import records, archived_chain
    from .publication import digest, encode
    retired = records(transport, repository_id, 'retired')
    imports = records(transport, repository_id, 'imports')
    lifecycle = tuple((kind, ident, digest(encode(value))) for kind, values in
                      (('imports', imports), ('retired', retired)) for ident, value in sorted(values.items()))
    retired_refs = {value['ref']: value for value in retired.values()}
    listing = transport.list("branches", recursive=True)
    metadata = {}
    directories = []
    refs = set()
    for item in listing:
        path = item["Path"]
        if item["IsDir"]:
            exchange.git.ref(directory_ref(path)[len("refs/heads/"):])
            if directory_ref(path) not in retired_refs:
                directories.append(path)
            continue
        if not re.fullmatch(r"[^/]+/[0-9a-f]{64}\.json", path):
            raise GdiError("GC found an unexpected metadata file: " + path)
        if path in metadata:
            raise GdiError("GC found duplicate metadata paths")
        raw = transport.read("branches/" + path)
        data = decode(raw)
        ref = data.get("ref")
        if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
            raise GdiError("GC found an invalid branch ref")
        exchange.git.ref(ref[len("refs/heads/"):])
        if path.split("/", 1)[0] != branch_directory(ref):
            raise GdiError("GC metadata directory does not match its branch ref")
        if ref in retired_refs:
            publication = path.rsplit('/', 1)[1][:-5]
            if publication not in retired_refs[ref]['manifests'] or digest(raw) != publication:
                raise GdiError('retired work has unexpected or changed metadata; keep clients paused')
            continue
        refs.add(ref)
        metadata[path] = raw
    archived = {}
    for value in retired.values():
        if value['archive']:
            archived[value['ref']] = archived_chain(value)
            prefix = branch_directory(value['ref']) + '/'
            metadata.update({prefix + pub + '.json': raw.encode('utf-8')
                             for pub, raw in value['manifests'].items()})
    # Reuse the reader's strict version, identity, checksum, base and chain checks.
    chains = {ref: exchange.publications(transport, repository_id, ref,
                                         listing=listing, metadata=metadata)
              for ref in sorted(refs)}
    chains.update(archived)
    bundles = {}
    for item in transport.list("bundles", recursive=True):
        path = item["Path"]
        if item["IsDir"] or not re.fullmatch(r"[0-9a-f]{64}\.bundle", path):
            raise GdiError("GC found an unexpected bundle path: " + path)
        size = item.get("Size")
        if type(size) is not int or size < 0:
            raise GdiError("GC requires a valid listed size for every bundle")
        if path in bundles:
            raise GdiError("GC found duplicate bundle paths")
        bundles[path] = size
    for chain in chains.values():
        for _, data in chain:
            path = data["bundle_sha256"] + ".bundle"
            if path in bundles and bundles[path] != data["bundle_bytes"]:
                raise GdiError("GC bundle size does not match metadata: " + path)
    return Snapshot(metadata, tuple(sorted(directories)), chains, bundles, lifecycle)


def plan_collection(state, repository_id, keep_checkpoints):
    branches = []
    referenced = set()
    retained_bundles = set()
    for ref, chain in state.chains.items():
        records = dict(chain)
        full_positions = [index for index, (_, data) in enumerate(chain)
                          if data["bundle_kind"] == "full"]
        if not full_positions:
            raise GdiError("GC requires a full checkpoint for every branch")
        start = full_positions[-keep_checkpoints] if len(full_positions) >= keep_checkpoints else full_positions[0]
        retained = {publication for publication, _ in chain[start:]}
        # v3 permits bases older than the immediately previous publication. Keep
        # the entire dependency closure, including any older full checkpoint.
        pending = list(retained)
        while pending:
            data = records[pending.pop()]
            base = data["base_publication"]
            if base is not None and base not in retained:
                retained.add(base)
                pending.append(base)
        for publication, data in chain:
            path = data["bundle_sha256"] + ".bundle"
            referenced.add(path)
            if publication in retained:
                if path not in state.bundles:
                    raise GdiError("GC retained bundle is missing: " + path)
                retained_bundles.add(path)
        checkpoints = tuple((publication, data["head"]) for publication, data in chain
                            if publication in retained and data["bundle_kind"] == "full")
        branches.append(BranchPlan(ref, chain[-1][1]["head"], chain[-1][0], checkpoints,
                                   tuple(publication for publication, _ in chain if publication in retained)))
    candidates = tuple(BundleFile("bundles/" + path, size) for path, size in sorted(state.bundles.items())
                       if path in referenced and path not in retained_bundles)
    orphans = tuple(BundleFile("bundles/" + path, size) for path, size in sorted(state.bundles.items())
                    if path not in referenced)
    return GcPlan(repository_id, keep_checkpoints, tuple(branches), candidates, orphans, len(retained_bundles))


def verify_retained(exchange, transport, repository_id, state, plan, report):
    """Download and verify retained publications without touching the owner's cache."""
    with tempfile.TemporaryDirectory(prefix="gdi-gc-verify-") as tmp:
        for number, branch in enumerate(plan.branches):
            report(f"Verifying {branch.ref} from scratch...")
            owner_path = Path(tmp) / str(number)
            owner_path.mkdir()
            owner = Git(owner_path, isolated=True)
            owner.call("init", "--quiet", "--bare", "--object-format=sha1", "--template=")
            cache = VerifiedCache(owner, repository_id)
            records = dict(state.chains[branch.ref])
            for publication in branch.retained:
                data = records[publication]
                base = data["base_publication"]
                if base is not None and not cache.contains(base, data["base_head"]):
                    raise GdiError("GC could not reconstruct a retained incremental base")
                bundle = Path(tmp) / "download.bundle"
                transport.download("bundles/" + data["bundle_sha256"] + ".bundle", bundle)
                previous = records.get(data["previous"])
                with exchange.verified(bundle, data, branch.ref, cache, previous) as quarantine:
                    cache.accept(publication, data["head"], quarantine)
                bundle.unlink()
            cache.git.call("fsck", "--full", "--strict", "--no-dangling", "--no-reflogs")
            if not cache.contains(branch.publication, branch.head):
                raise GdiError("GC could not reconstruct the exact branch HEAD")
            report(f"Verified {branch.ref}: {branch.head} (Publication: {branch.publication})")


def ci_guard(exchange, transport, repository_id, *, inbox_root=None):
    """Fail closed on incomplete jobs; all CI writers must also be paused."""
    from .ci_protocol import marker, request, validate_result, validate_cancellation
    pending = {}
    inbox_state = []
    if inbox_root is not None:
        from .inbox import read
        shared = exchange.transport_factory(inbox_root)
        if any(item['Path'] == 'inbox' and item['IsDir'] for item in shared.list('')):
            for entry in shared.list('inbox'):
                if entry['IsDir']:
                    continue
                event = read(shared, entry['Path'])
                if event['type'] == 'ci_requested' and event['repository_id'] == repository_id:
                    pending[event['job_id']] = event
                    inbox_state.append(('inbox/' + entry['Path'], digest(encode(event))))
    if not any(item["Path"] == "ci" for item in transport.list("")):
        if pending:
            raise GdiError('GC blocked by unregistered CI submissions in inbox')
        return ()
    listing = transport.list("ci", recursive=True)
    queue = [item for item in listing if item["Path"].startswith("queue/") and not item["IsDir"]]
    if queue:
        raise GdiError("GC blocked by CI queue entries; finish/publish every job and stop the worker first")
    immutable = list(inbox_state)
    jobs = set(pending)
    for item in listing:
        path = item["Path"]
        if path.startswith("jobs/"):
            parts = path.split("/")
            if len(parts) < 2 or not re.fullmatch(r"[0-9a-f]{32}", parts[1]):
                raise GdiError("GC found an unknown CI job path")
            jobs.add(parts[1])
            if not item["IsDir"] and not path.endswith("/status.json"):
                immutable.append((path, item.get("Size")))
        elif path.split("/", 1)[0] not in ("queue", "workers", "jobs"):
            raise GdiError("GC found an unknown CI namespace")
    for jid in sorted(jobs):
        prefix = f"ci/jobs/{jid}"
        names = {item["Path"][len("jobs/" + jid + "/"):] for item in listing
                 if not item["IsDir"] and item["Path"].startswith("jobs/" + jid + "/")}
        if 'submission.json' in names:
            from .inbox import submission
            event = decode(transport.read(prefix + '/submission.json'))
            req, raw = submission(event, exchange.git, repository_id)
            if req['job_id'] != jid:
                raise GdiError('GC found an invalid CI submission/job identity')
        elif {'request.json', 'request.ready'}.issubset(names):
            raw = transport.read(prefix + '/request.json')
            req = request(decode(raw), exchange.git, repository_id)
            if req['job_id'] != jid or decode(transport.read(prefix + '/request.ready')) != marker(req, raw):
                raise GdiError('GC found an invalid CI request/ready')
        else:
            raise GdiError('GC blocked by incomplete/nonterminal CI job: ' + jid)
        cancelled = (validate_cancellation(transport.read(prefix + '/cancel.json'), req, raw)
                     if 'cancel.json' in names else None)
        from .ci_package import PACKAGE_PATTERN
        if any(PACKAGE_PATTERN.fullmatch(name.removeprefix('cancelled/')) for name in names):
            from .ci import CiResultReader
            with tempfile.TemporaryDirectory(prefix='gdi-gc-ci-') as directory:
                reader = CiResultReader(exchange.git, transport, repository_id, directory)
                result = reader.result(jid)
            if result is None:
                reason = 'unconfirmed CI cancellation' if cancelled is not None else 'incomplete/nonterminal CI job'
                raise GdiError('GC blocked by ' + reason + ': ' + jid)
            immutable.append((jid, digest(raw), digest(encode(result))))
            continue
        result_directory = 'cancelled/' if cancelled is not None and 'cancelled/result.json' in names else ''
        if result_directory + 'result.json' not in names:
            raise GdiError("GC blocked by incomplete/nonterminal CI job: " + jid)
        result_raw = transport.read(prefix + '/' + result_directory + 'result.json')
        result = validate_result(decode(result_raw), req, digest(raw))
        if (result_directory and result['state'] != 'CANCELLED'):
            raise GdiError('GC found an invalid cancelled CI result: ' + jid)
        if (cancelled is not None and cancelled.get('worker_cancellation_supported') is not False
                and result['state'] != 'CANCELLED'):
            raise GdiError('GC blocked by unconfirmed CI cancellation: ' + jid)
        if any(result_directory + spec["path"] not in names for spec in result["artifacts"]):
            raise GdiError("GC blocked by missing CI result artifacts: " + jid)
        immutable.append((jid, digest(raw), digest(result_raw)))
    return tuple(sorted(immutable))


def collect(exchange, name, *, apply=False, keep_checkpoints=2, quiescent=False, report=print):
    if type(keep_checkpoints) is not int or keep_checkpoints < 1:
        raise GdiError("GC keep-checkpoints must be a positive integer")
    if apply and not quiescent:
        raise GdiError("GC --apply requires --quiescent: pause push/fetch/pull/gc on ALL other clients first; "
                       "this confirmation is not a distributed lock")
    transport, repository_id = exchange.connect(name)
    state = snapshot(exchange, transport, repository_id)
    plan = plan_collection(state, repository_id, keep_checkpoints)
    report(f"GC plan: {name}; Repository ID: {repository_id}; keep-checkpoints={keep_checkpoints}")
    for branch in plan.branches:
        report(f"Branch {branch.ref}: {branch.head}; Publication: {branch.publication}")
        for publication, head in branch.checkpoints:
            report(f"  Keep checkpoint: {publication} ({head})")
    report(f"Retain {plan.retained_bundles} referenced bundles and {len(plan.orphans)} orphan bundles")
    for bundle in plan.candidates:
        report(f"  Candidate: {bundle.path}; {bundle.size} bytes")
    report(f"Candidates: {len(plan.candidates)} bundles; {plan.reclaim_bytes} bytes")
    if not apply:
        report("Dry run: no changes; restoration verification runs with --apply --quiescent")
        return plan
    ci_state = ci_guard(exchange, transport, repository_id, inbox_root=exchange.remote(name).get('inbox_root'))
    if not plan.candidates:
        report("Nothing to delete")
        return plan
    verify_retained(exchange, transport, repository_id, state, plan, report)
    # Recheck the identity and complete snapshot after potentially lengthy downloads.
    latest_transport, latest_id = exchange.connect(name)
    if latest_id != repository_id or snapshot(exchange, latest_transport, latest_id) != state:
        raise GdiError("remote changed during GC verification; no bundles deleted; pause all clients and retry")
    if ci_guard(exchange, latest_transport, latest_id, inbox_root=exchange.remote(name).get('inbox_root')) != ci_state:
        raise GdiError("CI changed during GC verification; no bundles deleted")
    deleted_bytes = 0
    for bundle in plan.candidates:
        report(f"Deleting {bundle.path}; {bundle.size} bytes...")
        try:
            latest_transport.delete_bundle(bundle.path)
        except GdiError as exc:
            raise GdiError(f"GC could not confirm deletion of {bundle.path}; remote may be partially cleaned; "
                           f"keep clients paused and retry GC: {exc}") from exc
        deleted_bytes += bundle.size
        report(f"Deleted {bundle.path}")
    expected_bundles = dict(state.bundles)
    for bundle in plan.candidates:
        del expected_bundles[bundle.path[len("bundles/"):]]
    expected = Snapshot(state.metadata, state.directories, state.chains, expected_bundles, state.lifecycle)
    final_transport, final_id = exchange.connect(name)
    if final_id != repository_id or snapshot(exchange, final_transport, final_id) != expected:
        raise GdiError("remote changed or deletion was not reflected after GC; inspect state before resuming clients")
    if ci_guard(exchange, final_transport, final_id, inbox_root=exchange.remote(name).get('inbox_root')) != ci_state:
        raise GdiError("CI changed during GC; inspect state before resuming clients")
    report(f"GC complete: deleted {len(plan.candidates)} bundles; {deleted_bytes} bytes")
    return plan
