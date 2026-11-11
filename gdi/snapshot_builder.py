"""Offline, versioned adapters from saved connector responses to snapshot v1."""

import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .diagnostics import phase
from .git import GdiError
from .publication import decode, encode
from .snapshot import Snapshot, listing_pages, local_file, metadata_file, opaque_id


ADAPTER = 'google-drive-list-folder-v1'
FOLDER_MIME = 'application/vnd.google-apps.folder'


class SnapshotBuildError(GdiError):
    def __init__(self, code, message, *, source=None, stage='connector adaptation'):
        super().__init__('snapshot builder: ' + message)
        self.code, self.source, self.stage = code, source, stage

    def result(self):
        return {'error': str(self), 'kind': 'SNAPSHOT_BUILD_ERROR', 'code': self.code,
                'stage': self.stage, 'source': self.source, 'inputs_preserved': True,
                'snapshot_written': False,
                'next_step': 'report_to_operator; do not assemble a snapshot manually'}


def fail(code, message, source=None):
    raise SnapshotBuildError(code, message, source=source)


def drive_id(url, folder=False):
    if not isinstance(url, str):
        fail('UNSUPPORTED_RESPONSE', 'expected a Google Drive URL')
    parsed = urlsplit(url)
    pattern = r'/drive/(?:u/[0-9]+/)?folders/([A-Za-z0-9_-]+)' if folder else r'/file/d/([A-Za-z0-9_-]+)(?:/view)?'
    match = re.fullmatch(pattern, parsed.path.rstrip('/'))
    if parsed.scheme != 'https' or parsed.netloc != 'drive.google.com' or not match:
        fail('UNSUPPORTED_RESPONSE', 'unsupported Google Drive URL format')
    return match[1]


def response(root, name):
    """Accept the saved tool result or its unchanged structuredContent object."""
    try:
        data = decode(metadata_file(local_file(root, name)))
    except (GdiError, OSError, ValueError, TypeError) as exc:
        fail('INVALID_INPUT', 'cannot read saved connector response', name)
    if not isinstance(data, dict):
        fail('UNSUPPORTED_RESPONSE', 'connector response must be an object', name)
    if data.get('isError') not in (None, False):
        fail('CONNECTOR_ERROR', 'connector returned an error', name)
    if 'structuredContent' in data and 'content' in data and isinstance(data['content'], list):
        data = data['structuredContent']
    if not isinstance(data, dict):
        fail('UNSUPPORTED_RESPONSE', 'missing structured connector data', name)
    return data


def listing(root, item, mime_types):
    if not isinstance(item, dict) or set(item) != {'request', 'response'}:
        fail('INVALID_CAPTURE', 'listing must contain request and response')
    request, name = item['request'], item['response']
    if not isinstance(request, dict) or set(request) != {'url', 'top_k'}:
        fail('UNSUPPORTED_REQUEST', 'list_folder capture requires exactly url and top_k', name)
    limit = request['top_k']
    # This profile deliberately does not assume that raising top_k bypasses a provider cap.
    if type(limit) is not int or not 1 <= limit <= 100:
        fail('UNSUPPORTED_REQUEST', 'list_folder adapter supports explicit top_k from 1 to 100', name)
    folder = drive_id(request['url'], folder=True)
    data = response(root, name)
    if not isinstance(data.get('files'), list):
        fail('UNSUPPORTED_RESPONSE', 'list_folder response has no files array; update the adapter', name)
    tokens = {'next_page_token', 'nextPageToken'}
    flags = {'has_more', 'truncated', 'incompleteSearch'}
    if set(data) - {'files'} - tokens - flags:
        fail('UNSUPPORTED_RESPONSE', 'unknown listing envelope fields; update the adapter', name)
    for flag in tokens | flags:
        value = data.get(flag)
        if value is not None and type(value) is not (str if flag in tokens else bool):
            fail('UNSUPPORTED_RESPONSE', 'invalid pagination/completeness field type', name)
        if value:
            fail('INCOMPLETE_LISTING', 'connector listing is partial or paginated; update the adapter', name)
    if len(data['files']) >= limit:
        fail('INCOMPLETE_LISTING', 'listing reached top_k; completeness is not established', name)
    entries = []
    for row in data['files']:
        required = {'id', 'title', 'mime_type', 'file_or_folder', 'size', 'url'}
        if not isinstance(row, dict) or not required.issubset(row):
            fail('UNSUPPORTED_RESPONSE', 'list_folder item is missing required metadata', name)
        if not isinstance(row['mime_type'], str) or not row['mime_type']:
            fail('UNSUPPORTED_RESPONSE', 'invalid MIME type', name)
        is_dir = row['mime_type'] == FOLDER_MIME
        if row['file_or_folder'] != ('folder' if is_dir else 'file'):
            fail('UNSUPPORTED_RESPONSE', 'conflicting item type and MIME type', name)
        identity = opaque_id(row['id'])
        if drive_id(row['url'], folder=is_dir) != identity:
            fail('INVALID_IDENTITY', 'item URL and Drive ID disagree', name)
        parents = row.get('parent_ids')
        if parents is not None and (not isinstance(parents, list) or folder not in parents):
            fail('INVALID_IDENTITY', 'item does not belong to the requested folder', name)
        size = row['size']
        if is_dir:
            if size is not None:
                fail('UNSUPPORTED_RESPONSE', 'folder size must be null', name)
        elif isinstance(size, str) and re.fullmatch(r'0|[1-9][0-9]*', size):
            size = int(size)
        elif type(size) is not int or size < 0:
            fail('UNSUPPORTED_RESPONSE', 'file size must be a nonnegative integer or decimal string', name)
        mime_types[identity] = row['mime_type']
        entries.append({'id': identity, 'name': row['title'], 'is_dir': is_dir, 'bytes': size})
    pages = [{'page_token': None, 'next_page_token': None, 'entries': entries}]
    listing_pages(pages)
    return {'folder_id': folder, 'pages': pages}


