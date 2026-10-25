"""One immutable, content-addressed delivery for a complete CI result."""

from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile
import zlib

from .ci_protocol import validate_result, verify_file
from .exchange import decode, encode, file_digest
from .git import GdiError


PACKAGE_PATTERN = re.compile(r'result-([0-9a-f]{64})\.zip')
MAX_METADATA = 1024 * 1024
PACKAGE_METADATA = encode({'package_version': 1})


def package_checksum(name):
    match = PACKAGE_PATTERN.fullmatch(name)
    if match is None:
        raise GdiError('invalid CI result package name')
    return match[1]


def _metadata(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_METADATA:
        raise GdiError('CI package metadata exceeds 1 MiB')
    with archive.open(info) as source:
        data = source.read(MAX_METADATA + 1)
    if len(data) > MAX_METADATA:
        raise GdiError('CI package metadata exceeds 1 MiB')
    return data


def _contents(archive, req, request_sha256):
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise GdiError('duplicate CI result package entry')
    for info in infos:
        mode = info.external_attr >> 16
        if (info.orig_filename != info.filename or info.is_dir() or
                (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or info.flag_bits & 1 or
                info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise GdiError('invalid CI result package entry')
    if not {'package.json', 'result.json'}.issubset(names):
        raise GdiError('CI result package metadata is missing')
    if _metadata(archive, 'package.json') != PACKAGE_METADATA:
        raise GdiError('unsupported CI result package version')
    raw = _metadata(archive, 'result.json')
    value = decode(raw)
    if not isinstance(value, dict):
        raise GdiError('invalid CI package result metadata')
    result = validate_result(value, req, request_sha256)
    if encode(result) != raw:
        raise GdiError('CI result package metadata is not canonical')
    expected = {'package.json', 'result.json'} | {item['path'] for item in result['artifacts']}
    if set(names) != expected:
        raise GdiError('CI result package has missing or unexpected files')
    for spec in result['artifacts']:
        if archive.getinfo(spec['path']).file_size != spec['bytes']:
            raise GdiError('CI result package artifact size mismatch: ' + spec['path'])
    return result


def read_package(path, req, request_sha256, *, destination=None, metadata_only=False):
    """Verify the filename hash, identities and exact inventory; stream artifacts."""
    path = Path(path)
    checksum = package_checksum(path.name)
    if path.is_symlink() or not path.is_file() or file_digest(path) != checksum:
        raise GdiError('CI result package checksum mismatch')
    try:
        with zipfile.ZipFile(path) as archive:
            result = _contents(archive, req, request_sha256)
            if metadata_only:
                return result
            for spec in result['artifacts']:
                target = None if destination is None else Path(destination) / spec['path']
                if target is not None:
                    parent = Path(destination)
                    if parent.is_symlink():
                        raise GdiError('CI result destination must not traverse symlinks')
                    for part in Path(spec['path']).parts[:-1]:
                        parent /= part
                        if parent.is_symlink():
                            raise GdiError('CI result destination must not traverse symlinks')
                    if target.is_symlink():
                        raise GdiError('CI result destination must not be a symlink')
                    try:
                        verify_file(target, spec)
                    except (OSError, GdiError):
                        pass
                    else:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                pending = None
                try:
                    with nullcontext(None) if target is None else tempfile.NamedTemporaryFile(
                            dir=target.parent, prefix='.' + target.name + '-', suffix='.pending', delete=False) as output:
                        if target is not None:
                            pending = Path(output.name)
                        checksum, size = hashlib.sha256(), 0
                        with archive.open(spec['path']) as source:
                            while True:
                                data = source.read(1024 * 1024)
                                if not data:
                                    break
                                checksum.update(data)
                                size += len(data)
                                if size > spec['bytes']:
                                    raise GdiError('CI package artifact exceeds declared size')
                                if output is not None:
                                    output.write(data)
                        if size != spec['bytes'] or checksum.hexdigest() != spec['sha256']:
                            raise GdiError('CI package artifact checksum/size mismatch: ' + spec['path'])
                        if target is not None:
                            output.flush()
                            os.fsync(output.fileno())
                    if target is not None:
                        os.replace(pending, target)
                finally:
                    if pending is not None:
                        pending.unlink(missing_ok=True)
            return result
    except (zipfile.BadZipFile, KeyError, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
        raise GdiError('invalid CI result package: ' + str(exc)) from exc


def _entry(name):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def build_package(spool, req, request_sha256):
    """Build reproducibly from the durable result; a retry never reruns execution."""
    spool = Path(spool)
    raw = (spool / 'result.json').read_bytes()
    if len(raw) > MAX_METADATA:
        raise GdiError('CI package metadata exceeds 1 MiB')
    result = validate_result(decode(raw), req, request_sha256)
    if encode(result) != raw:
        raise GdiError('durable CI result is not canonical')
    for spec in result['artifacts']:
        source = spool / spec['path']
        if source.is_symlink() or not source.is_file():
            raise GdiError('CI package source must be a regular file')
        verify_file(source, spec)
    previous = list(spool.glob('result-*.zip'))
    if previous:
        if len(previous) != 1 or read_package(previous[0], req, request_sha256) != result:
            raise GdiError('durable CI result package differs from finalized result')
        return previous[0]
    descriptor, temporary = tempfile.mkstemp(dir=spool, prefix='.result-', suffix='.pending')
    pending = Path(temporary)
    try:
        with os.fdopen(descriptor, 'w+b') as output:
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr(_entry('package.json'), PACKAGE_METADATA)
                archive.writestr(_entry('result.json'), raw)
                for spec in sorted(result['artifacts'], key=lambda item: item['path']):
                    with (spool / spec['path']).open('rb') as source, archive.open(
                            _entry(spec['path']), 'w', force_zip64=True) as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        target = spool / ('result-' + file_digest(pending) + '.zip')
        os.replace(pending, target)
        # A source could change while being archived. Do not publish those bytes.
        try:
            if read_package(target, req, request_sha256) != result:
                raise GdiError('CI package differs from finalized result')
        except (GdiError, OSError):
            target.unlink(missing_ok=True)
            raise
        directory = os.open(spool, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return target
    finally:
        pending.unlink(missing_ok=True)
