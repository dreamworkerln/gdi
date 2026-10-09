"""Shared immutable notifications: one listing for every repository in a root."""

import re

from .exchange import decode, digest, encode, hex_value
from .git import GdiError

NAME = re.compile(r"([0-9a-f]{32})-([0-9a-f]{64})\.json")


def relative_repository(value):
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(part in ("", ".", "..") for part in value.split("/"))
            or value.split("/", 1)[0] in {"inbox", "ci"}):
        raise GdiError("invalid repository path in inbox notification")
    return value


def validate(value):
    keys = {"inbox_version", "event_id", "type", "repository_id", "repository_path", "ref", "head", "publication_id", "worker_id", "job_id", "request_sha256"}
    if set(value) != keys or type(value.get("inbox_version")) is not int or value["inbox_version"] != 1:
        raise GdiError("invalid inbox notification schema")
    for key, size in (("event_id", 32), ("repository_id", 32), ("head", 40), ("publication_id", 64)):
        if not hex_value(value[key], size):
            raise GdiError("invalid inbox " + key)
    relative_repository(value["repository_path"])
    if not isinstance(value["ref"], str) or not value["ref"].startswith("refs/heads/"):
        raise GdiError("invalid inbox branch ref")
    if value["type"] == "ci_requested":
        from .ci_protocol import identifier, job_id
        identifier(value["worker_id"]); job_id(value["job_id"])
        if value["event_id"] != value["job_id"] or not hex_value(value["request_sha256"], 64):
            raise GdiError("invalid inbox CI identity")
    elif value["type"] == "repository_updated":
        if any(value[key] is not None for key in ("worker_id", "job_id", "request_sha256")):
            raise GdiError("publication notification must not contain CI identity")
    else:
        raise GdiError("unknown inbox notification type")
    return value


def notification(repository_id, path, pub, publication_id, req=None, raw=None):
    value = {"inbox_version": 1, "event_id": req["job_id"] if req else publication_id[:32],
             "type": "ci_requested" if req else "repository_updated", "repository_id": repository_id,
             "repository_path": relative_repository(path), "ref": pub["ref"], "head": pub["head"],
             "publication_id": publication_id, "worker_id": req["worker_id"] if req else None,
             "job_id": req["job_id"] if req else None, "request_sha256": digest(raw) if req else None}
    validate(value)
    return value


def filename(value):
    return value["event_id"] + "-" + digest(encode(value)) + ".json"


def publish(transport, value):
    from .ci_protocol import upload_json
    validate(value)
    if not getattr(transport, 'creates_parents', False):
        transport.mkdir("inbox")
    upload_json(transport, "inbox/" + filename(value), value)


def read(transport, name):
    match = NAME.fullmatch(name)
    if not match:
        raise GdiError("invalid inbox notification filename")
    raw = transport.read("inbox/" + name)
    if digest(raw) != match[2]:
        raise GdiError("inbox checksum mismatch; keeping notification for retry")
    value = validate(decode(raw))
    if value["event_id"] != match[1] or filename(value) != name:
        raise GdiError("inbox filename/identity mismatch")
    return value
