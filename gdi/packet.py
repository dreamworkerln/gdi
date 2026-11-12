"""Protocol v4: one immutable full Git packet and one mutable branch pointer.

Packet parsing and Git checks are local. Successful writes are never read back.
"""

from contextlib import contextmanager
from pathlib import Path
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile

from .branches import branch_directory
from .ci_protocol import atomic_write, identifier
from .git import GdiError, Git
from .inbox import relative_repository
from .publication import decode, digest, encode, file_digest, hex_value, prepare_publication, verify_bundle

PROTOCOL = 4
VERSION = 1
META_KEYS = {'packet_version', 'repository_id', 'repository_path', 'ref', 'head', 'previous',
             'previous_head', 'worker_id', 'request_id', 'bundle_sha256', 'bundle_bytes'}


def head_path(ref):
    return 'heads/' + branch_directory(ref) + '.json'


def packet_path(identity):
    if not hex_value(identity, 64):
        raise GdiError('invalid packet identity')
    return 'packets/' + identity + '.zip'


def metadata(value, repository_id, ref=None):
    if (not isinstance(value, dict) or set(value) != META_KEYS
            or type(value['packet_version']) is not int or value['packet_version'] != VERSION
            or not hex_value(repository_id, 32) or value['repository_id'] != repository_id
            or not hex_value(value['head'], 40) or not hex_value(value['bundle_sha256'], 64)
            or type(value['bundle_bytes']) is not int or value['bundle_bytes'] <= 0
            or not hex_value(value['request_id'], 32)
            or (value['previous'] is not None and not hex_value(value['previous'], 64))
            or (value['previous_head'] is not None and not hex_value(value['previous_head'], 40))
            or (value['previous'] is None) != (value['previous_head'] is None)):
        raise GdiError('invalid Git packet metadata or repository identity')
    branch_directory(value['ref'])
    if ref is not None and value['ref'] != ref:
        raise GdiError('packet belongs to another branch')
    if value['repository_path'] is not None:
        relative_repository(value['repository_path'])
    if value['worker_id'] is not None:
        identifier(value['worker_id'])
    return value


def pointer(value, repository_id, ref=None):
    if (not isinstance(value, dict) or set(value) != {'head_version', 'repository_id', 'ref',
            'publication_id', 'packet_bytes', 'metadata'} or type(value['head_version']) is not int
            or value['head_version'] != VERSION or value['repository_id'] != repository_id
            or not hex_value(value['publication_id'], 64)
            or type(value['packet_bytes']) is not int or value['packet_bytes'] <= 0):
        raise GdiError('invalid branch pointer or repository identity')
    data = metadata(value['metadata'], repository_id, ref)
    if value['ref'] != data['ref']:
        raise GdiError('branch pointer and packet ref differ')
    return value


def make_pointer(data, path):
    return pointer({'head_version': VERSION, 'repository_id': data['repository_id'],
                    'ref': data['ref'], 'publication_id': file_digest(path),
                    'packet_bytes': Path(path).stat().st_size, 'metadata': data}, data['repository_id'])


def publication(value):
    """Object-import/CI projection, not a separately uploaded manifest."""
    data = value['metadata']
    return {'version': PROTOCOL, 'repository_id': data['repository_id'], 'ref': data['ref'],
            'head': data['head'], 'previous': data['previous'], 'nonce': data['request_id'],
            'bundle_kind': 'full', 'bundle_sha256': data['bundle_sha256'],
            'bundle_bytes': data['bundle_bytes'], 'base_publication': None, 'base_head': None,
            'prerequisites': [], 'packet_pointer': value}


def read_head(transport, repository_id, ref):
    from .ci import optional_read
    raw = optional_read(transport, head_path(ref))
    return pointer(decode(raw), repository_id, ref) if raw is not None else None


def write_head(transport, value):
    with tempfile.TemporaryDirectory(prefix='gdi-head-') as temporary:
        path = Path(temporary) / 'head.json'
        path.write_bytes(encode(value))
        transport.update_head(path, head_path(value['ref']))


def publisher_id(root, previous=None, explicit=None):
    if explicit is not None:
        return identifier(explicit)
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'gdi/worker.json'
    if root is not None and config.is_file():
        try:
            value = json.loads(config.read_text())
        except (OSError, ValueError) as exc:
            raise GdiError('cannot read publisher worker config') from exc
        if isinstance(value, dict) and value.get('config_version') == 2 and value.get('remote_url') == root:
            return identifier(value.get('worker_id'))
    return previous['metadata']['worker_id'] if previous is not None else None


