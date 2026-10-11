#!/usr/bin/env python3
"""Build a reproducible, standard-library-only zipapp from committed GDI sources."""

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import zipfile


def git(repository, *args):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return subprocess.check_output(['git', '-C', str(repository), *args], env=environment)


def build(repository, output):
    repository = Path(repository).resolve()
    if git(repository, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise ValueError('commit all changes before building the runtime')
    head = git(repository, 'rev-parse', 'HEAD').decode().strip()
    ref = git(repository, 'symbolic-ref', 'HEAD').decode().strip()
    archive = git(repository, 'archive', '--format=tar', head, 'gdi', 'LICENSE')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source:
            if member.isdir():
                continue
            if not member.isfile() or any(part in ('', '.', '..') for part in member.name.split('/')):
                raise ValueError('runtime sources must be regular files: ' + member.name)
            if member.name.startswith('gdi/') or member.name == 'LICENSE':
                files[member.name] = source.extractfile(member).read()
    for name in ('gdi/__init__.py', 'gdi/__main__.py', 'gdi/cli.py'):
        if name not in files:
            raise ValueError('missing runtime source: ' + name)
    version = None
    for node in ast.parse(files['gdi/__init__.py']).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__version__'
                                               for target in node.targets):
            version = ast.literal_eval(node.value)
    if not isinstance(version, str):
        raise ValueError('missing GDI version')
    metadata = {'runtime_version': 1, 'version': version, 'head': head, 'ref': ref,
                'python_requires': '>=3.10', 'entrypoint': 'gdi.cli:main'}
    files['__main__.py'] = b'from gdi.cli import main\n\nraise SystemExit(main())\n'
    files['GDI_RUNTIME.json'] = (json.dumps(metadata, sort_keys=True, indent=2) + '\n').encode()
    output = Path(output).resolve()
    if output.is_relative_to(repository):
        for name in ('gdi.pyz', 'runtime.json', 'gdi.pyz.sha256'):
            relative = (output / name).relative_to(repository).as_posix()
            if git(repository, 'ls-files', '--', relative):
                raise ValueError('output would overwrite a tracked file: ' + relative)
            try:
                git(repository, 'check-ignore', '-q', '--', relative)
            except subprocess.CalledProcessError:
                raise ValueError('output inside the repository must be git-ignored: ' + relative) from None
    output.mkdir(parents=True, exist_ok=True)
    target = output / 'gdi.pyz'
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as destination:
        for name, data in sorted(files.items()):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            destination.writestr(item, data)
    metadata.update(sha256=hashlib.sha256(target.read_bytes()).hexdigest(), bytes=target.stat().st_size)
    (output / 'runtime.json').write_text(json.dumps(metadata, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    (output / 'gdi.pyz.sha256').write_text(metadata['sha256'] + '  gdi.pyz\n', encoding='ascii')
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        metadata = build(args.repo, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, 'build-runtime: ' + str(error) + '\n')
    print(json.dumps(metadata, sort_keys=True))


if __name__ == '__main__':
    main()
