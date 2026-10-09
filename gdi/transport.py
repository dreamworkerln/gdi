"""rclone transport. No credentials are handled by gdi."""

import json
from pathlib import Path
import re

from .git import GdiError, run


def validate_url(url):
    if (not isinstance(url, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*:.+", url)
            or any(ord(char) < 32 or ord(char) == 127 for char in url)):
        raise GdiError("URL must be a configured rclone remote plus a dedicated folder, e.g. gdrive:gdi/project")
    path = url.split(":", 1)[1]
    if any(part in ("", ".", "..") for part in path.removeprefix("/").split("/")):
        raise GdiError("remote folder must not contain empty, '.' or '..' components")
    return url


class Rclone:
    def __init__(self, url, *, options=None):
        self.url = validate_url(url)
        self.options = options or {}

    def call(self, *args, allowed=(0,)):
        return run(["rclone", *args, "--contimeout", str(self.options.get("connect_timeout_seconds", 10)) + "s",
                    "--timeout", str(self.options.get("timeout_seconds", 60)) + "s",
                    "--retries", str(self.options.get("retries", 3)),
                    "--low-level-retries", str(self.options.get("low_level_retries", 3))], allowed=allowed)

    def path(self, relative):
        return self.url + ("/" + relative if relative else "")

    def mkdir(self, relative):
        self.call("mkdir", self.path(relative))

    def list(self, relative, *, recursive=False):
        args = ["lsjson", self.path(relative)]
        if recursive:
            args.append("--recursive")
        try:
            data = json.loads(self.call(*args).stdout)
        except json.JSONDecodeError as exc:
            raise GdiError("rclone returned invalid listing JSON") from exc
        if not isinstance(data, list) or any(not isinstance(item, dict) or
                not isinstance(item.get("Path"), str) or type(item.get("IsDir")) is not bool for item in data):
            raise GdiError("rclone returned an invalid directory listing")
        names = [item["Path"] for item in data]
        if len(names) != len(set(names)):
            raise GdiError("duplicate paths on remote; resolve duplicate Drive names before continuing")
        return data

    def read(self, relative):
        return self.call("cat", self.path(relative)).stdout.encode("utf-8")

    def read_optional(self, relative):
        """Only documented not-found codes permit legacy discovery."""
        result = self.call("lsjson", self.path(relative), "--stat", allowed=(0, 3, 4))
        if result.returncode in (3, 4):
            return None
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise GdiError("rclone returned invalid file stat JSON") from exc
        if not isinstance(value, dict) or value.get("IsDir") is not False:
            raise GdiError("expected a regular remote metadata file: " + relative)
        return self.read(relative)

    def download(self, relative, target):
        self.call("copyto", self.path(relative), str(target))

    def upload(self, source, relative):
        self.call("copyto", str(Path(source)), self.path(relative), "--immutable")

    def update_advisory(self, source, relative):
        if not re.fullmatch(r"ci/(?:workers/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/(?:status|capabilities)|jobs/[0-9a-f]{32}/status)\.json", relative):
            raise GdiError("only CI advisory snapshots may be overwritten")
        self.call("copyto", str(Path(source)), self.path(relative), "--ignore-times")

    def delete_queue(self, relative):
        if not re.fullmatch(r"ci/queue/[0-9a-f]{32}\.json", relative):
            raise GdiError("only a single CI queue pointer may be removed")
        self.call("deletefile", self.path(relative))

    def delete_notification(self, relative):
        if not re.fullmatch(r"inbox/[0-9a-f]{32}-[0-9a-f]{64}\.json", relative):
            raise GdiError("only a single immutable inbox notification may be removed")
        self.call("deletefile", self.path(relative))

    def delete_bundle(self, relative):
        if not re.fullmatch(r"bundles/[0-9a-f]{64}\.bundle", relative):
            raise GdiError("GC may delete only a single content-addressed bundle")
        self.call("deletefile", self.path(relative))
