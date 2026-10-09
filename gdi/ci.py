"""Agent-side durable submission, live console and verified results."""

import hashlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import uuid

from .ci_protocol import (TERMINAL, atomic_write, job_id, marker, now, request, upload_json,
                          validate_result, verify_file, workflow_selection)
from .exchange import decode, digest, encode, file_digest, hex_value
from .git import GdiError


def publication(exchange, transport, repository_id, publication_id):
    if not hex_value(publication_id, 64):
        raise GdiError("publication ID must be 64 lowercase hexadecimal characters")
    listing = transport.list("updates", recursive=True)
    paths = [entry["Path"] for entry in listing if not entry["IsDir"] and entry["Path"].endswith('/' + publication_id + '.json')]
    if len(paths) != 1:
        raise GdiError("CI publication not found or ambiguous")
    data = decode(transport.read("updates/" + paths[0]))
    if not isinstance(data.get("ref"), str) or not data["ref"].startswith("refs/heads/"):
        raise GdiError("invalid CI publication branch ref")
    chain = exchange.publications(transport, repository_id, data["ref"], listing=listing)
    if publication_id not in dict(chain):
        raise GdiError("CI publication is not on the verified metadata chain")
    return dict(chain)[publication_id], chain


def files(transport, path):
    return {item["Path"] for item in transport.list(path) if not item["IsDir"]}


def optional_read(transport, path):
    if hasattr(transport, "read_optional"):
        return transport.read_optional(path)
    # Custom transports can establish absence by listing; read failures propagate.
    parent = ""
    for part in path.split("/"):
        entries = {item["Path"]: item for item in transport.list(parent)}
        if part not in entries:
            return None
        parent += ("/" if parent else "") + part
    return transport.read(path)


def migrate_state(old, root):
    """Resume copying immutable legacy files; publish the completion marker last."""
    if root.is_symlink():
        raise GdiError("CI state must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    with (root.parent / ".migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        complete = root / ".legacy-migrated"
        if complete.exists() or not old.exists():
            return
        if old.is_symlink():
            raise GdiError("legacy CI state must not be a symlink")
        for source in sorted(old.rglob("*")):
            target = root / source.relative_to(old)
            if source.is_symlink() or target.is_symlink():
                raise GdiError("CI migration refuses symlinks")
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not source.is_file():
                raise GdiError("legacy CI state contains a non-regular file")
            if target.exists():
                if not target.is_file() or file_digest(source) != file_digest(target):
                    raise GdiError("conflicting legacy CI state; preserve both copies: " + str(target))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                pending = Path(output.name)
                try:
                    with source.open("rb") as handle:
                        shutil.copyfileobj(handle, output)
                    output.flush()
                    os.fsync(output.fileno())
                    os.replace(pending, target)
                finally:
                    pending.unlink(missing_ok=True)
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True) + [root, root.parent]:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        atomic_write(complete, b"1\n")


