"""Identify the local runner and clean only act containers for an owned checkout."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .ci_protocol import atomic_write
from .exchange import decode, digest, encode, file_digest, hex_value
from .git import GdiError


def command(argv, *, cwd=None):
    try:
        result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GdiError("runner tool unavailable: " + argv[0]) from exc
    if result.returncode:
        raise GdiError("runner tool failed: " + argv[0] + ": " + (result.stderr or result.stdout).strip())
    return result.stdout.strip()


def docker_identity(executable):
    value = command([executable, "info", "--format", '{{json .}}'])
    try:
        info = json.loads(value)
        return {"id": info["ID"], "version": info["ServerVersion"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise GdiError("invalid Docker daemon identity") from exc


def act_configuration():
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    paths = [config_home / 'act/actrc', Path.home() / '.actrc']
    return {str(path): file_digest(path) for path in paths if path.is_file()}


def resolve_profile(profile, cwd):
    """Revision binds actual act bytes and locally installed immutable base images."""
    configured = profile.get("configured_profile", {key: value for key, value in profile.items() if key != "revision"})
    workflow = dict(configured["workflow"])
    executable = shutil.which(workflow["executable"])
    if executable is None:
        raise GdiError("act not found; install the configured act version")
    version = command([executable, "--version"], cwd=cwd)
    if version != "act version " + workflow["act_version"]:
        raise GdiError("act version mismatch: expected " + workflow["act_version"] + ", got " + version)
    environment = {"act_version": workflow["act_version"], "act_sha256": file_digest(executable),
                   "platforms": {}, "docker": None, "act_configuration": act_configuration()}
    if any(image != "-self-hosted" for image in workflow["platforms"].values()):
        environment["docker"] = docker_identity(workflow["docker_executable"])
        endpoint = os.environ.get('DOCKER_HOST') or command([workflow['docker_executable'], 'context', 'inspect',
                    '--format', '{{.Endpoints.docker.Host}}'])
        if not endpoint.startswith(('unix://', 'tcp://')):
            raise GdiError('act requires an explicit unix:// or tcp:// Docker endpoint')
        environment['docker']['endpoint'] = endpoint
    for platform, image in workflow["platforms"].items():
        if image == "-self-hosted":
            environment["platforms"][platform] = image
        else:
            image_id = command([workflow["docker_executable"], "image", "inspect", "--format", "{{.Id}}", image])
            if not image_id.startswith("sha256:") or not hex_value(image_id[7:], 64):
                raise GdiError("invalid Docker image identity; pull the configured image before starting worker")
            environment["platforms"][platform] = image_id
    workflow["executable"] = executable
    workflow["platforms"] = dict(environment["platforms"])
    effective = {**configured, "workflow": workflow, "environment": environment, "configured_profile": configured}
    effective["revision"] = digest(encode({"profile": configured, "environment": environment}))
    return effective


def verify_environment(profile):
    environment = profile.get('environment')
    if environment is None:
        return
    if file_digest(profile['workflow']['executable']) != environment['act_sha256']:
        raise GdiError('act binary changed after capabilities; restart worker before accepting new jobs')
    if act_configuration() != environment['act_configuration']:
        raise GdiError('host act configuration changed after capabilities; restart worker')
    if environment['docker'] is not None:
        current = docker_identity(profile['workflow']['docker_executable'])
        if current != {key: environment['docker'][key] for key in ('id', 'version')}:
            raise GdiError('Docker environment changed after capabilities; restart worker')


def record_owner(spool, checkout, profile):
    if profile.get("environment", {}).get("docker") is None:
        return
    atomic_write(spool / "docker-owner.json", encode({"checkout": str(checkout.path),
                 "executable": profile["workflow"]["docker_executable"], "daemon": profile["environment"]["docker"]}))


def cleanup(spool):
    owner_path = spool / "docker-owner.json"
    if not owner_path.exists():
        return
    owner = decode(owner_path.read_bytes())
    checkout = str((spool / "checkout").resolve())
    if owner.get("checkout") != checkout:
        raise GdiError("Docker ownership checkout does not match durable job")
    executable = owner["executable"]
    current = docker_identity(executable)
    if current["id"] != owner["daemon"]["id"]:
        raise GdiError("restore the original Docker daemon to clean pending job containers")
    ids = command([executable, "ps", "--all", "--quiet", "--no-trunc", "--filter", "name=act-"]).splitlines()
    for cid in ids:
        if not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise GdiError("invalid Docker container identity")
        try:
            # Inspect only ownership fields; container secrets never enter the log.
            fields = command([executable, "inspect", "--format", '{{json .Name}} {{json .Config.WorkingDir}}', cid])
            decoder = json.JSONDecoder()
            name, offset = decoder.raw_decode(fields)
            workdir = json.loads(fields[offset:].strip())
        except (ValueError, TypeError) as exc:
            raise GdiError("invalid Docker container ownership") from exc
        if not isinstance(name, str) or not isinstance(workdir, str):
            raise GdiError("invalid Docker container ownership fields")
        if name.startswith("/act-") and workdir == checkout:
            command([executable, "rm", "--force", cid])
