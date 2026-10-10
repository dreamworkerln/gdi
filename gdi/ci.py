"""Agent-side durable submission, live console and verified results."""

from contextlib import contextmanager, nullcontext
import hashlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from .ci_protocol import (atomic_write, job_id, marker, request, upload_json,
                          validate_result, verify_file, workflow_selection, capability_revision, prepare_request,
                          cancellation, validate_cancellation, cancellation_capability)
from .exchange import decode, digest, encode, file_digest, hex_value
from .git import GdiError


def publication(exchange, transport, repository_id, publication_id):
    if not hex_value(publication_id, 64):
        raise GdiError("publication ID must be 64 lowercase hexadecimal characters")
    listing = transport.list("branches", recursive=True)
    paths = [entry["Path"] for entry in listing if not entry["IsDir"] and entry["Path"].endswith('/' + publication_id + '.json')]
    if len(paths) != 1:
        raise GdiError("CI publication not found or ambiguous")
    data = decode(transport.read("branches/" + paths[0]))
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


class CiResultReader:
    """Shared result verification for live transports and offline connector snapshots."""

    def __init__(self, git, transport, repository_id, root):
        self.git, self.transport, self.repository_id = git, transport, repository_id
        self.root = Path(root)

    def load_request(self, jid):
        job_id(jid)
        prefix = f"ci/jobs/{jid}"
        raw = self.transport.read(prefix + "/request.json")
        req = request(decode(raw), self.git, self.repository_id)
        if req["job_id"] != jid or decode(self.transport.read(prefix + "/request.ready")) != marker(req, raw):
            raise GdiError("CI ready/request checksum or job identity mismatch")
        return req, raw

    def result_source(self, req, raw, *, listing=None):
        """Cancellation fences ordinary results, including an upload already in flight."""
        prefix = f"ci/jobs/{req['job_id']}"
        listing = files(self.transport, prefix) if listing is None else listing
        cancelled = self.cancellation_record(req, raw, listing=listing)
        if cancelled is not None:
            directories = {item['Path'] for item in self.transport.list(prefix) if item['IsDir']}
            content = (optional_read(self.transport, prefix + '/cancelled/result.json')
                       if 'cancelled' in directories else None)
            if content is not None:
                result = validate_result(decode(content), req, digest(raw))
                if result['state'] != 'CANCELLED':
                    raise GdiError('CI cancelled delivery must have state CANCELLED')
                claim = optional_read(self.transport, prefix + '/worker.running.json')
                if claim is not None and decode(claim) != {
                        'ci_version': 1, 'job_id': req['job_id'], 'run_id': result['run_id'],
                        'worker_id': req['worker_id'], 'request_sha256': digest(raw)}:
                    raise GdiError('CI cancelled delivery does not match the execution claim')
                return prefix + '/cancelled', result
        if "result.json" not in listing:
            return None
        result = validate_result(decode(self.transport.read(prefix + "/result.json")), req, digest(raw))
        if (cancelled is not None and cancelled.get('worker_cancellation_supported') is not False
                and result['state'] != 'CANCELLED'):
            return None
        return prefix, result

    def result(self, jid, *, listing=None):
        req, raw = self.load_request(jid)
        source = self.result_source(req, raw, listing=listing)
        if source is None:
            return None
        prefix, result = source
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
        cancelled_delivery = prefix.endswith('/cancelled')
        for path, checksum in self.chunks(jid, cancelled_delivery=cancelled_delivery):
            data = self.chunk(jid, path, checksum, cancelled_delivery=cancelled_delivery)
            chunk_hash.update(data); size += len(data)
        log = next(item for item in result["artifacts"] if item["path"] == "build.log")
        if size != log["bytes"] or chunk_hash.hexdigest() != log["sha256"]:
            raise GdiError("CI log chunks are incomplete or differ from the final log")
        # Refresh after downloading: a cancellation can arrive while verifying artifacts.
        current = self.result_source(req, raw)
        if current is None:
            return None
        if current[0] != prefix:
            return self.result(jid)
        if current[1] != result:
            raise GdiError('immutable CI result changed during verification')
        atomic_write(local / "result.json", encode(result))
        return result

    def cancellation_record(self, req, raw, *, listing=None):
        path = f"ci/jobs/{req['job_id']}/cancel.json"
        content = (optional_read(self.transport, path) if listing is None else
                   self.transport.read(path) if 'cancel.json' in listing else None)
        saved = self.root / 'verified-cancellations' / (req['job_id'] + '.json')
        if saved.exists():
            previous = saved.read_bytes()
            value = validate_cancellation(previous, req, raw)
            if content is not None and content != previous:
                raise GdiError('immutable CI cancellation changed after verification')
            # An observed immutable cancellation cannot be revoked by deletion.
            return value
        if content is None:
            return None
        value = validate_cancellation(content, req, raw)
        atomic_write(saved, content)
        return value

    def cancellation_requested(self, jid):
        req, raw = self.load_request(jid)
        return self.cancellation_record(req, raw)

    def retry_request(self, jid):
        req, _ = self.load_request(jid)
        if self.result(jid) is None:
            cancelled = self.cancellation_requested(jid)
            if cancelled is None:
                raise GdiError('cannot retry an active job; verify its terminal result or durable cancellation first')
            if cancelled.get('worker_cancellation_supported') is False:
                raise GdiError('legacy queue withdrawal does not confirm execution cancellation; wait for a verified terminal result')
        return req

    def chunks(self, jid, *, cancelled_delivery=False):
        prefix = f"ci/jobs/{job_id(jid)}"
        if cancelled_delivery:
            prefix += '/cancelled'
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

    def chunk(self, jid, name, checksum, *, cancelled_delivery=False):
        relative = jid + ('/cancelled' if cancelled_delivery else '')
        target = self.root / "chunks" / relative / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or digest(target.read_bytes()) != checksum:
            pending = target.with_suffix(".pending")
            self.transport.download(f"ci/jobs/{relative}/log-chunks/{name}", pending)
            if digest(pending.read_bytes()) != checksum:
                raise GdiError("CI log chunk checksum mismatch")
            os.replace(pending, target)
        return target.read_bytes()


