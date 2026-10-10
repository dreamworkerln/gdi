"""Repository-local settings and explicit default connection owned by gdi."""

from .exchange import decode, encode, hex_value, remote_name
from .git import GdiError
from .transport import validate_url


def inferred_root(url):
    remote, path = url.split(":", 1)
    parent, separator, name = path.rpartition("/")
    return remote + ":" + parent if separator and parent and parent != "/" else None


def repository_path(root, url):
    validate_url(root); validate_url(url)
    if not url.startswith(root + "/"):
        raise GdiError("repository URL must be below the inbox root")
    from .inbox import relative_repository
    return relative_repository(url[len(root) + 1:])


def load(git):
    path = git.gdi_dir() / "config.json"
    if path.exists():
        data = decode(path.read_bytes())
        if set(data) != {"config_version", "default_remote", "remotes"} or type(data["config_version"]) is not int or data["config_version"] != 2 or not isinstance(data["remotes"], dict):
            raise GdiError("unsupported .gdi/config.json: recreate local connections with config version 2 (preserve the old file separately)")
    else:
        data = {"config_version": 2, "default_remote": None, "remotes": {}}
    for name, value in data["remotes"].items():
        remote_name(name)
        if not isinstance(value, dict) or set(value) != {"url", "repository_id", "inbox_root"} or not hex_value(value["repository_id"], 32):
            raise GdiError("invalid gdi remote settings")
        validate_url(value["url"])
        if value["inbox_root"] is not None:
            repository_path(value["inbox_root"], value["url"])
    default = data['default_remote']
    if default is not None and (not isinstance(default, str) or default not in data['remotes']):
        raise GdiError('invalid default remote in .gdi/config.json')
    return data


def select(git, name=None):
    data = load(git)
    if name is not None:
        get(git, name)
        return name
    if data['default_remote'] is not None:
        return data['default_remote']
    if len(data['remotes']) == 1:
        return next(iter(data['remotes']))
    if not data['remotes']:
        raise GdiError('no gdi connections: gdi remote add drive <rclone:path> --init')
    raise GdiError('multiple connections: specify a remote or run gdi remote default <name>')


def set_default(git, name):
    get(git, name)
    data = load(git)
    data['default_remote'] = name
    save(git, data)


def save(git, data):
    from .ci_protocol import atomic_write
    if git.call("ls-files", "--", ".gdi").stdout:
        raise GdiError(".gdi is local metadata and must not be tracked by git")
    atomic_write(git.gdi_dir() / "config.json", encode(data))


def get(git, name):
    remote_name(name)
    value = load(git)["remotes"].get(name)
    if value is None:
        raise GdiError("missing remote configuration: gdi remote add " + name + " <rclone:path>")
    return value