def build(git, repository_id, ref, directory, *, previous=None, repository_path=None,
          worker_id=None, request_id=None):
    prepared = prepare_publication(git, repository_id, ref, git.oid(ref), directory)
    data = metadata({'packet_version': VERSION, 'repository_id': repository_id,
        'repository_path': repository_path, 'ref': ref, 'head': prepared.data['head'],
        'previous': previous['publication_id'] if previous is not None else None,
        'previous_head': previous['metadata']['head'] if previous is not None else None,
        'worker_id': worker_id, 'request_id': request_id or uuid.uuid4().hex,
        'bundle_sha256': prepared.data['bundle_sha256'],
        'bundle_bytes': prepared.data['bundle_bytes']}, repository_id)
    path = Path(directory) / 'packet.zip'
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_STORED) as archive:
        for name, source in (('metadata.json', None), ('source.bundle', prepared.bundle)):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            if source is None:
                archive.writestr(entry, encode(data))
            else:
                with source.open('rb') as incoming, archive.open(entry, 'w', force_zip64=True) as target:
                    shutil.copyfileobj(incoming, target, 1024 * 1024)
    return path, make_pointer(data, path)


@contextmanager
def read(path, repository_id, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise GdiError('packet must be a regular local file')
    if expected is not None:
        pointer(expected, repository_id)
        if path.stat().st_size != expected['packet_bytes'] or file_digest(path) != expected['publication_id']:
            raise GdiError('Git packet checksum or size differs from its pointer')
    with tempfile.TemporaryDirectory(prefix='gdi-packet-') as temporary:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) != 2 or {entry.filename for entry in entries} != {'metadata.json', 'source.bundle'}:
                    raise GdiError('packet must contain exactly metadata.json and source.bundle')
                if any(entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16)
                       or entry.compress_type != zipfile.ZIP_STORED for entry in entries):
                    raise GdiError('unsupported packet entry')
                if archive.getinfo('metadata.json').file_size > 1024 * 1024:
                    raise GdiError('packet metadata exceeds 1 MiB')
                data = metadata(decode(archive.read('metadata.json')), repository_id)
                if expected is not None and data != expected['metadata']:
                    raise GdiError('packet metadata differs from its pointer')
                if archive.getinfo('source.bundle').file_size != data['bundle_bytes']:
                    raise GdiError('packet bundle size mismatch')
                bundle = Path(temporary) / 'source.bundle'
                with archive.open('source.bundle') as source, bundle.open('xb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
        except GdiError:
            raise
        except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise GdiError('invalid or incomplete Git packet') from exc
        value = expected if expected is not None else make_pointer(data, path)
        previous = {'head': data['previous_head']} if data['previous_head'] else None
        with verify_bundle(bundle, publication(value), data['ref'], previous=previous) as quarantine:
            from . import work
            if data['ref'].startswith(work.PREFIX):
                work.validate(Git(quarantine, isolated=True), data['ref'], data['head'], repository_id)
            yield value, quarantine


def load_publication(exchange, transport, repository_id, identity):
    """Fetch one historical/exact packet by ID; cache includes its local metadata."""
    directory = exchange.git.gdi_dir() / 'packets' / repository_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (identity + '.zip')
    packet_path(identity)
    saved = directory / (identity + '.json')
    from .cache import VerifiedCache
    cache = VerifiedCache(exchange.git, repository_id)
    if saved.exists():
        value = pointer(decode(saved.read_bytes()), repository_id)
        if value['publication_id'] != identity:
            raise GdiError('cached packet identity mismatch')
        if cache.contains(identity, value['metadata']['head']):
            return value, cache
    if not path.exists():
        temporary = directory / (identity + '.incoming')
        transport.download(packet_path(identity), temporary)
        if file_digest(temporary) != identity:
            temporary.unlink()
            raise GdiError('downloaded Git packet checksum mismatch')
        temporary.replace(path)
    with read(path, repository_id) as (value, quarantine):
        if value['publication_id'] != identity:
            raise GdiError('cached Git packet checksum mismatch')
        cache.accept(identity, value['metadata']['head'], quarantine)
        atomic_write(saved, encode(value))
    return value, cache


def remember(git, value):
    from .cache import VerifiedCache
    identity = value['publication_id']
    VerifiedCache(git, value['repository_id']).accept(identity, value['metadata']['head'], git.path)
    atomic_write(git.gdi_dir() / 'packets' / value['repository_id'] / (identity + '.json'), encode(value))
