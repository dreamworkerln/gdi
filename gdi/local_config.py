"""Repository-local settings owned by gdi; Git config is read only for migration."""

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
    return url[len(root) + 1:]


def load(git):
    path = git.gdi_dir() / "config.json"
    migrated = False
    if path.exists():
        data = decode(path.read_bytes())
        if set(data) != {"config_version", "remotes"} or type(data["config_version"]) is not int or data["config_version"] != 1 or not isinstance(data["remotes"], dict):
            raise GdiError("invalid .gdi/config.json")
    else:
        data = {"config_version": 1, "remotes": {}}
        output = git.call("config", "--local", "--get-regexp", r"^gdi\.remote\..*\.url$", allowed=(0, 1)).stdout
        for line in output.splitlines():
            key, url = line.split(" ", 1)
            name = key[len("gdi.remote."):-len(".url")]
            data["remotes"][name] = {"url": url, "repository_id": git.config(key[:-3] + "repositoryid"), "inbox_root": inferred_root(url)}
            migrated = True
    for name, value in data["remotes"].items():
        remote_name(name)
        if not isinstance(value, dict) or set(value) != {"url", "repository_id", "inbox_root"} or not hex_value(value["repository_id"], 32):
            raise GdiError("invalid gdi remote settings")
        validate_url(value["url"])
        if value["inbox_root"] is not None:
            repository_path(value["inbox_root"], value["url"])
    if migrated:
        save(git, data)
    return data


def save(git, data):
    from .ci_protocol import atomic_write
    if git.call("ls-files", "--", ".gdi").stdout:
        raise GdiError(".gdi is local metadata and must not be tracked by Git")
    atomic_write(git.gdi_dir() / "config.json", encode(data))


def get(git, name):
    remote_name(name)
    value = load(git)["remotes"].get(name)
    if value is None:
        raise GdiError("missing remote configuration: gdi remote add " + name + " <rclone:path>")
    return value
