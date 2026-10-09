"""CI v1 identities and artifact verification, independent of Git protocol v3."""

from datetime import datetime, timezone
import os
import math
from pathlib import Path
import re
import tempfile

from .exchange import decode, digest, encode, file_digest, hex_value
from .git import GdiError

TERMINAL = {"PASS", "FAIL", "ERROR", "TIMEOUT", "INTERRUPTED", "REJECTED"}
REQUEST_KEYS = {"ci_version", "job_id", "repository_id", "ref", "head", "publication_id",
                "worker_id", "profile_id", "profile_revision", "created_at", "retry_of"}


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise GdiError("CI worker/profile/stage names must use ASCII letters, digits, '_' or '-'")
    return value


def job_id(value):
    if not hex_value(value, 32):
        raise GdiError("CI job ID must be 32 lowercase hexadecimal characters")
    return value


def request(value, git, repository_id=None):
    version = value.get("ci_version")
    keys = REQUEST_KEYS | ({"workflow"} if version == 2 else set())
    if version == 2 and "github_repository" in value:
        keys.add("github_repository")
        if not isinstance(value["github_repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["github_repository"]):
            raise GdiError("invalid CI GitHub repository namespace")
    if set(value) != keys or type(version) is not int or version not in (1, 2):
        raise GdiError("unsupported or invalid CI request schema")
    if version == 2 and workflow_selection(value["workflow"]) != value["workflow"]:
        raise GdiError("CI workflow selector is not normalized")
    job_id(value["job_id"])
    for key, length in (("repository_id", 32), ("head", 40), ("publication_id", 64), ("profile_revision", 64)):
        if not hex_value(value[key], length):
            raise GdiError("invalid CI request " + key)
    if repository_id is not None and value["repository_id"] != repository_id:
        raise GdiError("CI repository identity mismatch")
    for key in ("worker_id", "profile_id"):
        identifier(value[key])
    if not isinstance(value["ref"], str) or not value["ref"].startswith("refs/heads/"):
        raise GdiError("CI request must name a branch ref")
    git.ref(value["ref"][11:])
    if value["retry_of"] is not None:
        job_id(value["retry_of"])
        if value["retry_of"] == value["job_id"]:
            raise GdiError("CI request cannot retry itself")
    try:
        if not isinstance(value["created_at"], str) or datetime.fromisoformat(value["created_at"]).tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise GdiError("invalid CI request timestamp") from exc
    return value


def workflow_selection(value=None):
    from .worker_config import relative_path
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"path", "event", "job", "inputs"}:
        raise GdiError("invalid CI workflow selector")
    result = {"path": value.get("path", ".github/workflows"), "event": value.get("event", "push"),
              "job": value.get("job", ""), "inputs": value.get("inputs", {})}
    relative_path(result["path"])
    if not isinstance(result["event"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", result["event"]):
        raise GdiError("invalid CI workflow event")
    if not isinstance(result["job"], str) or (result["job"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", result["job"])):
        raise GdiError("invalid CI workflow job")
    inputs = result["inputs"]
    if not isinstance(inputs, dict) or any(not isinstance(k, str) or not k or '=' in k or "\x00" in k
            or not isinstance(v, str) or "\x00" in v for k, v in inputs.items()):
        raise GdiError("workflow inputs must contain string keys/values")
    return result


def marker(req, raw):
    return {"ci_version": req["ci_version"], "job_id": req["job_id"], "request_sha256": digest(raw)}


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def upload_json(transport, path, value, *, mutable=False):
    with tempfile.TemporaryDirectory(prefix="gdi-json-") as directory:
        source = Path(directory) / "metadata.json"
        source.write_bytes(encode(value))
        (transport.update_advisory if mutable else transport.upload)(source, path)


def descriptor(path, name):
    return {"path": name, "bytes": Path(path).stat().st_size, "sha256": file_digest(path), "complete": True}


def validate_descriptor(value):
    if (not isinstance(value, dict) or set(value) != {"path", "bytes", "sha256", "complete"}
            or not isinstance(value["path"], str)
            or not re.fullmatch(r"(?:build\.log|final-status\.json|artifacts/[A-Za-z0-9][A-Za-z0-9_.-]{0,127})", value["path"])
            or type(value["bytes"]) is not int or value["bytes"] < 0
            or not hex_value(value["sha256"], 64) or value["complete"] is not True):
        raise GdiError("invalid CI artifact descriptor")
    return value


def verify_file(path, desc):
    validate_descriptor(desc)
    if Path(path).stat().st_size != desc["bytes"] or file_digest(path) != desc["sha256"]:
        raise GdiError("CI artifact checksum/size mismatch: " + desc["path"])


def validate_result(value, req, request_sha256):
    keys = {"ci_version", "job_id", "repository_id", "ref", "head", "publication_id", "worker_id",
            "profile_id", "profile_revision", "request_sha256", "run_id", "state", "exit_code",
            "failed_stage", "stages", "warnings", "started_at", "finished_at", "duration_seconds", "artifacts", "detail"}
    identity = {key: req[key] for key in ("ci_version", "job_id", "repository_id", "ref", "head",
                                         "publication_id", "worker_id", "profile_id", "profile_revision")}
    if (set(value) != keys or any(value.get(key) != item for key, item in identity.items())
            or value.get("request_sha256") != request_sha256
            or not hex_value(value.get("run_id"), 32) or value.get("state") not in TERMINAL
            or type(value.get("exit_code")) is not int
            or not isinstance(value.get("stages"), list) or not isinstance(value.get("warnings"), list)
            or not isinstance(value.get("detail"), str)
            or type(value.get("duration_seconds")) not in (float, int) or not math.isfinite(value["duration_seconds"]) or value["duration_seconds"] < 0):
        raise GdiError("invalid CI result or request/result identity mismatch")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list):
        raise GdiError("CI result artifacts must be a list")
    names = [validate_descriptor(item)["path"] for item in artifacts]
    if len(names) != len(set(names)) or not {"build.log", "final-status.json"}.issubset(names):
        raise GdiError("CI result missing required artifacts or duplicate paths")
    if value["state"] == "PASS":
        stages = value["stages"]
        for stage in stages:
            if (not isinstance(stage, dict) or set(stage) != {"name", "blocking", "state", "exit_code"}
                    or type(stage["blocking"]) is not bool or type(stage["exit_code"]) is not int):
                raise GdiError("invalid CI stage result")
        if (value["exit_code"] != 0 or not stages or not any(s["blocking"] for s in stages)
                or any(s["blocking"] and (s["state"] != "PASS" or s["exit_code"] != 0) for s in stages)):
            raise GdiError("CI PASS has no successful complete blocking stages")
    return value


def capability_revision(capabilities, worker_id, profile_id, repository_id=None):
    """Use the same advertised identity and revision checks for both client modes."""
    identifier(worker_id); identifier(profile_id)
    if (not isinstance(capabilities, dict) or type(capabilities.get('ci_version')) is not int or
            capabilities['ci_version'] != 1 or capabilities.get('worker_id') != worker_id):
        raise GdiError('invalid worker capabilities')
    if repository_id is None and (type(capabilities.get('inbox_version')) is not int or capabilities['inbox_version'] != 1):
        raise GdiError('worker does not support the shared inbox')
    try:
        revision = (capabilities['profiles'][profile_id] if repository_id is None else
                    capabilities['repositories'][repository_id][profile_id])
    except (KeyError, TypeError) as exc:
        raise GdiError('worker does not advertise the requested CI execution settings') from exc
    if not hex_value(revision, 64):
        raise GdiError('invalid worker capabilities revision')
    return revision


def prepare_request(git, repository_id, publication_id, pub, worker_id, profile_id, revision,
                    *, shared=True, workflow=None, github_repository='', retry_of=None):
    import uuid
    value = {'ci_version': 2 if shared else 1, 'job_id': uuid.uuid4().hex,
             'repository_id': repository_id, 'ref': pub['ref'], 'head': pub['head'],
             'publication_id': publication_id, 'worker_id': worker_id, 'profile_id': profile_id,
             'profile_revision': revision, 'created_at': now(), 'retry_of': retry_of}
    if shared:
        value['workflow'] = workflow_selection(workflow)
        if github_repository:
            value['github_repository'] = github_repository
    return request(value, git, repository_id)