def download(root, item):
    if not isinstance(item, dict) or set(item) != {'response', 'local_path'}:
        fail('INVALID_CAPTURE', 'download must contain response and local_path')
    name = item['response']
    data = response(root, name)
    required = {'id', 'file_name', 'file_size_bytes', 'mime_type', 'url', 'file_uri'}
    if not required.issubset(data) or not isinstance(data['file_uri'], dict):
        fail('UNSUPPORTED_RESPONSE', 'raw fetch response is missing file metadata or file_uri', name)
    if data.get('b64_string') is not None or data.get('content') not in (None, ''):
        fail('UNSUPPORTED_RESPONSE', 'use raw fetch with include_base64=false', name)
    identity = opaque_id(data['id'])
    if drive_id(data['url']) != identity:
        fail('INVALID_IDENTITY', 'download URL and Drive ID disagree', name)
    if (type(data['file_size_bytes']) is not int or data['file_size_bytes'] < 0
            or not isinstance(data['file_name'], str) or not isinstance(data['mime_type'], str)):
        fail('UNSUPPORTED_RESPONSE', 'invalid raw download metadata', name)
    path = local_file(root, item['local_path'])
    if path.stat().st_size != data['file_size_bytes']:
        fail('DOWNLOAD_MISMATCH', 'materialized file size differs from fetch metadata', name)
    return {'file_id': identity, 'local_path': item['local_path']}, data


