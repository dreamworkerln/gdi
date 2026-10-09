"""rclone transport. No credentials are handled by gdi."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile

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
    # Both copy and copyto create the destination's parent directories.
    creates_parents = True

    def __init__(self, url, *, options=None):
        self.url = validate_url(url)
        self.options = options or {}

    def call(self, *args, allowed=(0,)):
        from .diagnostics import TransferProgress, transfer_progress_enabled
        progress = None
        if args[0] in ('copy', 'copyto') and transfer_progress_enabled():
            progress = TransferProgress('transfer')
            args = (*args, '--stats', '1s', '--stats-log-level', 'NOTICE', '--use-json-log')
        error = None
        try:
            return run(["rclone", *args, "--contimeout", str(self.options.get("connect_timeout_seconds", 10)) + "s",
                        "--timeout", str(self.options.get("timeout_seconds", 60)) + "s",
                        "--retries", str(self.options.get("retries", 3)),
                        "--low-level-retries", str(self.options.get("low_level_retries", 3))], allowed=allowed,
                       **({'stderr_line': progress.line} if progress is not None else {}))
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            if progress is not None:
                progress.finish(error)

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

    def read_many(self, relatives):
        """Fresh publication bytes in one process, selected from a checked listing."""
        paths = list(relatives)
        if len(paths) != len(set(paths)) or any(
                not re.fullmatch(r'branches/[^/]+/[0-9a-f]{64}\.json', path) for path in paths):
            raise GdiError('invalid batch publication paths')
        from .branches import directory_ref
        for path in paths:
            directory_ref(path.split('/')[1])
        if not paths:
            return {}
        if len(paths) == 1:
            return {paths[0]: self.read(paths[0])}
        from .diagnostics import note
        note('download publication metadata batch', files=len(paths), paths=paths)
        with tempfile.TemporaryDirectory(prefix='gdi-metadata-') as temporary:
            root = Path(temporary)
            selection = root / 'files.txt'
            selection.write_text(''.join(path + '\n' for path in paths), encoding='utf-8')
            destination = root / 'snapshot'
            self.call('copy', self.url, str(destination), '--files-from-raw', str(selection),
                      '--no-traverse')
            result = {}
            for path in paths:
                source = destination / path
                if source.is_symlink() or not source.is_file():
                    raise GdiError('remote metadata missing or exceeds 1 MiB: ' + path)
                with source.open('rb') as handle:
                    raw = handle.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise GdiError('metadata exceeds 1 MiB: ' + path)
                result[path] = raw
            return result

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
        # Single-file copy/copyto bypass rclone's immutable guard. Stage one file
        # under its remote name and use directory copy's guarded sync path.
        parent, _, name = relative.rpartition('/')
        if name in ('', '.', '..') or '\\' in name:
            raise GdiError('invalid upload filename')
        source = Path(source).resolve(strict=True)
        from .diagnostics import note
        note('upload immutable file', path=relative, source_file_bytes=source.stat().st_size)
        with tempfile.TemporaryDirectory(prefix='gdi-upload-') as temporary:
            staged = Path(temporary) / name
            try:
                os.link(source, staged)
            except OSError:
                shutil.copyfile(source, staged)
            self.call('copy', temporary, self.path(parent), '--immutable', '--checksum', '--no-traverse')

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
