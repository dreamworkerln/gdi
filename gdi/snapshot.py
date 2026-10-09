"""Read-only connector snapshots. Freshness and remote completeness are connector duties."""

from pathlib import Path
import shutil
import stat

from .branches import branch_directory
from .git import GdiError, Git
from .inbox import relative_repository
from .publication import bundle_sequence, decode, digest, hex_value, validate_publications, validate_repository


MAX_METADATA = 1024 * 1024


def local_file(root, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative or
            any(part in ('', '.', '..') for part in relative.split('/')) or
            any(ord(c) < 32 or ord(c) == 127 for c in relative)):
        raise GdiError('invalid snapshot local path')
    path = Path(root)
    for part in relative.split('/'):
        path = path / part
        if path.is_symlink():
            raise GdiError('snapshot files must not traverse symlinks')
    if not stat.S_ISREG(path.stat().st_mode):
        raise GdiError('snapshot download must be a regular file')
    return path


def metadata_file(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise GdiError('metadata must be a regular file')
    with path.open('rb') as handle:
        raw = handle.read(MAX_METADATA + 1)
    if len(raw) > MAX_METADATA:
        raise GdiError('metadata exceeds 1 MiB')
    return raw


def opaque_id(value):
    if (not isinstance(value, str) or not value or len(value) > 512 or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise GdiError('invalid connector file/folder ID')
    return value


def listing_pages(pages, ids=None):
    ids = ids if ids is not None else set()
    if not isinstance(pages, list) or not pages:
        raise GdiError('listing requires all pages, including an empty first page')
    token, seen_tokens, entries, names = None, set(), [], set()
    for number, page in enumerate(pages):
        if (not isinstance(page, dict) or set(page) != {'page_token', 'next_page_token', 'entries'} or
                page['page_token'] != token or token in seen_tokens or
                not isinstance(page['entries'], list)):
            raise GdiError('incomplete or repeated listing pages')
        seen_tokens.add(token)
        for item in page['entries']:
            if (not isinstance(item, dict) or set(item) != {'id', 'name', 'is_dir', 'bytes'} or
                    type(item['is_dir']) is not bool or not isinstance(item['name'], str) or
                    not item['name'] or item['name'] in ('.', '..') or '/' in item['name'] or
                    '\\' in item['name'] or any(ord(c) < 32 or ord(c) == 127 for c in item['name']) or
                    (item['bytes'] is not None if item['is_dir'] else
                     type(item['bytes']) is not int or item['bytes'] < 0)):
                raise GdiError('invalid snapshot listing entry')
            identity = opaque_id(item['id'])
            if identity in ids or item['name'] in names:
                raise GdiError('duplicate Drive names or file IDs in snapshot')
            ids.add(identity); names.add(item['name']); entries.append(item)
        token = page['next_page_token']
        if token is not None:
            opaque_id(token)
        if (number == len(pages) - 1) != (token is None):
            raise GdiError('incomplete or trailing listing pages')
    return entries


class Snapshot:
    def __init__(self, path, expected_id, *, ref=None):
        if not hex_value(expected_id, 32):
            raise GdiError('expected repository ID must be supplied independently')
        self.path = Path(path).absolute()
        self.document = decode(metadata_file(self.path))
        value = self.document
        keys = {'snapshot_version', 'repository_path', 'repository_folder_id', 'ref', 'listings', 'files'}
        if set(value) != keys or type(value.get('snapshot_version')) is not int or value['snapshot_version'] != 1:
            raise GdiError('unsupported connector snapshot schema')
        self.repository_path = relative_repository(value['repository_path'])
        self.ref = value['ref']
        branch_directory(self.ref)
        Git(self.path.parent, isolated=True).ref(self.ref[11:])
        if ref is not None and self.ref != ref:
            raise GdiError('snapshot belongs to another branch')
        root_id = opaque_id(value['repository_folder_id'])
        if not isinstance(value['listings'], list) or not isinstance(value['files'], list):
            raise GdiError('snapshot listings/files must be lists')
        folders, ids = {}, {root_id}
        for listing in value['listings']:
            if not isinstance(listing, dict) or set(listing) != {'folder_id', 'pages'}:
                raise GdiError('invalid snapshot listing')
            folder = opaque_id(listing['folder_id'])
            if folder in folders:
                raise GdiError('duplicate folder listing')
            entries = listing_pages(listing['pages'], ids)
            folders[folder] = entries
        if root_id not in folders:
            raise GdiError('repository folder listing is missing')
        self.entries, self.folders = {}, {}
        visiting = set()
        def walk(folder, prefix):
            if folder in visiting:
                raise GdiError('cyclic snapshot folders')
            visiting.add(folder)
            if folder in folders:
                self.folders[prefix] = folders[folder]
                for item in folders[folder]:
                    name = prefix + ('/' if prefix else '') + item['name']
                    self.entries[name] = item
                    if item['is_dir']:
                        walk(item['id'], name)
        walk(root_id, '')
        if len(self.folders) != len(folders):
            raise GdiError('snapshot contains unrelated folder listings')
        by_id = {item['id']: name for name, item in self.entries.items() if not item['is_dir']}
        self.downloads = {}
        used_local = set()
        for item in value['files']:
            if not isinstance(item, dict) or set(item) != {'file_id', 'local_path'}:
                raise GdiError('invalid snapshot download descriptor')
            identity = opaque_id(item['file_id'])
            if identity not in by_id or by_id[identity] in self.downloads:
                raise GdiError('unlisted or duplicate snapshot download')
            local = local_file(self.path.parent, item['local_path'])
            if local in used_local:
                raise GdiError('snapshot downloads must use distinct local files')
            used_local.add(local)
            name = by_id[identity]
            if local.stat().st_size != self.entries[name]['bytes']:
                raise GdiError('snapshot download size differs from listing')
            self.downloads[name] = local
        for name in ('branches', 'bundles'):
            if name not in self.entries or not self.entries[name]['is_dir']:
                raise GdiError('repository must contain branches and bundles folders')
        self.repository = validate_repository(decode(self.read('repository.json')))
        self.repository_id = self.repository['repository_id']
        if self.repository_id != expected_id:
            raise GdiError('repository ID mismatch; snapshot is for a replaced or different repository')
        directory = branch_directory(self.ref)
        parent = self.list('branches')
        selected = [item for item in parent if item['Path'] == directory]
        if selected and selected[0]['IsDir'] is not True:
            raise GdiError('publication branch must be a directory')
        listing = ([{**item, 'Path': directory + '/' + item['Path']}
                    for item in self.list('branches/' + directory)] if selected else [])
        manifests = {item['Path']: self.read('branches/' + item['Path']) for item in listing
                     if not item['IsDir'] and item['Path'].endswith('.json')}
        self.chain = validate_publications(expected_id, self.ref, listing, manifests)
        self.fingerprint = digest(self.read('repository.json') + b''.join(
            identity.encode('ascii') for identity, _ in self.chain))

    def required_bundles(self):
        return [{'relative_path': 'bundles/' + data['bundle_sha256'] + '.bundle',
                 'target_folder': self.repository_path + '/bundles',
                 'folder_id': self.entries['bundles']['id'],
                 'target_name': data['bundle_sha256'] + '.bundle',
                 'sha256': data['bundle_sha256'], 'bytes': data['bundle_bytes'],
                 'publication_id': identity, 'bundle_kind': data['bundle_kind']}
                for identity, data in bundle_sequence(self.chain)]

    def list(self, relative, *, recursive=False):
        if relative not in self.folders:
            raise GdiError('snapshot requires a complete listing of folder: ' + relative)
        result = []
        for item in self.folders[relative]:
            result.append({'Path': item['name'], 'IsDir': item['is_dir'], 'Size': item['bytes']})
            if recursive and item['is_dir']:
                result.extend({**row, 'Path': item['name'] + '/' + row['Path']}
                              for row in self.list(relative + ('/' if relative else '') + item['name'], recursive=True))
        return result

    def downloaded_file(self, relative):
        if relative not in self.downloads:
            raise GdiError('snapshot download is missing: ' + relative)
        # Revalidate at use, not just construction.
        path = self.downloads[relative]
        checked = local_file(self.path.parent, str(path.relative_to(self.path.parent)))
        if checked.stat().st_size != self.entries[relative]['bytes']:
            raise GdiError('snapshot download size changed: ' + relative)
        return checked

    def read(self, relative):
        return metadata_file(self.downloaded_file(relative))

    def read_optional(self, relative):
        parent, _, name = relative.rpartition('/')
        listing = self.list(parent)
        if name not in {item['Path'] for item in listing}:
            return None
        return self.read(relative)

    def download(self, relative, target):
        shutil.copyfile(self.downloaded_file(relative), target)

    def require_file(self, relative, checksum, size):
        parent, _, name = relative.rpartition('/')
        self.list(parent)  # Prove uniqueness against a complete listing.
        path = self.downloaded_file(relative)
        from .publication import file_digest
        if path.stat().st_size != size or file_digest(path) != checksum:
            raise GdiError('uploaded file checksum/size mismatch: ' + relative)
        return path


def download_proof(path, expected_name, checksum, size):
    """Check a connector-downloaded file against a complete parent folder listing."""
    path = Path(path).absolute()
    value = decode(metadata_file(path))
    if (set(value) != {'proof_version', 'folder_id', 'pages', 'file_id', 'local_path'} or
            type(value['proof_version']) is not int or value['proof_version'] != 1):
        raise GdiError('invalid connector download proof')
    opaque_id(value['folder_id'])
    entries = listing_pages(value['pages'])
    selected = [item for item in entries if item['name'] == expected_name]
    if len(selected) != 1 or selected[0]['is_dir'] or selected[0]['id'] != value['file_id']:
        raise GdiError('download proof filename/identity mismatch')
    file = local_file(path.parent, value['local_path'])
    from .publication import file_digest
    if selected[0]['bytes'] != size or file.stat().st_size != size or file_digest(file) != checksum:
        raise GdiError('download proof checksum/size mismatch')
    return file
