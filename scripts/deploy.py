#!/usr/bin/env python3
"""Build, verify and publish the committed GDI runtime for cloud agents."""

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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


def environment():
    values = {key: value for key, value in os.environ.items()
              if not key.startswith('GIT_') and key not in ('PYTHONPATH', 'PYTHONHOME')}
    values['PYTHONDONTWRITEBYTECODE'] = '1'
    return values


def main(argv=None):
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-only', action='store_true', help='build and verify locally without remote changes')
    parser.add_argument('--output', type=Path, default=repository / 'dist/runtime', help='runtime output directory')
    parser.add_argument('--runtime-dir', default='gdrive:gdi/gdi/runtime', help='rclone runtime destination')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    process_environment = environment()
    stage = 'build'
    try:
        for executable in ('git',) + (() if args.build_only else ('rclone',)):
            if not shutil.which(executable):
                raise ValueError('required executable not found: ' + executable)
        print('deploy: building the committed runtime', flush=True)
        metadata = build(repository, output)
        print(json.dumps(metadata, sort_keys=True), flush=True)
        with tempfile.TemporaryDirectory(prefix='gdi-deploy-') as temporary:
            verification = Path(temporary)
            stage = 'runtime smoke checks'
            smoke_environment = {**process_environment, 'XDG_CONFIG_HOME': str(verification / 'config')}
            for arguments in (('--help',), ('--version',), ('agent', 'snapshot', '--help'), ('agent', 'clone', '--help')):
                subprocess.run([sys.executable, '-I', str(output / 'gdi.pyz'), *arguments],
                               cwd=verification, env=smoke_environment, stdout=subprocess.DEVNULL, check=True)
            if args.build_only:
                print(f'deploy: build verified: {output / "gdi.pyz"} (no remote changes)')
                return 0
            stage = 'source check'
            if (git(repository, 'status', '--porcelain=v1', '--untracked-files=all')
                    or git(repository, 'rev-parse', 'HEAD').decode().strip() != metadata['head']
                    or git(repository, 'symbolic-ref', 'HEAD').decode().strip() != metadata['ref']):
                raise ValueError('source changed since runtime build; no publication attempted')
            stage = 'full GDI publication'
            print('deploy: publishing a full checkpoint', flush=True)
            result = subprocess.run([sys.executable, '-m', 'gdi', 'push', 'drive', '--full', '--json'],
                                    cwd=repository, env=process_environment, stdout=subprocess.PIPE, check=True)
            stage = 'publication check'
            publication = json.loads(result.stdout)
            if (publication['head'] != metadata['head']
                    or 'refs/heads/' + publication['branch'] != metadata['ref']
                    or publication['bundle_kind'] != 'full'):
                raise ValueError('full publication does not match the built runtime; runtime was not uploaded')
            print(f'deploy: published {publication["head"]} ({publication["publication_id"]})', flush=True)
            stage = 'runtime upload'
            print(f'deploy: uploading runtime to {args.runtime_dir}', flush=True)
            filenames = ('gdi.pyz', 'runtime.json', 'gdi.pyz.sha256')
            for filename in filenames:
                subprocess.run(['rclone', 'copyto', str(output / filename), args.runtime_dir.rstrip('/') + '/' + filename],
                               cwd=repository, env=process_environment, check=True)
            stage = 'runtime download verification'
            for filename in filenames:
                subprocess.run(['rclone', 'copyto', args.runtime_dir.rstrip('/') + '/' + filename,
                                str(verification / filename)], cwd=repository, env=process_environment, check=True)
                if (output / filename).read_bytes() != (verification / filename).read_bytes():
                    raise ValueError('uploaded file differs: ' + filename)
            print(f'deploy: complete; uploaded files verified at {args.runtime_dir}')
    except subprocess.CalledProcessError as error:
        print(f'deploy: failed during {stage}; runtime files remain in {output}', file=sys.stderr)
        return error.returncode if error.returncode > 0 else 128 - error.returncode
    except (ValueError, OSError, KeyError, SyntaxError) as error:
        print(f'deploy: {error}; failed during {stage}; runtime files remain in {output}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f'deploy: interrupted during {stage}; runtime files remain in {output}', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
