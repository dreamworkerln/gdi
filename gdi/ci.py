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
                          cancellation, validate_cancellation, cancellation_capability, submission_capability)
from .exchange import decode, digest, encode, file_digest, hex_value
from .git import GdiError
from .ci_package import PACKAGE_PATTERN, package_checksum, read_package
from .diagnostics import phase


def publication(exchange, transport, repository_id, publication_id):
    if not hex_value(publication_id, 64):
        raise GdiError("publication ID must be 64 lowercase hexadecimal characters")
    listing = transport.list("branches", recursive=True)
    paths = [entry["Path"] for entry in listing if not entry["IsDir"] and entry["Path"].endswith('/' + publication_id + '.json')]
    if not paths:
        from .work_lifecycle import records, archived_chain
        found = []
        for value in records(transport, repository_id, 'retired').values():
            if publication_id in value['manifests'] and value['archive']:
                chain = archived_chain(value)
                found.append((dict(chain)[publication_id], chain))
        if len(found) == 1:
            return found[0]
    if len(paths) != 1:
        raise GdiError("CI publication not found or ambiguous")
    data = decode(transport.read("branches/" + paths[0]))
    if not isinstance(data.get("ref"), str) or not data["ref"].startswith("refs/heads/"):
        raise GdiError("invalid CI publication branch ref")
    chain = exchange.publications(transport, repository_id, data["ref"], listing=listing)
    if publication_id not in dict(chain):
        raise GdiError("CI publication is not on the verified metadata chain")
    return dict(chain)[publication_id], chain


