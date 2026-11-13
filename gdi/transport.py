"""rclone transport. No credentials are handled by gdi."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

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

    def __init__(self, url, *, options=None, network=None):
        self.url = validate_url(url)
        self.options = options or {}
        from .network import NetworkRoutes
        self.network = network if network is not None else NetworkRoutes()

    def call(self, *args, allowed=(0,)):
        from .diagnostics import TransferProgress, transfer_progress_enabled
        progress = None
        if args[0] in ('copy', 'copyto') and transfer_progress_enabled():
            progress = TransferProgress('transfer')
            args = (*args, '--stats', '1s', '--stats-log-level', 'NOTICE', '--use-json-log')
        from .rclone_policy import diagnostic_log, flags
        log_path, log_line = diagnostic_log(getattr(self.network, 'log_kind', 'cli'))
        def stderr_line(line):
            log_line(line)
            if progress is not None:
                progress.line(line)
        started = time.monotonic()
        log_line(f'operation start: {args[0]}')
        error = None
        try:
            return self.network.run(lambda route: run(["rclone", *args, *flags(self.options), "--contimeout", str(self.options.get("connect_timeout_seconds", 10)) + "s",
                        "--timeout", str(self.options.get("timeout_seconds", 60)) + "s",
                        "--retries", str(self.options.get("retries", 1)),
                        "--low-level-retries", str(self.options.get("low_level_retries", 1))], allowed=allowed,
                       environment=self.network.environment(route),
                       stderr_line=stderr_line))
        except BaseException as exc:
            error = type(exc).__name__
            if isinstance(exc, GdiError):
                raise GdiError(str(exc) + f"; rclone log: {log_path}") from exc
            raise
        finally:
            log_line(f'operation finish: {args[0]} seconds={time.monotonic() - started:.3f} error={error}')
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
        # One content read, no type/size stat before it. Consumers validate the
        # returned metadata locally; rclone cat has directory-stream semantics.
        result = self.call('cat', self.path(relative), allowed=(0, 3, 4))
        return None if result.returncode in (3, 4) else result.stdout.encode('utf-8')

    def update_head(self, source, relative):
        validate_head_path(relative)
        self.call('copyto', str(source), self.path(relative), '--ignore-times')

    def packet_exists(self, relative):
        validate_packet_path(relative)
        return self.call('lsjson', self.path(relative), '--stat', allowed=(0, 3, 4)).returncode == 0

    def move_packet(self, source_url, source_relative, target_relative):
        validate_url(source_url)
        from .agent_sync import PUSH_NAME
        if not source_relative.startswith('inbox/') or not PUSH_NAME.fullmatch(source_relative[6:]):
            raise GdiError('expected an exact agent inbox packet')
        validate_packet_path(target_relative)
        if source_url.split(':', 1)[0] != self.url.split(':', 1)[0]:
            raise GdiError('packet adoption requires the same rclone backend')
        self.call('moveto', source_url + '/' + source_relative, self.path(target_relative), '--ignore-times')

    def update_repository(self, source):
        from .publication import decode, validate_repository
        if validate_repository(decode(Path(source).read_bytes()))['version'] != 4:
            raise GdiError('upgrade requires protocol v4 metadata')
        self.call('copyto', str(source), self.path('repository.json'), '--ignore-times')

    def delete_packet(self, relative):
        validate_packet_path(relative)
        self.call('deletefile', self.path(relative))

    def delete_head(self, relative):
        validate_head_path(relative)
        self.call('deletefile', self.path(relative))

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
        from .agent_sync import PUSH_NAME
        if not (re.fullmatch(r"inbox/[0-9a-f]{32}-[0-9a-f]{64}\.json", relative)
                or relative.startswith('inbox/') and PUSH_NAME.fullmatch(relative[len('inbox/'):])):
            raise GdiError("only a single immutable inbox notification may be removed")
        self.call("deletefile", self.path(relative))

    def delete_bundle(self, relative):
        if not re.fullmatch(r"bundles/[0-9a-f]{64}\.bundle", relative):
            raise GdiError("GC may delete only a single content-addressed bundle")
        self.call("deletefile", self.path(relative))

    def enable_work(self, source):
        # Разрешённая миграция меняет только work_version, не identity/history.
        from .publication import decode, validate_repository
        before = validate_repository(decode(self.read('repository.json')))
        after = validate_repository(decode(Path(source).read_bytes()))
        if after != {**before, 'work_version': 1}:
            raise GdiError('work activation may only add work_version=1 to the same repository')
        self.call('copyto', str(source), self.path('repository.json'), '--ignore-times')

    def delete_work_manifest(self, relative):
        validate_work_manifest_path(relative)
        self.call('deletefile', self.path(relative))

    def enable_checkpoints(self, source):
        from .publication import decode, validate_repository
        from .publication_checkpoints import CAPABILITY
        before = validate_repository(decode(self.read('repository.json')))
        after = validate_repository(decode(Path(source).read_bytes()))
        if after != {**before, CAPABILITY: 1}:
            raise GdiError('checkpoint activation may only add its capability to the same repository')
        self.call('copyto', str(source), self.path('repository.json'), '--ignore-times')

    def update_checkpoint(self, source, relative):
        validate_checkpoint_path(relative)
        self.call('copyto', str(source), self.path(relative), '--ignore-times')

    def update_agent_export(self, source, relative):
        validate_agent_export_path(relative)
        self.call('copyto', str(source), self.path(relative), '--ignore-times')

    def delete_publication(self, relative):
        validate_publication_path(relative)
        self.call('deletefile', self.path(relative))

    def delete_checkpoint(self, relative):
        validate_checkpoint_path(relative)
        self.call('deletefile', self.path(relative))

    def remove_work_directory(self, relative):
        validate_work_directory(relative)
        self.call('rmdir', self.path(relative))


def validate_work_directory(relative):
    from .branches import directory_ref
    from .work import PREFIX, work_id
    if not relative.startswith('branches/'):
        raise GdiError('only a retired work branch directory may be removed')
    ref = directory_ref(relative[len('branches/'):])
    if not ref.startswith(PREFIX):
        raise GdiError('ordinary branch directories must not be removed')
    work_id(ref[len(PREFIX):])


def validate_publication_path(relative):
    from .branches import directory_ref
    match = re.fullmatch(r'branches/([^/]+)/[0-9a-f]{64}\.json', relative)
    if not match:
        raise GdiError('only an exact archived publication may be removed')
    directory_ref(match[1])


def validate_checkpoint_path(relative):
    from .branches import directory_ref
    match = re.fullmatch(r'branches/([^/]+)/checkpoint\.json', relative)
    if not match:
        raise GdiError('expected an exact branch checkpoint path')
    directory_ref(match[1])


def validate_agent_export_path(relative):
    if not re.fullmatch(r'agent/export-[0-9a-f]{32}-[0-9a-f]{64}\.zip', relative):
        raise GdiError('only an exact agent export may be replaced')


def validate_work_manifest_path(relative):
    parent, separator, name = relative.rpartition('/')
    validate_work_directory(parent)
    if not separator or not re.fullmatch(r'[0-9a-f]{64}\.json', name):
        raise GdiError('only an exact retired work manifest may be removed')


def validate_head_path(relative):
    from .branches import directory_ref
    match = re.fullmatch(r'heads/([^/]+)\.json', relative)
    if not match:
        raise GdiError('expected one branch pointer')
    directory_ref(match[1])


def validate_packet_path(relative):
    if not re.fullmatch(r'packets/[0-9a-f]{64}\.zip', relative):
        raise GdiError('expected one content-addressed Git packet')
