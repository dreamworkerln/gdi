"""Locally registered execution profiles. Requests never supply executable commands."""

import math
import os
import re
from pathlib import Path, PurePosixPath

from .ci_protocol import identifier
from .exchange import decode, digest, encode, hex_value
from .git import GdiError
from .transport import validate_url


def relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or '..' in value.split('/')):
        raise GdiError("profile paths must stay inside the job checkout")
    return value


def seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise GdiError("timeouts/poll intervals must be positive finite numbers")
    return value


def directory(kind):
    base = os.environ.get("XDG_" + kind.upper() + "_HOME")
    return Path(base).expanduser().resolve() / "gdi" if base else Path.home() / (".cache" if kind == "cache" else ".local/state") / "gdi"


def load_config(path):
    config = decode(Path(path).expanduser().read_bytes())
    allowed = {"config_version", "worker_id", "repositories", "poll_active_seconds", "poll_idle_max_seconds", "state_dir", "cache_dir"}
    if set(config) - allowed or type(config.get("config_version")) is not int or config["config_version"] != 1:
        raise GdiError("invalid worker config; expected config_version 1")
    identifier(config.get("worker_id"))
    config["poll_active_seconds"] = seconds(config.get("poll_active_seconds", 5))
    config["poll_idle_max_seconds"] = seconds(config.get("poll_idle_max_seconds", 60))
    if config["poll_idle_max_seconds"] < config["poll_active_seconds"]:
        raise GdiError("poll_idle_max_seconds must be >= poll_active_seconds")
    for key, kind in (("state_dir", "state"), ("cache_dir", "cache")):
        value = config.get(key, str(directory(kind)))
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise GdiError(key + " must be an absolute path")
        config[key] = str(Path(value).expanduser().resolve())
    repos = config.get("repositories")
    if not isinstance(repos, list) or not repos:
        raise GdiError("worker needs at least one registered repository")
    ids, urls = set(), set()
    for repo in repos:
        if (not isinstance(repo, dict) or set(repo) != {"repository_id", "remote_url", "profiles"}
                or not hex_value(repo["repository_id"], 32)):
            raise GdiError("invalid registered repository")
        validate_url(repo["remote_url"])
        if repo["repository_id"] in ids or repo["remote_url"] in urls:
            raise GdiError("duplicate repository ID/URL in worker config")
        ids.add(repo["repository_id"]); urls.add(repo["remote_url"])
        if not isinstance(repo["profiles"], dict) or not repo["profiles"]:
            raise GdiError("registered repository has no profiles")
        for name, profile in repo["profiles"].items():
            identifier(name)
            if not isinstance(profile, dict) or set(profile) - {"timeout_seconds", "stages", "artifacts", "env"}:
                raise GdiError("invalid profile fields (revision is computed, never configured)")
            profile["timeout_seconds"] = seconds(profile.get("timeout_seconds", 3600))
            env = profile.setdefault("env", {})
            if (not isinstance(env, dict) or any(not isinstance(k, str) or not k or '=' in k or '\x00' in k
                    or not isinstance(v, str) or '\x00' in v for k, v in env.items())):
                raise GdiError("profile env must contain string keys/values")
            stages = profile.get("stages")
            if not isinstance(stages, list) or not stages:
                raise GdiError("profile must have stages")
            names = set()
            for stage in stages:
                if not isinstance(stage, dict) or set(stage) - {"name", "argv", "cwd", "blocking", "timeout_seconds"}:
                    raise GdiError("invalid stage fields")
                identifier(stage.get("name"))
                if stage["name"] in names:
                    raise GdiError("duplicate stage name")
                names.add(stage["name"])
                argv = stage.get("argv")
                if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\x00' in a for a in argv) or not argv[0]:
                    raise GdiError("stage argv must be a non-empty list of strings")
                relative_path(stage.setdefault("cwd", "."))
                if type(stage.setdefault("blocking", True)) is not bool:
                    raise GdiError("stage blocking must be boolean")
                stage["timeout_seconds"] = seconds(stage.get("timeout_seconds", profile["timeout_seconds"]))
            if not any(stage["blocking"] for stage in stages):
                raise GdiError("profile requires at least one blocking stage")
            artifacts = profile.setdefault("artifacts", [])
            if not isinstance(artifacts, list):
                raise GdiError("artifacts must be a list")
            artifact_names = set()
            for artifact in artifacts:
                if not isinstance(artifact, dict) or set(artifact) != {"name", "path", "required"}:
                    raise GdiError("artifact needs name, path and required")
                if not isinstance(artifact["name"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", artifact["name"]) or artifact["name"] in artifact_names:
                    raise GdiError("invalid/duplicate artifact name")
                artifact_names.add(artifact["name"])
                relative_path(artifact["path"])
                if type(artifact["required"]) is not bool:
                    raise GdiError("artifact required must be boolean")
            # Publish only the digest. Secrets belong to inherited host environment, not env.
            public = profile
            profile["revision"] = digest(encode(public))
    return config
