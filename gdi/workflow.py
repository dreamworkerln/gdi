"""Run GitHub Actions from the verified checkout using nektos/act."""

import json
import os
from pathlib import Path
import zipfile

from .ci_protocol import atomic_write
from .exchange import encode
from .executor import inside
from .git import GdiError


def prepare(checkout, profile, spool, req, *, cache_dir=None):
    """Resolve only paths and runner options; act owns all workflow semantics."""
    workflow = profile["workflow"]
    path = inside(checkout.path, workflow["path"])
    candidates = sorted(path.iterdir()) if path.is_dir() else [path]
    files = [item for item in candidates if item.suffix in (".yml", ".yaml") and item.is_file()]
    if not files:
        raise GdiError("no workflow YAML files found: " + workflow["path"])
    for item in files:
        inside(checkout.path, item.relative_to(checkout.path))
        if item.is_symlink():
            raise GdiError("workflow YAML must be a regular tracked file")
        checkout.call("ls-files", "--error-unmatch", "--", item.relative_to(checkout.path).as_posix())
    executable = workflow["executable"]
    if "/" in executable and not Path(executable).is_absolute():
        raise GdiError("workflow executable must be absolute or a command in PATH")
    for name in workflow["secrets"]:
        if name not in os.environ:
            raise GdiError("missing workflow secret in worker environment: " + name)
    event = {"ref": req["ref"], "after": req["head"], "act": True,
             "inputs": workflow["inputs"], "head_commit": {"id": req["head"]}}
    repository = workflow["repository"]
    if repository:
        owner, name = repository.split("/")
        event["repository"] = {"full_name": repository, "name": name, "owner": {"login": owner, "name": owner}}
    event_path = spool / "workflow-event.json"
    atomic_write(event_path, encode(event))
    uploads = spool / "workflow-uploads"
    uploads.mkdir(exist_ok=True)
    cache_dir = Path(cache_dir) if cache_dir is not None else spool.parent / "act-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    argv = [executable, workflow["event"], "--directory", str(checkout.path),
            "--workflows", str(path), "--no-recurse", "--eventpath", str(event_path),
            "--artifact-server-path", str(uploads), "--artifact-server-port", str(workflow["artifact_server_port"]),
            "--action-cache-path", str(cache_dir),
            "--env-file", os.devnull, "--secret-file", os.devnull,
            "--var-file", os.devnull, "--input-file", os.devnull,
            "--json", "--rm", "--bind=false", "--reuse=false", "--no-skip-checkout=false",
            "--dryrun=false", "--list=false", "--graph=false", "--watch=false", "--validate=false",
            "--job", workflow["job"], "--env", "GITHUB_REF=" + req["ref"],
            "--env", "SHA_REF=" + req["head"]]
    if repository:
        argv += ["--env", "GITHUB_REPOSITORY=" + repository]
    for key, flag in (("platforms", "--platform"), ("inputs", "--input"), ("vars", "--var")):
        for name, value in sorted(workflow[key].items()):
            argv += [flag, name + "=" + value]
    for name, value in sorted(profile["env"].items()):
        if name in {"GITHUB_REF", "SHA_REF", "GITHUB_REPOSITORY"}:
            raise GdiError("workflow env cannot override the verified Git identity")
        argv += ["--env", name + "=" + value]
    for name in workflow["secrets"]:
        argv += ["--secret", name]
    # Run outside checkout so a committed .actrc cannot replace the execution plan.
    effective = dict(profile)
    effective["stages"] = [{"name": "github-actions", "argv": argv, "cwd": ".", "blocking": True,
                            "timeout_seconds": profile["timeout_seconds"]}]
    return effective


def require_completed_job(log):
    """Exit zero with all jobs skipped (or a dry run) is not verified CI."""
    with log.open("rb") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if (isinstance(record, dict) and not record.get("raw_output")
                    and record.get("jobResult") == "success"):
                return
    raise GdiError("workflow runner completed without any successful jobs")


def collect_uploads(spool):
    """Preserve act's artifact store, including original uploaded archives."""
    root = spool / "workflow-uploads"
    if not root.exists():
        return None
    paths = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GdiError("workflow artifact symlink is not allowed")
        inside(root, path.relative_to(root))
        if path.is_file():
            paths.append(path)
    if not paths:
        return None
    destination = spool / "artifacts" / "workflow-artifacts.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(root).as_posix())
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return destination