class CiClient(CiResultReader):
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
        revision = capability_revision(capabilities, worker_id, profile_id,
                                       None if shared is not None else self.repository_id)
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
            req = prepare_request(self.git, self.repository_id, publication_id, pub, worker_id,
                                  profile_id, revision, shared=shared is not None,
                                  workflow=selector, github_repository=namespace, retry_of=retry_of)
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
        if 'cancel.json' in existing:
            self.cancellation_requested(jid)
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



    def status(self, jid):
        result = self.result(jid)
        if result is not None:
            return {**result, "verified": True}
        req, raw = self.load_request(jid)
        cancelled = self.cancellation_requested(jid)
        if cancelled is not None:
            return {**req, 'state': 'CANCEL_REQUESTED', 'verified': False,
                    'worker_cancellation_supported': cancelled.get('worker_cancellation_supported', True)}
        listing = files(self.transport, f"ci/jobs/{jid}")
        if "status.json" not in listing:
            return {**req, "state": "QUEUED", "verified": False}
        value = decode(self.transport.read(f"ci/jobs/{jid}/status.json"))
        if value.get("job_id") != jid or value.get("request_sha256") != digest(raw):
            raise GdiError("CI status/request identity mismatch")
        return {**value, "verified": False}



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

    @contextmanager
    def cursor_session(self, jid, *, restart=False):
        """One follower per job; only flushed, verified chunks advance the cursor."""
        req, raw = self.load_request(jid)
        directory = self.root / 'follow'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (jid + '.json')
        lock = directory / (jid + '.lock')
        if path.is_symlink() or lock.is_symlink():
            raise GdiError('CI follow cursor must not be a symlink')
        with lock.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GdiError('another command is following this CI job') from exc
            state = {'cursor_version': 1, 'job_id': jid, 'request_sha256': digest(raw), 'chunks': []}
            if path.exists() and not restart:
                saved = decode(path.read_bytes())
                if (set(saved) != set(state) or type(saved.get('cursor_version')) is not int or
                        saved['cursor_version'] != 1 or saved['job_id'] != jid or
                        saved['request_sha256'] != digest(raw) or not isinstance(saved['chunks'], list) or
                        any(not isinstance(name, str) or not re.fullmatch(r'[0-9]{8}-[0-9a-f]{64}\.bin', name)
                            for name in saved['chunks'])):
                    raise GdiError('invalid CI follow cursor; use --restart to replay the log')
                state = saved
            atomic_write(path, encode(state))
            yield path, state

    def follow_saved(self, jid, cursor, stream):
        path, state = cursor
        chunks = self.chunks(jid)
        names = [name for name, _ in chunks]
        consumed = state['chunks']
        if names[:len(consumed)] != consumed:
            raise GdiError('CI log sequence shrank or previously printed chunks changed')
        for name, checksum in chunks[len(consumed):]:
            data = self.chunk(jid, name, checksum)
            if hasattr(stream, 'buffer'):
                stream.buffer.write(data); stream.buffer.flush()
            else:
                stream.write(data.decode('utf-8', errors='replace')); stream.flush()
            # Terminal writes and fsync cannot be atomic. A crash here can replay
            # the current chunk, but never silently skip unflushed output.
            consumed.append(name)
            atomic_write(path, encode(state))

    def wait(self, jid, *, timeout=3600, follow=False, stream=None, interval=2, restart=False):
        from .worker_config import seconds
        if timeout is not None:
            seconds(timeout)
        seconds(interval)
        if restart and not follow:
            raise GdiError('--restart requires --follow')
        stream = stream or sys.stderr
        deadline, last = (time.monotonic() + timeout if timeout is not None else None), None
        with self.cursor_session(jid, restart=restart) if follow else nullcontext(None) as cursor:
            while True:
                value = self.status(jid)
                if follow:
                    summary = (value.get('state'), value.get('stage'), value.get('updated_at'))
                    if summary != last:
                        print(f"[{jid}] {summary[0]} stage={summary[1]} heartbeat={summary[2]}", file=stream, flush=True)
                        last = summary
                    self.follow_saved(jid, cursor, stream)
                if value.get('verified'):
                    return value, 0 if value['state'] == 'PASS' else 1
                if deadline is not None and time.monotonic() >= deadline:
                    return {'job_id': jid, 'state': 'WAIT_TIMEOUT', 'verified': False}, 124
                time.sleep(interval if deadline is None else min(interval, max(0, deadline - time.monotonic())))

    def logs(self, jid, *, output=None, follow=False, restart=False):
        self.load_request(jid)
        if restart and (not follow or output):
            raise GdiError('--restart requires --follow')
        if output:
            result, code = self.wait(jid)
            if code == 124:
                raise GdiError("job is still running; use logs --follow or wait again")
            source = self.root / "results" / jid / "build.log"
            import shutil
            shutil.copyfile(source, Path(output))
            return
        if follow:
            self.wait(jid, follow=True, stream=sys.stdout, timeout=None, restart=restart)
        else:
            self.follow(jid, 0, sys.stdout)

    def withdraw(self, req, raw):
        """Remove only this request's queue notification after persisting cancellation."""
        if req['ci_version'] == 2:
            from .inbox import filename, notification, read
            from .local_config import repository_path
            root = self.exchange.transport_factory(self.settings['inbox_root'])
            event = notification(self.repository_id,
                                 repository_path(self.settings['inbox_root'], self.settings['url']),
                                 req, req['publication_id'], req, raw)
            name = filename(event)
            if name in files(root, 'inbox'):
                if read(root, name) != event:
                    raise GdiError('CI withdrawal inbox/request identity mismatch')
                root.delete_notification('inbox/' + name)
        else:
            path = f"ci/queue/{req['job_id']}.json"
            content = optional_read(self.transport, path)
            if content is not None:
                if decode(content) != marker(req, raw):
                    raise GdiError('CI withdrawal queue/request identity mismatch')
                self.transport.delete_queue(path)

    def cancel(self, jid, *, withdraw=False):
        completed = self.result(jid)
        if completed is not None:
            return {**completed, 'verified': True}
        req, raw = self.load_request(jid)
        target = (self.exchange.transport_factory(self.settings['inbox_root'])
                  if req['ci_version'] == 2 else self.transport)
        content = optional_read(target, f"ci/workers/{req['worker_id']}/capabilities.json")
        caps = decode(content) if content is not None else None
        supported = True
        try:
            cancellation_capability(caps, req['worker_id'])
        except GdiError:
            supported = False
            if not withdraw:
                raise
        existing = self.cancellation_requested(jid)
        content = encode(existing if existing is not None else cancellation(req, raw, supported=supported))
        path = self.root / 'cancellations' / (jid + '.json')
        atomic_write(path, content)
        self.transport.upload(path, f'ci/jobs/{jid}/cancel.json')
        validate_cancellation(self.transport.read(f'ci/jobs/{jid}/cancel.json'), req, raw)
        if withdraw:
            self.withdraw(req, raw)
        value = self.status(jid)
        if withdraw:
            value.update(queue_withdrawn=True,
                         worker_cancellation_supported=decode(content).get('worker_cancellation_supported', True))
        return value

    def retry(self, jid, *, worker_id=None):
        req = self.retry_request(jid)
        return self.submit(req["publication_id"], worker_id or req["worker_id"], req["profile_id"], retry_of=jid,
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