def build(capture_path, expected_id, output):
    """Build without network access, mutation of inputs, or replacement of old snapshots."""
    capture_path, output = Path(capture_path).absolute(), Path(output).absolute()
    temporary = None
    stage = 'capture validation'
    try:
        if output.parent != capture_path.parent:
            fail('INVALID_OUTPUT', 'output must be beside capture.json so local paths remain unchanged')
        if output.exists() or output.is_symlink():
            fail('INVALID_OUTPUT', 'output already exists; choose a new snapshot filename')
        capture = decode(metadata_file(capture_path))
        keys = {'capture_version', 'adapter', 'repository_path', 'repository_url', 'ref', 'listings', 'downloads'}
        if (not isinstance(capture, dict) or not keys.issubset(capture)
                or set(capture) - keys - {'archive_downloads', 'bundle_downloads'}
                or type(capture.get('capture_version')) is not int or capture['capture_version'] != 1):
            fail('INVALID_CAPTURE', 'unsupported capture schema/version')
        if capture['adapter'] != ADAPTER:
            fail('UNSUPPORTED_ADAPTER', 'unsupported connector adapter; update GDI instead of guessing fields')
        if not isinstance(capture['listings'], list) or not isinstance(capture['downloads'], list):
            fail('INVALID_CAPTURE', 'listings and downloads must be arrays')
        root = capture_path.parent
        with phase('adapt connector responses'):
            stage = 'connector adaptation'
            mime_types = {}
            listings = [listing(root, item, mime_types) for item in capture['listings']]
            files, details = [], []
            for item in capture['downloads']:
                descriptor, metadata = download(root, item)
                files.append(descriptor)
                details.append(metadata)
            addressed = {}
            for key, suffix in (('archive_downloads', '.json'), ('bundle_downloads', '.bundle')):
                addressed[key] = []
                if not isinstance(capture.get(key, []), list):
                    fail('INVALID_CAPTURE', key + ' must be an array')
                for item in capture.get(key, []):
                    if not isinstance(item, dict) or set(item) != {'response', 'local_path', 'sha256'}:
                        fail('INVALID_CAPTURE', key + ' requires response, local_path and sha256')
                    identity = item['sha256']
                    from .publication import hex_value
                    if not hex_value(identity, 64):
                        fail('INVALID_CAPTURE', 'invalid content-addressed checksum')
                    descriptor, metadata = download(root, {key: item[key] for key in ('response', 'local_path')})
                    if metadata['file_name'] not in (identity + suffix, identity + suffix + '.bin'):
                        fail('DOWNLOAD_MISMATCH', 'download filename does not match its checksum', metadata['id'])
                    addressed[key].append({**descriptor, 'sha256': identity})
        entries = {row['id']: row for folder in listings for page in folder['pages'] for row in page['entries']}
        for data in details:
            row = entries.get(data['id'])
            # Raw downloads may append a transport suffix; listing names remain canonical.
            if (row is None or row['is_dir'] or data['file_name'] not in (row['name'], row['name'] + '.bin')
                    or row['bytes'] != data['file_size_bytes']
                    or mime_types[data['id']] != data['mime_type']):
                fail('DOWNLOAD_MISMATCH', 'fetch metadata does not match saved listing', data['id'])
        value = {'snapshot_version': 1, 'repository_path': capture['repository_path'],
                 'repository_folder_id': drive_id(capture['repository_url'], folder=True),
                 'ref': capture['ref'], 'listings': listings, 'files': files}
        if addressed['archive_downloads']:
            value['archives'] = addressed['archive_downloads']
        if addressed['bundle_downloads']:
            value['bundle_downloads'] = addressed['bundle_downloads']
        stage = 'snapshot validation'
        with phase('validate assembled snapshot'):
            with tempfile.NamedTemporaryFile(dir=root, prefix='.gdi-snapshot-', suffix='.json', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encode(value))
                handle.flush()
                os.fsync(handle.fileno())
            snapshot = Snapshot(temporary, expected_id)
            report = summary(snapshot)
        stage = 'snapshot creation'
        # Atomic no-clobber publication: even a concurrent creator cannot lose a file.
        os.link(temporary, output)
        return {'snapshot': str(output), 'adapter': ADAPTER, 'inputs_preserved': True, **report}
    except SnapshotBuildError as exc:
        exc.stage = stage
        if exc.source is None:
            exc.source = capture_path.name
        raise
    except (GdiError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        # Do not echo raw connector objects, bearer URLs, or file contents in errors.
        raise SnapshotBuildError('INVALID_DATA', str(exc), stage=stage) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def summary(snapshot):
    """Metadata validation is distinct from bundle content verification and Drive freshness."""
    required = snapshot.required_bundles()
    checkpoint = getattr(snapshot.chain, 'checkpoint', None)
    return {'repository_id': snapshot.repository_id, 'repository_path': snapshot.repository_path,
            'ref': snapshot.ref, 'publications': len(snapshot.chain),
            'publication_id': snapshot.chain[-1][0] if snapshot.chain else None,
            'head': snapshot.chain[-1][1]['head'] if snapshot.chain else None,
            'publication_checkpoint': checkpoint,
            'metadata_scope': 'active_checkpoint_and_tail' if checkpoint else 'complete_publication_chain',
            'required_bundles': required, 'snapshot_fingerprint': snapshot.fingerprint,
            'freshness': 'connector_provided_snapshot',
            'validation': {'manifests_validated': len(snapshot.chain),
                           'bundles_required': len(required),
                           'required_bundles_present': sum(item['relative_path'] in snapshot.downloads for item in required),
                           'bundle_contents_verified': False,
                           'drive_freshness_verified': False}}
