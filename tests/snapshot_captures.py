"""Sanitized fixtures matching Google Drive list_folder/raw fetch structured data.

Field names were checked against connector responses on 2026-11-11. Fake IDs,
URLs and generated Git data keep real Drive files and signed download URLs out.
"""

from pathlib import Path

from gdi.publication import decode, digest, encode
from gdi.snapshot_builder import ADAPTER, FOLDER_MIME
from tests.snapshots import export_snapshot


def capture_snapshot(root, transport, *, downloads=None):
    source = export_snapshot(root, transport, include=['', 'branches', 'branches/main', 'bundles'],
                             downloads=downloads)
    root = Path(root)
    original = decode(source.read_bytes())
    responses = root / 'responses'
    responses.mkdir()
    def ident(old):
        return 'fixture-' + digest(old.encode())[:16]
    def url(identity, folder=False):
        return ('https://drive.google.com/drive/folders/' + identity if folder else
                'https://drive.google.com/file/d/' + identity + '/view')
    def save(name, data):
        relative = 'responses/' + name + '.json'
        (root / relative).write_bytes(encode({'content': [{'type': 'text', 'text': 'Action completed.'}],
                                            'structuredContent': data, 'isError': False}))
        return relative
    capture = {'capture_version': 1, 'adapter': ADAPTER, 'repository_path': 'project',
               'repository_url': url(ident(original['repository_folder_id']), True),
               'ref': original['ref'], 'listings': [], 'downloads': []}
    by_id = {}
    for number, listing in enumerate(original['listings']):
        files = []
        for page in listing['pages']:
            for row in page['entries']:
                data = {'id': ident(row['id']), 'title': row['name'],
                        'mime_type': FOLDER_MIME if row['is_dir'] else 'application/octet-stream',
                        'file_or_folder': 'folder' if row['is_dir'] else 'file',
                        'size': None if row['is_dir'] else str(row['bytes']),
                        'url': url(ident(row['id']), row['is_dir']),
                        'parent_ids': None, 'can_download': True}
                files.append(data)
                by_id[row['id']] = data
        capture['listings'].append({'request': {'url': url(ident(listing['folder_id']), True), 'top_k': 100},
                                    'response': save('listing-' + str(number), {'files': files})})
    for number, item in enumerate(original['files']):
        row = by_id[item['file_id']]
        data = {'id': row['id'], 'title': row['title'], 'file_name': row['title'],
                'mime_type': row['mime_type'], 'url': row['url'], 'file_size_bytes': int(row['size']),
                'file_uri': {'file_id': 'sediment://fake-' + row['id']}, 'content': '', 'b64_string': None}
        capture['downloads'].append({'response': save('fetch-' + str(number), data),
                                     'local_path': item['local_path']})
    path = root / 'capture.json'
    path.write_bytes(encode(capture))
    return path
