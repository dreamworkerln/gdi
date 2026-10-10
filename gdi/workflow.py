"""Run GitHub Actions from the verified checkout using nektos/act."""

import json
import os
from pathlib import Path
import zipfile

from .ci_protocol import atomic_write
from .exchange import encode
from .executor import inside
from .git import GdiError


def validate_selection(git, head, selector):
    """Validate paths from the committed tree before publishing any Git data."""
    selected = Path(selector["path"]).as_posix().rstrip("/")
    prefix = "" if selected == "." else selected + "/"
    records = git.call("ls-tree", "-r", "-z", head, "--", selected).stdout.split("\0")
    files = []
    for record in records:
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        if path == selected or path.startswith(prefix) and "/" not in path[len(prefix):]:
            if path.endswith((".yml", ".yaml")):
                if not metadata.startswith(("100644 blob ", "100755 blob ")):
                    raise GdiError("workflow YAML must be a regular tracked file")
                files.append(path)
    if not files:
        raise GdiError("no workflow YAML files found in the selected commit: " + selector["path"])
    return files


def prepare(checkout, profile, spool, req, *, cache_dir=None):
    """Resolve only paths and runner options; act owns all workflow semantics."""
    workflow = profile["workflow"]
    from .runner import verify_environment
    verify_environment(profile)
    validate_selection(checkout, req["head"], workflow)
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
    if (spool / '.actrc').exists():
        raise GdiError('job spool must not contain .actrc')
    if "/" in executable and not Path(executable).is_absolute():
        raise GdiError("workflow executable must be absolute or a command in PATH")
    for name in workflow["secrets"]:
        if name not in os.environ:
            raise GdiError("missing workflow secret in worker environment: " + name)
    event = {"ref": req["ref"], "after": req["head"], "act": True,
             "inputs": workflow["inputs"], "head_commit": {"id": req["head"]}}
    repository = req.get("github_repository", workflow["repository"])
    if repository:
        owner, name = repository.split("/")
        event["repository"] = {"full_name": repository, "name": name, "owner": {"login": owner, "name": owner}}
    event_path = spool / "workflow-event.json"
    atomic_write(event_path, encode(event))
    uploads = spool / "workflow-uploads"
    uploads.mkdir(exist_ok=True)
    cache_dir = Path(cache_dir) if cache_dir is not None else spool.parent / "act-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # act's --env NAME sets an empty value rather than reading os.environ.
    # Its YAML env file accepts JSON strings without dotenv interpolation.
    # Keep proxy credentials outside argv, checkout and published artifacts.
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy")
    proxy_env = {name: os.environ[name] for name in (*proxy_names, "NO_PROXY", "no_proxy")
                 if name in os.environ and name not in profile["env"]
                 and name.swapcase() not in profile["env"]}
    if any(os.environ.get(name) or profile["env"].get(name) for name in proxy_names):
        entries = list(dict.fromkeys(item.strip()
                       for source in ("NO_PROXY", "no_proxy")
                       for item in os.environ.get(source, '').split(',') if item.strip()))
        for address in ("localhost", "127.0.0.1", "::1", workflow["artifact_server_addr"]):
            if address not in entries:
                entries.append(address)
        for name in ("NO_PROXY", "no_proxy"):
            if not any(alias in profile["env"] for alias in ("NO_PROXY", "no_proxy")):
                proxy_env[name] = ','.join(entries)
    proxy_file = spool / 'worker-proxy-env.yaml'
    atomic_write(proxy_file, encode(proxy_env))
    argv = [executable, workflow["event"], "--directory", str(checkout.path),
            "--workflows", str(path), "--no-recurse", "--eventpath", str(event_path),
            "--artifact-server-path", str(uploads), "--artifact-server-port", str(workflow["artifact_server_port"]),
            "--artifact-server-addr", workflow["artifact_server_addr"], "--network", "host",
            "--action-cache-path", str(cache_dir),
            "--cache-server-path", str(cache_dir / 'cache-server'),
            "--cache-server-addr", workflow["artifact_server_addr"], "--cache-server-port", "0",
            "--env-file", str(proxy_file), "--secret-file", os.devnull,
            "--var-file", os.devnull, "--input-file", os.devnull,
            "--json", "--rm", "--pull=false", "--bind=false", "--reuse=false", "--no-skip-checkout=false",
            "--use-gitignore=false",
            "--dryrun=false", "--list=false", "--graph=false", "--watch=false", "--validate=false",
            "--job", workflow["job"], "--env", "GITHUB_REF=" + req["ref"],
            "--env", "SHA_REF=" + req["head"]]
    if repository:
        argv += ["--env", "GITHUB_REPOSITORY=" + repository, "--env", "GITHUB_REPOSITORY_OWNER=" + repository.split("/")[0]]
    for key, flag in (("platforms", "--platform"), ("inputs", "--input"), ("vars", "--var")):
        for name, value in sorted(workflow[key].items()):
            argv += [flag, name + "=" + value]
    for name, value in sorted(profile["env"].items()):
        if name in {"GITHUB_REF", "SHA_REF", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER"}:
            raise GdiError("workflow env cannot override the verified git identity")
        argv += ["--env", name + "=" + value]
    for name in workflow["secrets"]:
        argv += ["--secret", name]
    # Run outside checkout so a committed .actrc cannot replace the execution plan.
    effective = dict(profile)
    effective['process_env'] = {'XDG_CACHE_HOME': str(cache_dir / 'xdg')}
    if profile.get('environment', {}).get('docker') is not None:
        effective['process_env']['DOCKER_HOST'] = profile['environment']['docker']['endpoint']
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
