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
from .exchange import decode, digest
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


def snapshot(exchange, transport, repository_id):
    """Read every branch from one listing, rejecting unknown metadata/bundle paths."""
    listing = transport.list("branches", recursive=True)
    metadata = {}
    directories = []
    refs = set()
    for item in listing:
        path = item["Path"]
        if item["IsDir"]:
            exchange.git.ref(directory_ref(path)[len("refs/heads/"):])
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
        refs.add(ref)
        metadata[path] = raw
    # Reuse the reader's strict version, identity, checksum, base and chain checks.
    chains = {ref: exchange.publications(transport, repository_id, ref,
                                         listing=listing, metadata=metadata)
              for ref in sorted(refs)}
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
    return Snapshot(metadata, tuple(sorted(directories)), chains, bundles)


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


def ci_guard(exchange, transport, repository_id):
    """Fail closed on incomplete jobs; all CI writers must also be paused."""
    from .ci_protocol import marker, request, validate_result
    if not any(item["Path"] == "ci" for item in transport.list("")):
        return ()
    listing = transport.list("ci", recursive=True)
    queue = [item for item in listing if item["Path"].startswith("queue/") and not item["IsDir"]]
    if queue:
        raise GdiError("GC blocked by CI queue entries; finish/publish every job and stop the worker first")
    immutable = []
    jobs = set()
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
        if not {"request.json", "request.ready", "result.json"}.issubset(names):
            raise GdiError("GC blocked by incomplete/nonterminal CI job: " + jid)
        raw = transport.read(prefix + "/request.json")
        req = request(decode(raw), exchange.git, repository_id)
        if req["job_id"] != jid or decode(transport.read(prefix + "/request.ready")) != marker(req, raw):
            raise GdiError("GC found an invalid CI request/ready")
        result_raw = transport.read(prefix + "/result.json")
        result = validate_result(decode(result_raw), req, digest(raw))
        if any(spec["path"] not in names for spec in result["artifacts"]):
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
    ci_state = ci_guard(exchange, transport, repository_id)
    if not plan.candidates:
        report("Nothing to delete")
        return plan
    verify_retained(exchange, transport, repository_id, state, plan, report)
    # Recheck the identity and complete snapshot after potentially lengthy downloads.
    latest_transport, latest_id = exchange.connect(name)
    if latest_id != repository_id or snapshot(exchange, latest_transport, latest_id) != state:
        raise GdiError("remote changed during GC verification; no bundles deleted; pause all clients and retry")
    if ci_guard(exchange, latest_transport, latest_id) != ci_state:
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
    expected = Snapshot(state.metadata, state.directories, state.chains, expected_bundles)
    final_transport, final_id = exchange.connect(name)
    if final_id != repository_id or snapshot(exchange, final_transport, final_id) != expected:
        raise GdiError("remote changed or deletion was not reflected after GC; inspect state before resuming clients")
    if ci_guard(exchange, final_transport, final_id) != ci_state:
        raise GdiError("CI changed during GC; inspect state before resuming clients")
    report(f"GC complete: deleted {len(plan.candidates)} bundles; {deleted_bytes} bytes")
    return plan
