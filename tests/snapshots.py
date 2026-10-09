"""Connector-shaped snapshot fixtures with stable fake Drive IDs and pagination."""

from pathlib import Path

from gdi.exchange import encode
from gdi.branches import branch_directory


def export_snapshot(root, transport, *, repository_path='project', ref='refs/heads/main',
                    include=None, downloads=None, page_size=2):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    include = set(include or ('', 'branches', 'branches/' + branch_directory(ref)))
    include.add('')
    # Resolve directory and file IDs by path, independent of page ordering.
    value = {'snapshot_version': 1, 'repository_path': repository_path,
             'repository_folder_id': 'id-root', 'ref': ref, 'listings': [], 'files': []}
    def identity(path):
        return 'id-' + path if path else 'id-root'
    selected_downloads = set(downloads) if downloads is not None else None
    for folder in sorted(include):
        rows = transport.list(folder)
        known = {row['Path'] for row in rows}
        for row in transport.list(folder, recursive=True):
            if '/' in row['Path'] and row['Path'].split('/')[0] not in known:
                name = row['Path'].split('/')[0]
                rows.append({'Path': name, 'IsDir': True})
                known.add(name)
        entries = [{'id': identity(folder + ('/' if folder else '') + row['Path']),
                    'name': row['Path'], 'is_dir': row['IsDir'],
                    'bytes': None if row['IsDir'] else len(transport.read(folder + ('/' if folder else '') + row['Path']))}
                   for row in rows]
        # Omitted selected branch is valid, but orphan listing would be invalid.
        if folder and not entries and not any(row['Path'] == folder.rsplit('/', 1)[-1] for row in
                transport.list(folder.rpartition('/')[0])):
            continue
        pages = []
        for offset in range(0, max(1, len(entries)), page_size):
            pages.append({'page_token': str(offset) if offset else None,
                          'next_page_token': str(offset + page_size) if offset + page_size < len(entries) else None,
                          'entries': entries[offset:offset + page_size]})
        value['listings'].append({'folder_id': identity(folder), 'pages': pages})
        for entry in entries:
            if entry['is_dir']:
                continue
            path = folder + ('/' if folder else '') + entry['name']
            if selected_downloads is not None and path not in selected_downloads:
                continue
            target = root / 'downloads' / path
            target.parent.mkdir(parents=True, exist_ok=True)
            transport.download(path, target)
            value['files'].append({'file_id': entry['id'], 'local_path': target.relative_to(root).as_posix()})
    path = root / 'snapshot.json'
    path.write_bytes(encode(value))
    return path