def files(transport, path, *, optional=False):
    if optional:
        parent = ''
        for part in path.split('/'):
            entries = {item['Path']: item for item in transport.list(parent)}
            if part not in entries:
                return set()
            if not entries[part]['IsDir']:
                raise GdiError('CI folder path is not a directory: ' + path)
            parent += ('/' if parent else '') + part
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

    def __init__(self, git, transport, repository_id, root, *, submission_root=None):
        self.git, self.transport, self.repository_id = git, transport, repository_id
        self.root = Path(root)
        self.package_sources = {}
        self.submission_root = Path(submission_root) if submission_root is not None else None
        self.repository_path = getattr(transport, 'repository_path', None)
        self.request_inbox = None

    def remember_submission(self, event):
        from .inbox import filename, submission
        req, raw = submission(event, self.git, self.repository_id, self.repository_path)
        if self.submission_root is None:
            return req, raw
        self.submission_root.mkdir(parents=True, exist_ok=True)
        target = self.submission_root / filename(event)
        previous = list(self.submission_root.glob(req['job_id'] + '-*.json'))
        if any(path != target or path.read_bytes() != encode(event) for path in previous):
            raise GdiError('local CI submission conflicts with the saved job identity')
        if not target.exists():
            atomic_write(target, encode(event))
        return req, raw

    def cached_submission(self, jid):
        if self.submission_root is None:
            return None
        from .inbox import filename, submission
        entries = list(self.submission_root.glob(jid + '-*.json'))
        if not entries:
            return None
        if len(entries) != 1:
            raise GdiError('multiple saved CI submissions for one job')
        from .snapshot import metadata_file
        content = metadata_file(entries[0])
        event = decode(content)
        req, raw = submission(event, self.git, self.repository_id, self.repository_path)
        if req['job_id'] != jid or filename(event) != entries[0].name or encode(event) != content:
            raise GdiError('saved CI submission checksum/identity mismatch')
        return event, req, raw

    def load_request(self, jid):
        job_id(jid)
        cached = self.cached_submission(jid)
        if cached is not None:
            return cached[1], cached[2]
        prefix = f"ci/jobs/{jid}"
        content = optional_read(self.transport, prefix + '/submission.json')
        if content is not None:
            event = decode(content)
            if encode(event) != content:
                raise GdiError('CI submission is not canonical')
            req, raw = self.remember_submission(event)
            if req['job_id'] != jid:
                raise GdiError('CI submission/job identity mismatch')
            return req, raw
        if self.request_inbox is not None:
            from .inbox import NAME, read
            names = [item['Path'] for item in self.request_inbox.list('inbox')
                     if not item['IsDir'] and item['Path'].startswith(jid + '-')]
            if len(names) > 1:
                raise GdiError('ambiguous CI submissions for one job')
            if names:
                if not NAME.fullmatch(names[0]):
                    raise GdiError('invalid CI submission filename')
                req, raw = self.remember_submission(read(self.request_inbox, names[0]))
                if req['job_id'] != jid:
                    raise GdiError('CI inbox submission/job identity mismatch')
                return req, raw
        raw = self.transport.read(prefix + "/request.json")
        req = request(decode(raw), self.git, self.repository_id)
        if req["job_id"] != jid or decode(self.transport.read(prefix + "/request.ready")) != marker(req, raw):
            raise GdiError("CI ready/request checksum or job identity mismatch")
        return req, raw

    def live_status(self, req, raw, *, heartbeat_timeout=600, listing=None):
        """Advisory status with shared freshness diagnostics, never terminal proof."""
        from .scheduling import heartbeat
        path = f"ci/jobs/{req['job_id']}/status.json"
        content = (optional_read(self.transport, path) if listing is None else
                   self.transport.read(path) if 'status.json' in listing else None)
        value = decode(content) if content is not None else None
        if value is not None and (value.get('job_id') != req['job_id'] or
                value.get('request_sha256') != digest(raw) or
                value.get('worker_id', req['worker_id']) != req['worker_id']):
            raise GdiError('CI status/request identity mismatch')
        return value, heartbeat(value.get('updated_at') if value is not None else None, heartbeat_timeout)

    def delivery_result(self, prefix, req, raw, *, listing=None):
        """Prefer the single package; retain read-only support for old results."""
        listing = files(self.transport, prefix, optional=True) if listing is None else listing
        candidates = sorted(name for name in listing if name.startswith('result-'))
        sources = self.package_sources
        sources.pop(prefix, None)
        if candidates:
            if len(candidates) != 1 or not PACKAGE_PATTERN.fullmatch(candidates[0]):
                raise GdiError('CI result package is invalid or ambiguous')
            name = candidates[0]
            relative = prefix + '/' + name
            checksum = package_checksum(name)
            if hasattr(self.transport, 'require_file'):
                # An offline observation must include its own verified download,
                # even if a previous snapshot populated the local package cache.
                self.transport.require_file(relative, checksum, self.transport.entries[relative]['bytes'])
            kind = 'cancelled' if prefix.endswith('/cancelled') else 'original'
            target = self.root / 'packages' / req['job_id'] / kind / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise GdiError('CI package cache must not be a symlink')
            if not target.is_file() or file_digest(target) != checksum:
                descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix='.result-', suffix='.pending')
                os.close(descriptor)
                pending = Path(temporary)
                try:
                    self.transport.download(relative, pending)
                    if file_digest(pending) != checksum:
                        raise GdiError('CI result package checksum mismatch')
                    os.replace(pending, target)
                finally:
                    pending.unlink(missing_ok=True)
            result = read_package(target, req, digest(raw), metadata_only=True)
            sources[prefix] = target
            return result
        if 'result.json' in listing:
            return validate_result(decode(self.transport.read(prefix + '/result.json')), req, digest(raw))
        return None

    def result_source(self, req, raw, *, listing=None):
        """Cancellation fences ordinary results, including an upload already in flight."""
        prefix = f"ci/jobs/{req['job_id']}"
        listing = files(self.transport, prefix, optional=True) if listing is None else listing
        cancelled = self.cancellation_record(req, raw, listing=listing)
        if cancelled is not None:
            directories = {item['Path'] for item in self.transport.list(prefix) if item['IsDir']}
            result = (self.delivery_result(prefix + '/cancelled', req, raw)
                      if 'cancelled' in directories else None)
            if result is not None:
                if result['state'] != 'CANCELLED':
                    raise GdiError('CI cancelled delivery must have state CANCELLED')
                claim = optional_read(self.transport, prefix + '/worker.running.json')
                if claim is not None and decode(claim) != {
                        'ci_version': 1, 'job_id': req['job_id'], 'run_id': result['run_id'],
                        'worker_id': req['worker_id'], 'request_sha256': digest(raw)}:
                    raise GdiError('CI cancelled delivery does not match the execution claim')
                return prefix + '/cancelled', result
        result = self.delivery_result(prefix, req, raw, listing=listing)
        if result is None:
            return None
        if (cancelled is not None and cancelled.get('worker_cancellation_supported') is not False
                and result['state'] != 'CANCELLED'):
            return None
        return prefix, result

    def result(self, jid, *, listing=None, _request=None):
        # A caller can reuse the request/ready pair it verified in this step.
        # Revalidate result/cancellation after downloading as before; no mutable
        # status or cross-step request is cached.
        req, raw = self.load_request(jid) if _request is None else _request
        if req['job_id'] != job_id(jid):
            raise GdiError('verified CI request belongs to a different job')
        source = self.result_source(req, raw, listing=listing)
        if source is None:
            return None
        prefix, result = source
        local = self.root / "results" / jid
        local.mkdir(parents=True, exist_ok=True)
        package = self.package_sources.get(prefix)
        if package is not None:
            if read_package(package, req, digest(raw), destination=local) != result:
                raise GdiError('CI package result changed during verification')
        else:
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
        if package is None:
            # Historical results retain their original streaming-log contract.
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
        req, raw = self.load_request(jid)
        if self.result(jid, _request=(req, raw)) is None:
            cancelled = self.cancellation_record(req, raw)
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
        transport, repository_id = exchange.connect(remote)
        super().__init__(exchange.git, transport, repository_id,
                         exchange.git.gdi_dir() / 'ci' / repository_id,
                         submission_root=exchange.git.gdi_dir() / 'ci' / repository_id / 'submissions')
        self.settings = exchange.remote(remote)
        if self.settings['inbox_root'] is not None:
            from .local_config import repository_path
            self.repository_path = repository_path(self.settings['inbox_root'], self.settings['url'])
            self.request_inbox = exchange.transport_factory(self.settings['inbox_root'])
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
            if raw_capabilities is None:
                raise GdiError('shared worker capabilities are missing; update worker')
            capabilities = decode(raw_capabilities)
            submission_capability(capabilities, worker_id)
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

    def submit(self, publication_id, worker_id, profile_id="full", *, retry_of=None, workflow=None, github_repository=None,
               prepared_request=None):
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
            if prepared_request is not None and req != prepared_request:
                raise GdiError('an existing retry outbox conflicts with the automatic successor')
        else:
            req = prepared_request or prepare_request(self.git, self.repository_id, publication_id, pub, worker_id,
                                  profile_id, revision, shared=shared is not None,
                                  workflow=selector, github_repository=namespace, retry_of=retry_of)
            request(req, self.git, self.repository_id)
            if (req['publication_id'] != publication_id or req['worker_id'] != worker_id or
                    req['profile_id'] != profile_id or req['profile_revision'] != revision or req['retry_of'] != retry_of or
                    req['head'] != pub['head'] or req['ref'] != pub['ref'] or
                    req['ci_version'] != (2 if shared is not None else 1) or
                    (shared is not None and (req['workflow'] != selector or req.get('github_repository', '') != namespace))):
                raise GdiError('prepared CI request differs from current execution settings')
            raw = encode(req)
            atomic_write(outbox, raw)
        jid = req["job_id"]
        prefix = f"ci/jobs/{jid}"
        if shared is not None:
            from .inbox import notification, publish, read, filename
            event = notification(self.repository_id, self.repository_path, pub, publication_id, req, raw)
            self.remember_submission(event)
            existing = files(self.transport, prefix, optional=True)
            if 'result.json' in existing or any(PACKAGE_PATTERN.fullmatch(name) for name in existing):
                self.result(jid)
                return req
            if 'cancel.json' in existing:
                self.cancellation_requested(jid)
                return req
            if req['retry_of'] is not None:
                successor = optional_read(self.transport, f"ci/jobs/{req['retry_of']}/successor.json")
                if successor is not None:
                    from .scheduling import validate_successor
                    old, old_raw = self.load_request(req['retry_of'])
                    validate_successor(successor, old, old_raw, req)
            publish(shared, event)
            if read(shared, filename(event)) != event:
                raise GdiError('CI inbox submission changed during read-back')
            return req
        for directory in ("ci/queue", prefix):
            self.transport.mkdir(directory)
        # A repeated successful submit must not put a completed job back in the queue.
        existing = files(self.transport, prefix)
        if "result.json" in existing or any(PACKAGE_PATTERN.fullmatch(name) for name in existing):
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
            upload_json(self.transport, f"ci/queue/{jid}.json", ready)
            upload_json(self.transport, prefix + "/request.ready", ready)
            checked, checked_raw = self.load_request(jid)
            if checked != req or checked_raw != raw:
                raise GdiError('immutable CI request changed before queue notification')
            if req['retry_of'] is not None:
                successor = optional_read(self.transport, f"ci/jobs/{req['retry_of']}/successor.json")
                if successor is not None:
                    from .scheduling import validate_successor
                    old, old_raw = self.load_request(req['retry_of'])
                    validate_successor(successor, old, old_raw, req)
        return req

    def workers(self, profile_id='full', *, settings=None):
        from .scheduling import discover, inspect_workers, policy
        registry = discover(self.exchange.transport_factory(self.settings['inbox_root']))
        return inspect_workers(registry, profile_id, policy(settings))

    def dispatch(self, publication_id, profile_id='full', *, settings=None, workflow=None):
        from .agent import plan_lock
        from .scheduling import choose, policy
        settings = policy(settings)
        selector = workflow_selection(workflow)
        namespace = self.git.github_repository()
        key = digest(encode([publication_id, profile_id, selector, namespace, settings]))
        with plan_lock(self.root / 'dispatch' / key) as directory:
            directory.mkdir(parents=True, exist_ok=True)
            saved = directory / 'request.json'
            if saved.exists():
                prepared = request(decode(saved.read_bytes()), self.git, self.repository_id)
            else:
                state_path = self.root / 'scheduler.json'
                previous = decode(state_path.read_bytes()).get('last_worker') if state_path.exists() else None
                selected = choose(self.workers(profile_id, settings=settings), settings, previous=previous)
                if selected is None:
                    raise GdiError('no fresh compatible worker is available; inspect gdi ci workers')
                revision, shared, selector = self.execution_settings(selected['worker_id'], profile_id, workflow)
                if revision != selected['profile_revision'] or shared is None:
                    raise GdiError('worker revision changed during selection; inspect fresh capabilities')
                pub, _ = publication(self.exchange, self.transport, self.repository_id, publication_id)
                prepared = prepare_request(self.git, self.repository_id, publication_id, pub, selected['worker_id'],
                                           profile_id, revision, workflow=selector, github_repository=namespace)
                # Reuse a preexisting explicit outbox for the same execution.
                key_fields = [publication_id, selected['worker_id'], profile_id, revision, None, selector]
                if namespace:
                    key_fields.append(namespace)
                outbox = self.root / 'outbox' / (digest(encode(key_fields)) + '.json')
                if outbox.exists():
                    prepared = request(decode(outbox.read_bytes()), self.git, self.repository_id)
                atomic_write(saved, encode(prepared))
                atomic_write(state_path, encode({'last_worker': prepared['worker_id']}))
            return self.submit(publication_id, prepared['worker_id'], profile_id, workflow=selector,
                               github_repository=namespace, prepared_request=prepared)

    def supervise(self, jid, settings):
        from .agent import plan_lock
        from .scheduling import Supervisor, discover
        with plan_lock(self.root / 'supervision' / jid) as directory:
            supervisor = Supervisor(self, jid, directory, settings)
            registry = discover(self.exchange.transport_factory(self.settings['inbox_root']))
            decision = supervisor.decide(registry)
            active = decision['job_id']
            if decision['state'] == 'CANCEL_REQUIRED':
                value = self.cancel(active)
                if value.get('verified') and value['state'] != 'CANCELLED':
                    return supervisor.response(value['state'], result=value, verified=True)
                return supervisor.decide(registry)
            if decision['state'] != 'RETRY_REQUIRED':
                return decision
            req, raw, successor = supervisor.pending_request(decision['worker'])
            upload_json(self.transport, f'ci/jobs/{active}/successor.json', successor)
            if self.transport.read(f'ci/jobs/{active}/successor.json') != encode(successor):
                raise GdiError('automatic successor changed; stop competing coordinators')
            self.retry_request(active)
            delivered = self.submit(req['publication_id'], req['worker_id'], req['profile_id'],
                                    retry_of=active, workflow=req.get('workflow'),
                                    github_repository=req.get('github_repository', ''), prepared_request=req)
            if delivered != req:
                raise GdiError('automatic retry delivery differs from saved request')
            supervisor.activate(req)
            return supervisor.response('REASSIGNED', worker_id=req['worker_id'], retry_of=active)

    def supervise_wait(self, jid, settings, *, watch=False, interval=30, timeout=3600):
        from .worker_config import seconds
        seconds(interval); seconds(timeout)
        if watch and interval < 30:
            raise GdiError('supervisor polling interval must be at least 30 seconds')
        deadline = time.monotonic() + timeout
        previous = None
        while True:
            # Git's shared worktree lock also serializes local selectors with
            # push/GC while a step publishes cancellation or a replacement.
            with self.git.lock():
                value = self.supervise(jid, settings)
            if value.get('verified'):
                result = value.get('result', value)
                return value, 0 if result['state'] == 'PASS' else 1
            if value['state'] == 'ATTEMPTS_EXHAUSTED':
                return value, 1
            if not watch:
                return value, 0
            summary = (value['job_id'], value['state'], tuple(value['attempts']))
            if summary != previous:
                print(f"[{value['job_id']}] {value['state']} attempts={len(value['attempts'])}", file=sys.stderr, flush=True)
                previous = summary
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**value, 'state': 'WATCH_TIMEOUT'}, 124
            time.sleep(min(interval, remaining))



    def status(self, jid, *, _request=None, heartbeat_timeout=600):
        from .worker_config import seconds
        seconds(heartbeat_timeout)
        req, raw = self.load_request(jid) if _request is None else _request
        if req['job_id'] != job_id(jid):
            raise GdiError('CI status/request job identity mismatch')
        result = self.result(jid, _request=(req, raw))
        if result is not None:
            return {**result, "verified": True}
        cancelled = self.cancellation_record(req, raw)
        listing = files(self.transport, f"ci/jobs/{jid}", optional=True)
        live, freshness = self.live_status(req, raw, heartbeat_timeout=heartbeat_timeout, listing=listing)
        if cancelled is not None:
            return {**req, 'state': 'CANCEL_REQUESTED', 'verified': False,
                    'heartbeat': freshness,
                    'worker_cancellation_supported': cancelled.get('worker_cancellation_supported', True)}
        if live is None:
            return {**req, "state": "QUEUED", "verified": False, 'heartbeat': freshness}
        return {**live, "verified": False, 'heartbeat': freshness}



    @staticmethod
    def write_log(stream, data):
        if hasattr(stream, 'buffer'):
            stream.buffer.write(data)
            stream.buffer.flush()
        else:
            stream.write(data.decode('utf-8', errors='replace'))
            stream.flush()

    def follow(self, jid, cursor, stream, *, _result=None):
        result = self.result(jid) if _result is None else _result
        if result is None:
            return cursor
        path = self.root / 'results' / jid / 'build.log'
        with path.open('rb') as source:
            source.seek(cursor)
            while True:
                data = source.read(1024 * 1024)
                if not data:
                    break
                self.write_log(stream, data)
                cursor += len(data)
        return cursor

    @contextmanager
    def cursor_session(self, jid, *, restart=False, _request=None):
        """One follower per job; offsets advance after flushing the final log."""
        with phase('verify CI request'):
            req, raw = self.load_request(jid) if _request is None else _request
            if req['job_id'] != job_id(jid):
                raise GdiError('CI cursor/request job identity mismatch')
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
            state = {'cursor_version': 2, 'job_id': jid, 'request_sha256': digest(raw),
                     'log_sha256': None, 'offset': 0}
            if path.exists() and not restart:
                saved = decode(path.read_bytes())
                if (not isinstance(saved, dict) or set(saved) != set(state) or
                        type(saved.get('cursor_version')) is not int or saved['cursor_version'] != 2 or
                        saved['job_id'] != jid or saved['request_sha256'] != digest(raw) or
                        (saved['log_sha256'] is not None and not hex_value(saved['log_sha256'], 64)) or
                        type(saved['offset']) is not int or saved['offset'] < 0 or
                        (saved['log_sha256'] is None and saved['offset'] != 0)):
                    raise GdiError('invalid CI follow cursor; use --restart to replay the log')
                state = saved
            atomic_write(path, encode(state))
            yield path, state

    def follow_saved(self, jid, cursor, stream, *, _result=None):
        result = self.result(jid) if _result is None else _result
        if result is None:
            return
        path, state = cursor
        log = next(spec for spec in result['artifacts'] if spec['path'] == 'build.log')
        if state['log_sha256'] not in (None, log['sha256']) or state['offset'] > log['bytes']:
            # A late cancellation has its own final log. Never mix two logs.
            raise GdiError('CI final log changed; use --restart to view the current result')
        state['log_sha256'] = log['sha256']
        source_path = self.root / 'results' / jid / 'build.log'
        verify_file(source_path, log)
        with source_path.open('rb') as source:
            source.seek(state['offset'])
            while True:
                data = source.read(1024 * 1024)
                if not data:
                    break
                self.write_log(stream, data)
                state['offset'] += len(data)
                atomic_write(path, encode(state))
        atomic_write(path, encode(state))

    def wait(self, jid, *, timeout=3600, follow=False, stream=None, interval=2, restart=False, heartbeat_timeout=600):
        from .worker_config import seconds
        if timeout is not None:
            seconds(timeout)
        seconds(interval)
        seconds(heartbeat_timeout)
        if restart and not follow:
            raise GdiError('--restart requires --follow')
        stream = stream or sys.stderr
        deadline, last = (time.monotonic() + timeout if timeout is not None else None), None
        first_poll = True
        initial_request = None
        if follow:
            with phase('read CI request'):
                initial_request = self.load_request(jid)
        with self.cursor_session(jid, restart=restart, _request=initial_request) if follow else nullcontext(None) as cursor:
            while True:
                with phase('read CI status') if first_poll else nullcontext():
                    value = self.status(jid, _request=initial_request if first_poll else None,
                                        heartbeat_timeout=heartbeat_timeout)
                if follow:
                    freshness = value.get('heartbeat', {})
                    summary = (value.get('state'), value.get('stage'), value.get('updated_at'), freshness.get('state'))
                    if summary != last:
                        age = freshness.get('age_seconds')
                        diagnostic = (f" heartbeat_status={summary[3].upper()}" +
                                      (f" age={age:.1f}s" if age is not None else '') +
                                      f" threshold={heartbeat_timeout:g}s" if summary[3] else '')
                        print(f"[{jid}] {summary[0]} stage={summary[1]} heartbeat={summary[2]}{diagnostic}", file=stream, flush=True)
                        last = summary
                    if value.get('verified'):
                        with phase('read final CI log'):
                            self.follow_saved(jid, cursor, stream, _result=value)
                first_poll = False
                if value.get('verified'):
                    return value, 0 if value['state'] == 'PASS' else 1
                if deadline is not None and time.monotonic() >= deadline:
                    return {'job_id': jid, 'state': 'WAIT_TIMEOUT', 'verified': False}, 124
                time.sleep(interval if deadline is None else min(interval, max(0, deadline - time.monotonic())))

    def logs(self, jid, *, output=None, follow=False, restart=False, heartbeat_timeout=600):
        from .worker_config import seconds
        seconds(heartbeat_timeout)
        if restart and (not follow or output):
            raise GdiError('--restart requires --follow')
        if output:
            result, code = self.wait(jid, heartbeat_timeout=heartbeat_timeout)
            if code == 124:
                raise GdiError("job is still running; use logs --follow or wait again")
            source = self.root / "results" / jid / "build.log"
            import shutil
            shutil.copyfile(source, Path(output))
            return
        if follow:
            self.wait(jid, follow=True, stream=sys.stdout, timeout=None, restart=restart, heartbeat_timeout=heartbeat_timeout)
        else:
            result = self.result(jid)
            if result is None:
                raise GdiError('CI result is not published; use status or logs --follow to wait')
            self.follow(jid, 0, sys.stdout, _result=result)

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
        self.transport.mkdir(f'ci/jobs/{jid}')
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