class CiClient:
    def __init__(self, exchange, remote):
        self.exchange = exchange
        self.git = exchange.git
        self.transport, self.repository_id = exchange.connect(remote)
        self.settings = exchange.remote(remote)
        self.root = self.git.gdi_dir() / "ci" / self.repository_id
        old = self.git.common_dir() / "gdi-ci" / self.repository_id
        migrate_state(old, self.root)

    def execution_settings(self, worker_id, profile_id="full", workflow=None):
        from .ci_protocol import identifier
        identifier(worker_id); identifier(profile_id)
        selector = workflow_selection(workflow)
        shared = None
        capabilities = None
        if self.settings["inbox_root"] is not None:
            candidate = self.exchange.transport_factory(self.settings["inbox_root"])
            raw_capabilities = optional_read(candidate, f"ci/workers/{worker_id}/capabilities.json")
            if raw_capabilities is not None:
                capabilities = decode(raw_capabilities)
                if capabilities.get("inbox_version") != 1:
                    raise GdiError("worker does not support the shared inbox")
                shared = candidate
        if capabilities is None:
            capabilities = decode(self.transport.read(f"ci/workers/{worker_id}/capabilities.json"))
        try:
            revision = (capabilities["profiles"][profile_id] if shared is not None else
                        capabilities["repositories"][self.repository_id][profile_id])
        except (KeyError, TypeError) as exc:
            raise GdiError("worker does not advertise the requested CI execution settings; start/check the worker") from exc
        if capabilities.get("ci_version") != 1 or capabilities.get("worker_id") != worker_id or not hex_value(revision, 64):
            raise GdiError("invalid worker capabilities")
        if shared is None and selector != workflow_selection():
            raise GdiError("workflow selection requires a global inbox worker")
        return revision, shared, selector

    def submit(self, publication_id, worker_id, profile_id="full", *, retry_of=None, workflow=None, github_repository=None):
        revision, shared, selector = self.execution_settings(worker_id, profile_id, workflow)
        pub, _ = publication(self.exchange, self.transport, self.repository_id, publication_id)
        namespace = self.git.github_repository() if github_repository is None else github_repository
        key_fields = [publication_id, worker_id, profile_id, revision, retry_of]
        if shared is not None:
            key_fields.append(selector)
            if namespace:
                key_fields.append(namespace)
        key = digest(encode(key_fields))
        outbox = self.root / "outbox" / (key + ".json")
        if outbox.exists():
            raw = outbox.read_bytes()
            req = request(decode(raw), self.git, self.repository_id)
        else:
            req = {"ci_version": 1, "job_id": uuid.uuid4().hex, "repository_id": self.repository_id,
                   "ref": pub["ref"], "head": pub["head"], "publication_id": publication_id,
                   "worker_id": worker_id, "profile_id": profile_id, "profile_revision": revision,
                   "created_at": now(), "retry_of": retry_of}
            if shared is not None:
                req.update(ci_version=2, workflow=selector)
                if namespace:
                    req["github_repository"] = namespace
            request(req, self.git, self.repository_id)
            raw = encode(req)
            atomic_write(outbox, raw)
        jid = req["job_id"]
        prefix = f"ci/jobs/{jid}"
        for directory in ((prefix,) if shared is not None else ("ci/queue", prefix)):
            self.transport.mkdir(directory)
        # A repeated successful submit must not put a completed job back in the queue.
        existing = files(self.transport, prefix)
        if "result.json" in existing:
            self.result(jid)
            return req
        with tempfile.TemporaryDirectory(prefix="gdi-submit-") as temporary:
            source = Path(temporary) / "request.json"
            source.write_bytes(raw)
            self.transport.upload(source, prefix + "/request.json")
            ready = marker(req, raw)
            if shared is None:
                upload_json(self.transport, f"ci/queue/{jid}.json", ready)
            upload_json(self.transport, prefix + "/request.ready", ready)
            if shared is not None:
                from .inbox import notification, publish
                from .local_config import repository_path
                event = notification(self.repository_id, repository_path(self.settings["inbox_root"], self.settings["url"]),
                                     pub, publication_id, req, raw)
                publish(shared, event)
        return req

    def load_request(self, jid):
        job_id(jid)
        prefix = f"ci/jobs/{jid}"
        raw = self.transport.read(prefix + "/request.json")
        req = request(decode(raw), self.git, self.repository_id)
        if req["job_id"] != jid or decode(self.transport.read(prefix + "/request.ready")) != marker(req, raw):
            raise GdiError("CI ready/request checksum or job identity mismatch")
        return req, raw

    def result(self, jid, *, listing=None):
        req, raw = self.load_request(jid)
        prefix = f"ci/jobs/{jid}"
        listing = files(self.transport, prefix) if listing is None else listing
        if "result.json" not in listing:
            return None
        result = validate_result(decode(self.transport.read(prefix + "/result.json")), req, digest(raw))
        local = self.root / "results" / jid
        local.mkdir(parents=True, exist_ok=True)
        for desc in result["artifacts"]:
            target = local / desc["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                verify_file(target, desc)
            except (OSError, GdiError):
                pending = target.with_name(target.name + ".pending")
                self.transport.download(prefix + '/' + desc["path"], pending)
                verify_file(pending, desc)
                os.replace(pending, target)
        final_status = decode((local / "final-status.json").read_bytes())
        if (final_status.get("job_id") != jid or final_status.get("run_id") != result["run_id"]
                or final_status.get("state") != result["state"] or final_status.get("request_sha256") != digest(raw)):
            raise GdiError("CI final status does not match result")
        # Validate the immutable streaming sequence against the final full binary log.
        chunk_hash = hashlib.sha256()
        size = 0
        for path, checksum in self.chunks(jid):
            data = self.chunk(jid, path, checksum)
            chunk_hash.update(data); size += len(data)
        log = next(item for item in result["artifacts"] if item["path"] == "build.log")
        if size != log["bytes"] or chunk_hash.hexdigest() != log["sha256"]:
            raise GdiError("CI log chunks are incomplete or differ from the final log")
        atomic_write(local / "result.json", encode(result))
        return result

    def status(self, jid):
        result = self.result(jid)
        if result is not None:
            return {**result, "verified": True}
        req, raw = self.load_request(jid)
        listing = files(self.transport, f"ci/jobs/{jid}")
        if "status.json" not in listing:
            return {**req, "state": "QUEUED", "verified": False}
        value = decode(self.transport.read(f"ci/jobs/{jid}/status.json"))
        if value.get("job_id") != jid or value.get("request_sha256") != digest(raw):
            raise GdiError("CI status/request identity mismatch")
        return {**value, "verified": False}

    def chunks(self, jid):
        prefix = f"ci/jobs/{job_id(jid)}"
        if "log-chunks" not in {item["Path"] for item in self.transport.list(prefix) if item["IsDir"]}:
            return []
        entries = self.transport.list(prefix + "/log-chunks")
        paths = []
        for entry in entries:
            match = re.fullmatch(r"([0-9]{8})-([0-9a-f]{64})\.bin", entry["Path"])
            if entry["IsDir"] or not match:
                raise GdiError("invalid CI log chunk path")
            paths.append((int(match[1]), entry["Path"], match[2]))
        paths.sort()
        if [item[0] for item in paths] != list(range(1, len(paths) + 1)):
            raise GdiError("CI log chunk sequence contains gaps or duplicates")
        return [(path, checksum) for _, path, checksum in paths]

    def chunk(self, jid, name, checksum):
        target = self.root / "chunks" / jid / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or digest(target.read_bytes()) != checksum:
            pending = target.with_suffix(".pending")
            self.transport.download(f"ci/jobs/{jid}/log-chunks/{name}", pending)
            if digest(pending.read_bytes()) != checksum:
                raise GdiError("CI log chunk checksum mismatch")
            os.replace(pending, target)
        return target.read_bytes()

    def follow(self, jid, cursor, stream):
        chunks = self.chunks(jid)
        if len(chunks) < cursor:
            raise GdiError("CI log sequence shrank")
        for name, checksum in chunks[cursor:]:
            data = self.chunk(jid, name, checksum)
            if hasattr(stream, "buffer"):
                stream.buffer.write(data); stream.buffer.flush()
            else:
                stream.write(data.decode("utf-8", errors="replace")); stream.flush()
        return len(chunks)

    def wait(self, jid, *, timeout=3600, follow=False, stream=None, interval=2):
        from .worker_config import seconds
        if timeout is not None:
            seconds(timeout)
        seconds(interval)
        stream = stream or sys.stderr
        deadline, cursor, last = (time.monotonic() + timeout if timeout is not None else None), 0, None
        while True:
            value = self.status(jid)
            if follow:
                summary = (value.get("state"), value.get("stage"), value.get("updated_at"))
                if summary != last:
                    print(f"[{jid}] {summary[0]} stage={summary[1]} heartbeat={summary[2]}", file=stream, flush=True)
                    last = summary
                cursor = self.follow(jid, cursor, stream)
            if value.get("verified"):
                return value, 0 if value["state"] == "PASS" else 1
            if deadline is not None and time.monotonic() >= deadline:
                return {"job_id": jid, "state": "WAIT_TIMEOUT", "verified": False}, 124
            time.sleep(interval if deadline is None else min(interval, max(0, deadline - time.monotonic())))

    def logs(self, jid, *, output=None, follow=False):
        self.load_request(jid)
        if output:
            result, code = self.wait(jid)
            if code == 124:
                raise GdiError("job is still running; use logs --follow or wait again")
            source = self.root / "results" / jid / "build.log"
            import shutil
            shutil.copyfile(source, Path(output))
            return
        if follow:
            self.wait(jid, follow=True, stream=sys.stdout, timeout=None)
        else:
            self.follow(jid, 0, sys.stdout)

    def retry(self, jid):
        req, _ = self.load_request(jid)
        if self.result(jid) is None:
            raise GdiError("cannot retry an active job; wait for a verified terminal result")
        return self.submit(req["publication_id"], req["worker_id"], req["profile_id"], retry_of=jid,
                           workflow=req.get("workflow"), github_repository=req.get("github_repository", ""))

    def pull_passed(self, jid, profile):
        result = self.result(jid)
        if result is None or result["state"] != "PASS" or result["profile_id"] != profile:
            raise GdiError("pull --passed requires a verified PASS for the requested profile")
        snapshot = self.git.snapshot()
        self.git.require_clean(snapshot)
        if self.git.ref(snapshot[0]) != result["ref"]:
            raise GdiError("CI result belongs to another branch")
        _, chain = publication(self.exchange, self.transport, self.repository_id, result["publication_id"])
        cache = self.exchange.restore(self.transport, self.repository_id, chain)
        if not cache.git.has_commit(result["head"]) or not cache.git.ancestor(result["head"], chain[-1][1]["head"]):
            raise GdiError("CI result commit is not reachable from verified history")
        self.git.import_objects(cache.path, result["head"])
        if not self.git.ancestor(snapshot[1], result["head"]):
            raise GdiError("CI result is not a fast-forward of the current branch")
        if self.git.snapshot() != snapshot:
            raise GdiError("worktree changed while verifying CI result")
        self.git.require_clean(snapshot)
        self.git.call("merge", "--ff-only", "--", result["head"])
        if self.git.oid("HEAD") != result["head"]:
            raise GdiError("HEAD changed while applying CI result")
        return result
